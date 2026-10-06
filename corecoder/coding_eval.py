"""Container-backed coding Agent evaluation. Tool execution never falls back to the host."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from . import __version__
from .agent import Agent
from . import checkpoints
from .config import Config
from .llm import LLM, LiteLLM
from .tools.base import Tool
from .tools.read import ReadFileTool
from .tools.edit import EditFileTool, _changed_files
from .tools.write import WriteFileTool
from .trace import TraceRecorder
from .evaluation import PacedLLM
from .scenarios import RoleSpec, SkillSpec
from dataclasses import asdict


IMAGE = "python:3.11-slim"
MAX_OUTPUT = 16_000
ROLE = ("You are maintaining a Python repository in /workspace. Read the code and public tests, "
        "make the smallest justified change, run the public test with `python -B test_public.py` "
        "(pytest is not installed), and report only verified results. "
        "The shell is read-only and has no network; use edit_file/write_file to modify code. "
        "Never claim hidden tests passed: you cannot see or run them.")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_relative(name):
    p = Path(name)
    if p.is_absolute() or not p.parts or any(part in ("..", ".git") for part in p.parts):
        raise ValueError(f"unsafe relative path: {name}")
    return p


def load_cases(path):
    root = Path(path).resolve().parent
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("coding cases must be a nonempty list")
    seen = set()
    for case in cases:
        if case["id"] in seen or case.get("split") not in {"development", "holdout"}:
            raise ValueError("duplicate id or invalid split")
        seen.add(case["id"])
        if not isinstance(case.get("issue"), str) or not case["issue"].strip():
            raise ValueError("issue is required")
        files = case["fixture"]
        if not isinstance(files, dict) or not files:
            raise ValueError("fixture files are required")
        for name, content in files.items():
            _safe_relative(name)
            if not isinstance(content, str):
                raise ValueError("fixture content must be text")
        for key in ("hidden_test", "public_test", "allowed_changes"):
            if key not in case:
                raise ValueError(f"missing {key}")
        hidden = (root / _safe_relative(case["hidden_test"])).resolve()
        if not hidden.is_relative_to(root) or not hidden.is_file():
            raise ValueError("hidden test must exist inside case directory")
        if _safe_relative(case["public_test"]).as_posix() not in files:
            raise ValueError("public test must be in fixture")
        if not case["allowed_changes"] or any(_safe_relative(p).as_posix() not in files
                                               for p in case["allowed_changes"]):
            raise ValueError("allowed changes must name fixture files")
    return root, cases


def require_docker(image=IMAGE):
    try:
        subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        subprocess.run(["docker", "image", "inspect", image], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Docker daemon or local image {image} unavailable; no host fallback") from exc


def image_digest(image=IMAGE):
    """Fingerprint the actual local image, not a mutable human-readable tag."""
    proc = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                          capture_output=True, text=True, check=True, timeout=15)
    return proc.stdout.strip()


def _docker_args(workspace, hidden=None):
    args = ["docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "512m", "--cpus", "1",
            "--user", "65534:65534", "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/workspace",
            "--mount", f"type=bind,src={Path(workspace).resolve()},dst=/workspace,readonly"]
    if hidden is not None:
        args += ["--mount", f"type=bind,src={Path(hidden).resolve()},dst=/hidden_test.py,readonly"]
    return args


def _container_command(workspace, command, timeout=30, hidden=None, image=IMAGE):
    if timeout < 1 or timeout > 120:
        return {"exit_code": None, "output": "Error: timeout must be 1..120 seconds", "infra_error": False}
    args = _docker_args(workspace, hidden) + [image, "sh", "-lc", "cd /workspace && " + command]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 10)
        output = (proc.stdout + proc.stderr)[:MAX_OUTPUT]
        return {"exit_code": proc.returncode, "output": output,
                "infra_error": proc.returncode in {125, 126, 127}}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "output": f"Error: container command timed out after {timeout}s",
                "infra_error": True}


class WorkspaceFileTool(Tool):
    """Project-rooted adapter; never expose host paths outside this case."""

    def __init__(self, workspace, delegate):
        self.workspace = Path(workspace).resolve()
        self.delegate = delegate
        self.name, self.description, self.parameters = delegate.name, delegate.description, delegate.parameters

    def execute(self, **kwargs):
        name = kwargs.get("file_path", "")
        if name == "/workspace":
            name = "."
        elif name.startswith("/workspace/"):
            name = name[len("/workspace/"):]
        elif Path(name).is_absolute():
            return "Error: path outside evaluation workspace"
        candidate = (self.workspace / name).resolve()
        if not candidate.is_relative_to(self.workspace) or candidate == self.workspace:
            return "Error: path outside evaluation workspace"
        kwargs["file_path"] = str(candidate)
        return self.delegate.execute(**kwargs).replace(str(self.workspace), "/workspace")


class ContainerBashTool(Tool):
    name = "bash"
    description = "Run a read-only shell command in the isolated /workspace container; no network."
    parameters = {"type": "object", "properties": {
        "command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}

    def __init__(self, workspace, image=IMAGE):
        self.workspace, self.image = workspace, image

    def execute(self, command, timeout=30):
        result = _container_command(self.workspace, command, timeout=timeout, image=self.image)
        return result["output"] + (f"\n[exit code: {result['exit_code']}]" if result["exit_code"] else "")


class CodingAgent(Agent):
    """Serialize a model's tool batch: tests must not race with edits."""

    def _exec_tools_parallel(self, tool_calls, on_tool=None):
        results = []
        for call in tool_calls:
            if on_tool:
                on_tool(call.name, call.arguments)
            result = self._gate(call)
            if result is None:
                result = self._exec_tool(call)
                self._post_hooks(call, result)
            results.append(result)
        return results


