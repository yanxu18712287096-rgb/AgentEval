"""Build and audit repository fixtures without exposing verifier/reference files to agents.

Run as: python -m scripts.repository_suite {audit,freeze,build} --help
Generated manifests are mechanical exports, not hand-maintained duplicate fixtures.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from corecoder.coding_eval import _container_command, _materialize, _safe_relative

ROOT = Path(__file__).resolve().parents[1] / "eval" / "coding_repository"
VERSION = "repository-maintenance-v1.1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_files(root=ROOT):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for split in ("development", "holdout") for p in sorted((root / split).rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def tasks(root=ROOT):
    result, ids = [], set()
    for split in ("development", "holdout"):
        for path in sorted((root / split).glob("*/task.json")):
            task = read(path)
            if task["split"] != split or task["id"] in ids or not re.fullmatch(r"REPO-[DH][0-9]{2}", task["id"]):
                raise ValueError("invalid task identity or split")
            ids.add(task["id"])
            directory = path.parent
            fixture = {str(p.relative_to(directory / "repo")): p.read_text(encoding="utf-8")
                       for p in sorted((directory / "repo").rglob("*"))
                       if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
            for name in fixture:
                _safe_relative(name)
            if "test_public.py" not in fixture or not set(task["allowed_changes"]) <= set(fixture):
                raise ValueError("missing public test or writable file")
            if "test_public.py" in task["allowed_changes"]:
                raise ValueError("public tests must remain immutable")
            tree = ast.parse((directory / "verify.py").read_text(encoding="utf-8"))
            methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")}
            mapped = [name for names in task["requirements"].values() for name in names]
            if set(mapped) != methods or len(mapped) != len(set(mapped)):
                raise ValueError("requirement/test mapping must be complete and unique")
            for requirement in task["requirements"]:
                if requirement + "：" not in task["issue"]:
                    raise ValueError("verifier requirement missing from public issue")
            reference = read(directory / "reference.json")
            if len(reference) < 2 or not set(reference) <= set(task["allowed_changes"]):
                raise ValueError("reference must span at least two allowed implementation files")
            result.append((task, fixture, reference, directory))
    if {t[0]["split"] for t in result} != {"development", "holdout"}:
        raise ValueError("both splits required")
    return result


def repaired(fixture, reference, omit=None):
    files = dict(fixture)
    for name, edits in reference.items():
        if name == omit:
            continue
        for before, after in edits:
            if not before or files[name].count(before) != 1:
                raise ValueError(f"reference replacement must match exactly once: {name}")
            files[name] = files[name].replace(before, after, 1)
    return files


def execute(fixture, verifier, docker=False):
    with tempfile.TemporaryDirectory(prefix="repository-audit-") as temp:
        workspace = Path(temp)
        _materialize(workspace, fixture)
        if docker:
            public = _container_command(workspace, "python -B test_public.py", 30)
            private = _container_command(workspace, "python -B /hidden_test.py", 30, hidden=verifier)
        else:
            env = dict(os.environ, PYTHONPATH=str(workspace), PYTHONDONTWRITEBYTECODE="1")
            results = []
            for command in ([sys.executable, "-B", "test_public.py"], [sys.executable, "-B", str(verifier.resolve())]):
                run = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
                results.append({"exit_code": run.returncode, "output": run.stdout + run.stderr, "infra_error": False})
            public, private = results
        if public["infra_error"] or private["infra_error"]:
            raise RuntimeError("audit infrastructure failure")
        return {"public": public, "acceptance": private}


def audit(root=ROOT, docker=False):
    rows = []
    for task, fixture, reference, directory in tasks(root):
        baseline = execute(fixture, directory / "verify.py", docker)
        fixed = execute(repaired(fixture, reference), directory / "verify.py", docker)
        partial = {name: execute(repaired(fixture, reference, omit=name), directory / "verify.py", docker)
                   for name in reference}
        passed = (baseline["public"]["exit_code"] == 0 and baseline["acceptance"]["exit_code"] != 0
                  and fixed["public"]["exit_code"] == 0 and fixed["acceptance"]["exit_code"] == 0
                  and all(value["acceptance"]["exit_code"] != 0 for value in partial.values()))
        rows.append({"id": task["id"], "split": task["split"], "requirements": task["requirements"],
                     "repository_files": len(fixture), "reference_changed_files": list(reference),
                     "passed": passed, "baseline": baseline, "reference": fixed, "partial_repairs": partial})
        print(task["id"], "audit PASS" if passed else "audit FAIL", flush=True)
    return {"schema": "repository-suite-audit-v1", "version": VERSION, "source_hash": digest(source_files(root)),
            "mode": "docker" if docker else "local-stdlib", "model_calls": 0,
            "passed": all(row["passed"] for row in rows), "results": rows}


def freeze(report, root=ROOT):
    files = source_files(root)
    expected = {task["id"] for task, *_ in tasks(root)}
    if (not report.get("passed") or report.get("source_hash") != digest(files)
            or len(report["results"]) != len(expected) or {r["id"] for r in report["results"]} != expected
            or not all(r["passed"] for r in report["results"])):
        raise ValueError("freeze requires a complete passing audit of current sources")
    lock = {"version": VERSION, "source_hash": digest(files), "files": files,
            "audit_hash": digest(report), "holdout_status": "authored-and-audited; not independent-third-party-blind"}
    write(root / "suite-lock.json", lock)
    return lock


def build(output, split="all", root=ROOT):
    lock = read(root / "suite-lock.json")
    if lock["files"] != source_files(root) or lock["source_hash"] != digest(lock["files"]):
        raise ValueError("frozen suite drifted; create a new reviewed dataset version")
    selected = [row for row in tasks(root) if split == "all" or row[0]["split"] == split]
    if not selected:
        raise ValueError("empty selection")
    output.mkdir(parents=True, exist_ok=False)
    (output / "verifiers").mkdir()
    cases = []
    for task, fixture, _, directory in selected:
        fixture["TASK.md"] = task["issue"] + "\n"
        hidden = f"verifiers/{task['id']}.py"
        with (output / hidden).open("x", encoding="utf-8") as stream:
            stream.write((directory / "verify.py").read_text(encoding="utf-8"))
        cases.append({"id": task["id"], "split": task["split"], "issue": task["issue"], "fixture": fixture,
                      "public_test": "test_public.py", "hidden_test": hidden, "allowed_changes": task["allowed_changes"],
                      "dataset_version": VERSION, "suite_hash": lock["source_hash"], "requirements": task["requirements"]})
    write(output / "cases.json", cases)
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("audit")
    check.add_argument("--docker", action="store_true")
    check.add_argument("--output", type=Path, required=True)
    seal = sub.add_parser("freeze")
    seal.add_argument("--audit", type=Path, required=True)
    export = sub.add_parser("build")
    export.add_argument("--split", choices=("development", "holdout", "all"), default="all")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "audit":
        if args.output.exists():
            parser.error("audit output exists")
        report = audit(docker=args.docker)
        write(args.output, report)
        return 0 if report["passed"] else 1
    if args.action == "freeze":
        freeze(read(args.audit))
    else:
        build(args.output, args.split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
