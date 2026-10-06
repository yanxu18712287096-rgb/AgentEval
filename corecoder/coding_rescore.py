"""Re-test immutable coding patches under a new scorer without asking the model again."""

import argparse
import copy
import json
from pathlib import Path
import tempfile

from . import __version__
from .coding_eval import (_container_command, _file_hash, _hash, _materialize,
                          _safe_relative, load_cases, require_docker, tool_protocol_failures,
                          out_of_scope_changes)


SCORER_VERSION = "coding-artifact-3"


def rescore_one(case, root, source, image):
    source = Path(source)
    row = json.loads((source / "result.json").read_text(encoding="utf-8"))
    if row["case_id"] != case["id"] or row["fingerprint"]["case"] != _hash(case):
        raise ValueError(f"source case differs: {source}")
    for filename, key in (("trace.jsonl", "trace_sha256"), ("patch.json", "patch_sha256"),
                          ("test-results.json", "tests_sha256")):
        if row.get(key) != _file_hash(source / filename):
            raise ValueError(f"source artifact hash differs: {source / filename}")
    events = [json.loads(line) for line in (source / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    if (not events or events[0].get("event") != "run_started" or events[-1].get("event") != "run_finished"
            or any(e.get("seq") != i or e.get("run_id") != events[0].get("run_id")
                   for i, e in enumerate(events))
            or sum(e.get("event") == "run_finished" for e in events) != 1
            or events[0].get("case_id") != case["id"]):
        raise ValueError(f"source trace identity/order invalid: {source}")
    patch = json.loads((source / "patch.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="corecoder-rescore-") as temp:
        workspace = Path(temp)
        _materialize(workspace, case["fixture"])
        for name, item in patch.items():
            path = workspace / _safe_relative(name)
            if item.get("before_sha256") != (_file_hash(path) if path.is_file() else None):
                raise ValueError(f"patch starting state differs: {name}")
            if item.get("after") is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item["after"], encoding="utf-8")
            if item.get("after_sha256") != (_file_hash(path) if path.is_file() else None):
                raise ValueError(f"patch content truncated or modified: {name}")
        public = _container_command(workspace, "python -B " + case["public_test"], 40, image=image)
        hidden_file = (root / case["hidden_test"]).resolve()
        private = _container_command(workspace, "python -B /hidden_test.py", 40,
                                     hidden=hidden_file, image=image)
    requested = [e for e in events if e["event"] == "tool_requested"]
    finished = [e for e in events if e["event"] == "tool_finished"]
    failures = []
    if row.get("error_type") or any(e["event"] == "turn_failed" for e in events):
        failures.append("agent_runtime_error")
    failures.extend(tool_protocol_failures(events))
    if public["infra_error"] or private["infra_error"]:
        failures.append("evaluation_infrastructure_error")
    if public["exit_code"] != 0:
        failures.append("public_test_failed")
    if private["exit_code"] != 0:
        failures.append("hidden_test_failed")
    if not patch:
        failures.append("no_patch")
    if out_of_scope_changes(case, patch):
        failures.append("out_of_scope_change")
    if row["answer"] == "(reached maximum tool-call rounds)":
        failures.append("round_limit")
    tool_errors = sum(e.get("status") == "failed" for e in finished)
    revised = copy.deepcopy(row)
    revised.update(status="pass" if not failures else "fail", failures=failures,
                   tool_error_count=tool_errors,
                   efficiency_findings=(["recovered_tool_errors"] if tool_errors else []),
                   historical_artifact_rescore=True,
                   source_result_sha256=_file_hash(source / "result.json"),
                   source_trace_sha256=row["trace_sha256"],
                   scorer_version=SCORER_VERSION,
                   scorer_code_sha256=_file_hash(__file__),
                   scoring_hidden_test_sha256=_file_hash(root / case["hidden_test"]),
                   scoring_runtime_version=__version__,
                   source_run=str(source),
                   public_test=public, hidden_test=private)
    return revised


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="python:3.11-slim")
    args = parser.parse_args()
    root, cases = load_cases(args.cases)
    require_docker(args.image)
    source_cases = json.loads((args.source / "cases.json").read_text(encoding="utf-8"))
    if source_cases != json.loads(args.cases.read_text(encoding="utf-8")):
        parser.error("source case definitions differ; historical rescore only permits scorer/test change")
    source_report = json.loads((args.source / "scorecard.json").read_text(encoding="utf-8"))
    if not source_report.get("complete"):
        parser.error("source run incomplete; resume it before rescoring")
    rows = []
    for case in cases:
        for repeat in range(source_report["repeats"]):
            run = args.source / f"{case['id']}-{repeat:02d}"
            rows.append(rescore_one(case, root, run, args.image))
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"operation": "historical_artifact_rescore", "source_report": str(args.source / "scorecard.json"),
              "source_report_sha256": _file_hash(args.source / "scorecard.json"),
              "scorer_version": SCORER_VERSION, "scorer_code_sha256": _file_hash(__file__),
              "runs": len(rows), "passed": sum(r["status"] == "pass" for r in rows),
              "by_split": {split: {"runs": sum(r["split"] == split for r in rows),
                                  "passed": sum(r["split"] == split and r["status"] == "pass" for r in rows)}
                           for split in ("development", "holdout")},
              "total_tokens": sum(r["total_tokens"] for r in rows if r["total_tokens"] is not None),
              "token_observed_runs": sum(r["total_tokens"] is not None for r in rows),
              "results": rows}
    (args.output / "scorecard.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Historical artifact re-test: {report['passed']}/{report['runs']} objective passes; no model calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