def _materialize(workspace, files):
    for name, content in files.items():
        path = workspace / _safe_relative(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _snapshot(workspace):
    return {p.relative_to(workspace).as_posix(): _file_hash(p)
            for p in workspace.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def tool_protocol_failures(events):
    requested = [e for e in events if e["event"] == "tool_requested"]
    finished = [e for e in events if e["event"] == "tool_finished"]
    errors = []
    if Counter(e.get("call_key") for e in requested) != Counter(e.get("call_key") for e in finished):
        errors.append("tool_result_pairing")
    ends = {e.get("call_key"): e for e in finished}
    for call in requested:
        end = ends.get(call.get("call_key"))
        if end and (end.get("seq", -1) <= call.get("seq", -1) or any(
                end.get(k) != call.get(k) for k in
                ("tool_name", "tool_call_id", "agent_id", "turn", "round"))):
            errors.append("tool_result_identity_or_order")
            break
    identities = [(e.get("agent_id"), e.get("turn"), e.get("round"), e.get("tool_call_id"))
                  for e in requested]
    if len(identities) != len(set(identities)) or any(not identity[-1] for identity in identities):
        errors.append("invalid_tool_call_id")
    return errors


def out_of_scope_changes(case, patch):
    """Allow new self-tests; never allow edits to bundled tests or other files."""
    allowed = set(case["allowed_changes"])
    rejected = []
    for name, change in patch.items():
        if name in allowed:
            continue
        path = Path(name)
        created_test = (path.name.startswith("test_") and path.suffix == ".py"
                        and name != case["public_test"] and change.get("before_sha256") is None
                        and change.get("after_sha256") is not None)
        if not created_test:
            rejected.append(name)
    return sorted(rejected)


def _tools(workspace, image):
    tools = [WorkspaceFileTool(workspace, t) for t in (ReadFileTool(), EditFileTool(), WriteFileTool())]
    tools.append(ContainerBashTool(workspace, image))
    return tools


def coding_role(skill=None):
    """The evaluation and strategy experiment use the same runtime contract."""
    if skill is not None and not isinstance(skill, SkillSpec):
        raise ValueError("coding strategy must be a SkillSpec")
    return RoleSpec("coding", "1", ROLE,
                    ("read_file", "edit_file", "write_file", "bash"),
                    (skill,) if skill else ())


def coding_fingerprint(case, case_root, model, image, model_config=None, skill=None):
    hidden = (case_root / case["hidden_test"]).resolve()
    return {"runtime": __version__, "case": _hash(case), "hidden_test": _file_hash(hidden),
            "image": image_digest(image), "role": _hash(ROLE), "model": model,
            "model_config": model_config or {}, "runner_code": _file_hash(__file__),
            "agent_code": _file_hash(Path(__file__).parent / "agent.py"),
            "tools": _hash([t.schema() for t in _tools(Path("/workspace"), image)]),
            "strategy": json.loads(json.dumps(asdict(skill))) if skill else None,
            "strategy_hash": _hash(asdict(skill) if skill else None),
            "prompt_hash": _hash(coding_role(skill).render()),
            "runtime_sources": _hash({str(p.relative_to(Path(__file__).parent)): _file_hash(p)
                                      for p in sorted(Path(__file__).parent.rglob("*.py"))})}


def run_case(case, case_root, llm, output, repeat=0, image=IMAGE, model_config=None, mode="live", skill=None):
    role = coding_role(skill)
    prompt = role.render()  # validate compatibility before creating artifacts
    require_docker(image)
    # These legacy tool helpers are process-global; never carry case state forward.
    checkpoints.clear()
    _changed_files.clear()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="corecoder-coding-") as temp:
        workspace = Path(temp)
        _materialize(workspace, case["fixture"])
        before = _snapshot(workspace)
        tools = _tools(workspace, image)
        hidden = (case_root / case["hidden_test"]).resolve()
        fingerprint = coding_fingerprint(case, case_root, llm.model, image, model_config, skill)
        trace = TraceRecorder(output / "trace.jsonl", case_id=case["id"], repeat=repeat,
                              mode=mode, fingerprint=fingerprint, case_definition=case)
        agent = CodingAgent(llm, tools=tools, system_prompt_override=prompt, max_rounds=20, trace=trace)
        started = time.monotonic()
        error = None
        try:
            answer = agent.chat(case["issue"] + "\nWorkspace: /workspace")
        except Exception as exc:
            answer, error = "", type(exc).__name__
        after = _snapshot(workspace)
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        patch = {p: {"before_sha256": before.get(p), "after_sha256": after.get(p),
                     "after": (workspace / p).read_text(encoding="utf-8", errors="replace")
                     if p in after else None} for p in changed}
        (output / "patch.json").write_text(json.dumps(patch, indent=2, ensure_ascii=False), encoding="utf-8")
        public = _container_command(workspace, "python -B " + case["public_test"], 40, image=image)
        private = _container_command(workspace, "python -B /hidden_test.py", 40, hidden=hidden, image=image)
        (output / "test-results.json").write_text(json.dumps({"public": public, "hidden": private}, indent=2),
                                                    encoding="utf-8")
        failures = []
        if error:
            failures.append("agent_runtime_error")
        if public["exit_code"] != 0:
            failures.append("public_test_failed")
        if private["exit_code"] != 0:
            failures.append("hidden_test_failed")
        if public["infra_error"] or private["infra_error"]:
            failures.append("evaluation_infrastructure_error")
        if not changed:
            failures.append("no_patch")
        if out_of_scope_changes(case, patch):
            failures.append("out_of_scope_change")
        if answer == "(reached maximum tool-call rounds)":
            failures.append("round_limit")
        requested = [e for e in trace.events if e["event"] == "tool_requested"]
        finished = [e for e in trace.events if e["event"] == "tool_finished"]
        agent_tested = any(e.get("tool_name") == "bash" and
                           case["public_test"] in str(e.get("arguments", {}).get("command", ""))
                           for e in requested)
        failures.extend(tool_protocol_failures(trace.events))
        tool_errors = sum(e.get("status") == "failed" for e in finished)
        if any(e.get("status") in {"aborted", "interrupted"} for e in finished):
            failures.append("tool_execution_interrupted")
        if any(e["event"] == "turn_failed" for e in trace.events):
            failures.append("incomplete_run")
        trace.record("run_finished", error_type=error, answer=answer, changed_files=changed,
                     public=public, hidden=private, elapsed_seconds=time.monotonic() - started)
        responses = [e for e in trace.events if e["event"] == "model_response"]
        row = {"case_id": case["id"], "split": case["split"], "repeat": repeat,
               "status": "pass" if not failures else "fail", "failures": failures,
               "error_type": error,
               "agent_ran_public_test": agent_tested, "answer_review_pending": True,
               "evidence_findings": [] if agent_tested else ["agent_did_not_run_public_test"],
               "tool_error_count": tool_errors,
               "efficiency_findings": ["recovered_tool_errors"] if tool_errors else [],
               "fingerprint": fingerprint, "changed_files": changed, "answer": answer,
               "model_rounds": len(responses),
               "tool_calls": len(requested),
               "total_tokens": sum(e["prompt_tokens"] + e["completion_tokens"] for e in responses)
               if responses and all(e.get("usage_available") for e in responses) else None,
               "elapsed_seconds": time.monotonic() - started, "trace": str(output / "trace.jsonl")}
        row["trace_sha256"] = _file_hash(output / "trace.jsonl")
        row["patch_sha256"] = _file_hash(output / "patch.json")
        row["tests_sha256"] = _file_hash(output / "test-results.json")
        (output / "result.json").write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
        checkpoints.clear()
        _changed_files.clear()
        return row


def compare_reports(candidate, baseline):
    """Refuse numeric comparison whenever any execution/scoring fingerprint differs."""
    if candidate.get("mode") != baseline.get("mode"):
        raise ValueError("cannot compare runs with different modes")
    old = {(row["case_id"], row["repeat"]): row for row in baseline["results"]}
    changes = []
    for row in candidate["results"]:
        key = row["case_id"], row["repeat"]
        before = old.get(key)
        if before is None:
            changes.append({"case_id": key[0], "repeat": key[1], "status": "missing_baseline"})
        elif before.get("fingerprint") != row.get("fingerprint"):
            changes.append({"case_id": key[0], "repeat": key[1], "status": "not_comparable",
                            "changed_fields": sorted(k for k in set(before["fingerprint"]) | set(row["fingerprint"])
                                                     if before["fingerprint"].get(k) != row["fingerprint"].get(k))})
        else:
            changes.append({"case_id": key[0], "repeat": key[1], "status": "comparable",
                            "before": before["status"], "after": row["status"],
                            "before_tokens": before.get("total_tokens"), "after_tokens": row.get("total_tokens"),
                            "before_tools": before["tool_calls"], "after_tools": row["tool_calls"]})
    return changes


def recover_coding_result(directory, expected_fingerprint):
    """Reuse only a completed, hash-consistent run with the exact current fingerprint."""
    directory = Path(directory)
    result_path = directory / "result.json"
    if not result_path.is_file():
        return None
    row = json.loads(result_path.read_text(encoding="utf-8"))
    if row.get("fingerprint") != expected_fingerprint:
        raise ValueError(f"resume fingerprint changed: {directory}")
    for name, recorded in (("trace.jsonl", row.get("trace_sha256")),
                           ("patch.json", row.get("patch_sha256")),
                           ("test-results.json", row.get("tests_sha256"))):
        if not recorded or _file_hash(directory / name) != recorded:
            raise ValueError(f"resume artifact differs: {directory / name}")
    events = [json.loads(line) for line in (directory / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    if not events or events[-1].get("event") != "run_finished":
        raise ValueError(f"resume trace incomplete: {directory}")
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--baseline", type=Path, help="compare only identical fingerprints")
    parser.add_argument("--resume", action="store_true", help="reuse only complete, matching runs")
    parser.add_argument("--strategy", type=Path, help="explicit frozen coding candidate; never auto-activated")
    parser.add_argument("--request-gap", type=float, default=6.0,
                        help="minimum gap between live model requests (default 6s)")
    parser.add_argument("--rate-limit-retries", type=int, default=2)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    if args.request_gap < 0 or args.rate_limit_retries < 0:
        parser.error("request gap/retries must be nonnegative")
    root, cases = load_cases(args.cases)
    skill = None
    if args.strategy:
        from .coding_evolution import candidate_skill
        skill = candidate_skill(json.loads(args.strategy.read_text(encoding="utf-8")))
    if args.case_id:
        cases = [c for c in cases if c["id"] in args.case_id]
        if len(cases) != len(set(args.case_id)):
            parser.error("unknown case id")
    require_docker(args.image)
    if args.dry_run:
        print(f"Validated {len(cases)} cases and local Docker image {args.image}; no model calls")
        return 0
    cfg = Config.from_env()
    if cfg.provider != "litellm" and not cfg.api_key:
        parser.error("live evaluation requires model API key")
    if args.resume:
        if not args.output.is_dir() or json.loads((args.output / "cases.json").read_text(encoding="utf-8")) != json.loads(args.cases.read_text(encoding="utf-8")):
            parser.error("resume requires the same existing cases.json")
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(args.cases, args.output / "cases.json")
    rows = []
    stopped_for_provider = False
    request_clock = [0.0]
    model_config = {"provider": cfg.provider, "temperature": cfg.temperature,
                    "max_tokens": cfg.max_tokens, "endpoint_hash": _hash(cfg.base_url),
                    "request_gap": args.request_gap, "rate_limit_retries": args.rate_limit_retries}
    for case in cases:
        for repeat in range(args.repeat):
            run_dir = args.output / f"{case['id']}-{repeat:02d}"
            expected = coding_fingerprint(case, root, cfg.model, args.image, model_config, skill)
            if args.resume and run_dir.exists():
                row = recover_coding_result(run_dir, expected)
                if row is not None and row.get("error_type") not in {"RateLimitError", "APIConnectionError", "APITimeoutError"}:
                    rows.append(row)
                    print(f"REUSED {case['id']} #{repeat + 1}: {row['status']}")
                    continue
                interrupted = run_dir.with_name(run_dir.name + ".interrupted")
                if interrupted.exists():
                    parser.error(f"interrupted archive already exists: {interrupted}")
                run_dir.rename(interrupted)
            cls = LiteLLM if cfg.provider == "litellm" else LLM
            llm = cls(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
                      temperature=cfg.temperature, max_tokens=cfg.max_tokens)
            llm = PacedLLM(llm, request_clock, args.request_gap, args.rate_limit_retries)
            row = run_case(case, root, llm, run_dir,
                           repeat, args.image, model_config, skill=skill)
            rows.append(row)
            print(f"{row['status'].upper()} {case['id']} #{repeat + 1}: {row['failures']}")
            if row.get("error_type") in {"RateLimitError", "APIConnectionError", "APITimeoutError"}:
                stopped_for_provider = True
                break
        if stopped_for_provider:
            break
    report = {"runtime_version": __version__, "mode": "live", "model": cfg.model,
              "case_count": len(cases), "repeats": args.repeat, "passed": sum(r["status"] == "pass" for r in rows),
              "total": len(rows), "expected_runs": len(cases) * args.repeat,
              "complete": not stopped_for_provider and len(rows) == len(cases) * args.repeat,
              "results": rows}
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report["comparison"] = compare_reports(report, baseline)
    (args.output / "scorecard.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
