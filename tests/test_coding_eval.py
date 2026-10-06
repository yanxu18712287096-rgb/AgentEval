"""Infrastructure and case logic checks; these are not live model results."""

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from corecoder.coding_eval import (WorkspaceFileTool, _container_command, _hash, _materialize,
                                   compare_reports, load_cases, require_docker, tool_protocol_failures)
from corecoder.tools.read import ReadFileTool
from corecoder.coding_rescore import rescore_one
from corecoder.coding_eval import run_case
from corecoder.coding_eval import out_of_scope_changes
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall


CASES = Path(__file__).parent.parent / "eval" / "coding" / "cases.json"


def test_new_self_test_allowed_but_existing_test_and_other_files_rejected():
    case = {"allowed_changes": ["target.py"], "public_test": "test_public.py"}
    before = "old"
    added = {"before_sha256": None, "after_sha256": "new"}
    modified = {"before_sha256": before, "after_sha256": "new"}
    assert out_of_scope_changes(case, {"target.py": modified, "tests/test_regression.py": added}) == []
    assert out_of_scope_changes(case, {"test_public.py": modified, "tests/test_regression.py": modified,
                                       "notes.txt": added}) == ["notes.txt", "test_public.py", "tests/test_regression.py"]


def test_coding_cases_have_valid_manifest_and_separate_holdout():
    root, cases = load_cases(CASES)
    assert len(cases) == 8
    assert {c["split"] for c in cases} == {"development", "holdout"}
    assert all((root / c["hidden_test"]).is_file() for c in cases)


@pytest.mark.parametrize("case_id", [f"CODE-{i:02d}" for i in range(1, 9)])
def test_public_passes_and_hidden_reveals_original_bug(case_id):
    root, cases = load_cases(CASES)
    case = next(c for c in cases if c["id"] == case_id)
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        _materialize(workspace, case["fixture"])
        env = dict(os.environ, PYTHONPATH=str(workspace), PYTHONDONTWRITEBYTECODE="1")
        public = subprocess.run([sys.executable, "-B", case["public_test"]], cwd=workspace, env=env,
                                capture_output=True, text=True)
        hidden = subprocess.run([sys.executable, "-B", str(root / case["hidden_test"])],
                                cwd=workspace, env=env, capture_output=True, text=True)
        assert public.returncode == 0, public.stderr
        assert hidden.returncode != 0, "baseline must expose the issue"


def test_file_tool_rejects_escape_and_accepts_workspace_prefix(tmp_path):
    (tmp_path / "target.py").write_text("x = 1\n", encoding="utf-8")
    tool = WorkspaceFileTool(tmp_path, ReadFileTool())
    assert "x = 1" in tool.execute(file_path="/workspace/target.py")
    assert "outside evaluation workspace" in tool.execute(file_path="/etc/passwd")
    assert "outside evaluation workspace" in tool.execute(file_path="../secret")
    (tmp_path / "link").symlink_to("/etc/passwd")
    assert "outside evaluation workspace" in tool.execute(file_path="link")


def test_strict_coding_regression_comparison():
    row = {"case_id": "CODE-01", "repeat": 0, "status": "pass", "tool_calls": 2,
           "total_tokens": 100, "fingerprint": {"case": "a", "model": "m"}}
    same = {"mode": "live", "results": [dict(row)]}
    assert compare_reports(same, same)[0]["status"] == "comparable"
    changed = {"mode": "live", "results": [dict(row, fingerprint={"case": "b", "model": "m"})]}
    result = compare_reports(changed, same)[0]
    assert result["status"] == "not_comparable"
    assert result["changed_fields"] == ["case"]


def test_historical_rescore_rejects_modified_artifacts(tmp_path):
    root, cases = load_cases(CASES)
    case = cases[0]
    (tmp_path / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "patch.json").write_text("{}", encoding="utf-8")
    (tmp_path / "test-results.json").write_text("{}", encoding="utf-8")
    (tmp_path / "result.json").write_text(json.dumps({
        "case_id": case["id"], "fingerprint": {"case": _hash(case)},
        "trace_sha256": "wrong", "patch_sha256": "wrong", "tests_sha256": "wrong"}), encoding="utf-8")
    with pytest.raises(ValueError, match="source artifact hash differs"):
        rescore_one(case, root, tmp_path, "python:3.11-slim")


def test_coding_tool_result_requires_matching_name_and_id():
    base = {"call_key": "one", "tool_call_id": "id1", "tool_name": "read_file",
            "agent_id": "agent", "turn": 1, "round": 1}
    events = [dict(base, event="tool_requested", seq=0),
              dict(base, event="tool_finished", seq=1)]
    assert tool_protocol_failures(events) == []
    events[1]["tool_name"] = "write_file"
    assert "tool_result_identity_or_order" in tool_protocol_failures(events)


@pytest.mark.skipif(os.getenv("RUN_DOCKER_CODING_TESTS") != "1", reason="requires Docker daemon")
@pytest.mark.parametrize("case_id", [f"CODE-{i:02d}" for i in range(1, 9)])
def test_container_detects_original_bug(case_id):
    require_docker()
    root, cases = load_cases(CASES)
    case = next(c for c in cases if c["id"] == case_id)
    with tempfile.TemporaryDirectory() as temp:
        workspace = Path(temp)
        _materialize(workspace, case["fixture"])
        public = _container_command(workspace, "python -B " + case["public_test"], 30)
        hidden = _container_command(workspace, "python -B /hidden_test.py", 30,
                                    hidden=root / case["hidden_test"])
        assert public["exit_code"] == 0, public
        assert hidden["exit_code"] != 0, hidden


@pytest.mark.skipif(os.getenv("RUN_DOCKER_CODING_TESTS") != "1", reason="requires Docker daemon")
def test_container_shell_cannot_modify_fixture(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    result = _container_command(tmp_path, "python -B -c 'open(\"target.py\", \"w\").write(\"WRONG\")'", 20)
    assert result["exit_code"] != 0
    assert target.read_text(encoding="utf-8") == "ORIGINAL\n"


@pytest.mark.skipif(os.getenv("RUN_DOCKER_CODING_TESTS") != "1", reason="requires Docker daemon")
def test_recovered_edit_error_is_efficiency_not_task_failure(tmp_path):
    root, cases = load_cases(CASES)
    case = cases[0]
    fixed = "def average(values):\n    if not values:\n        return 0.0\n    return sum(values) / len(values)\n"
    script = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("bad", "edit_file", {"file_path": "/workspace/target.py",
                                                                "old_string": "not there", "new_string": "x"})]),
        LLMResponse(tool_calls=[ToolCall("good", "write_file", {"file_path": "/workspace/target.py",
                                                                 "content": fixed})]),
        LLMResponse(tool_calls=[ToolCall("test", "bash", {"command": "python -B test_public.py"})]),
        LLMResponse(content="Fixed and ran the public test.")])
    row = run_case(case, root, script, tmp_path / "run", mode="scripted")
    assert row["status"] == "pass"
    assert row["tool_error_count"] == 1
    assert row["efficiency_findings"] == ["recovered_tool_errors"]
