import copy
import json
from pathlib import Path

from corecoder.evaluation import aggregate, run_case, scripted_model, rescore_trace


CASES = {c["id"]: c for c in json.loads(Path("eval/cases/examples.json").read_text())}


def execute(cid, answer, path=None):
    case = copy.deepcopy(CASES[cid])
    case["offline_script"][-1]["content"] = answer
    return run_case(case, scripted_model(case), path)[0]


def test_lexical_miss_is_pending_not_pass_or_fail(tmp_path):
    path = tmp_path / "trace.jsonl"
    result = execute("refund_success_1", "这笔款项的退回手续已经办妥。", path)
    assert result["status"] == "review"
    assert result["passed"] is None and result["optimal_route"] is None
    assert result["rule_passed"] and result["rule_optimal_route"]
    assert result["failures"] == [] and result["review_reasons"]
    assert result["dimensions"]["evidence"]["passed"]
    rescored = rescore_trace(path)["result"]
    assert rescored["status"] == "review" and rescored["passed"] is None


def test_explicit_contradiction_is_still_failure():
    result = execute("policy_3", "政策允许退款，目前仍在有效期内。")
    assert result["status"] == "fail" and result["passed"] is False
    assert result["needs_review"]  # Both kinds of evidence are retained.
    assert any(f.startswith("contradictory_answer_claim:") for f in result["failures"])


def test_three_way_aggregate_keeps_full_denominator():
    passed = execute("refund_success_1", "退款已完成。")
    pending = execute("refund_success_1", "已经办妥。")
    failed = execute("policy_3", "可以退款。")
    summary = aggregate([passed, pending, failed])
    assert summary["passed"] == summary["failed_runs"] == summary["review_runs"] == 1
    assert summary["success_rate"] == 1 / 3
    assert summary["rule_pass_rate"] == 2 / 3
    assert summary["needs_review_runs"] == 2


def test_first_turn_miss_is_review_not_hard_failure():
    case = copy.deepcopy(CASES["intent_change_1"])
    next(s for s in case["offline_script"] if "content" in s)["content"] = "核验通过，是否继续办理？"
    result, _ = run_case(case, scripted_model(case))
    assert result["status"] == "review" and result["rule_passed"]
    assert any(r.startswith("missing_answer_turn:") for r in result["review_reasons"])


def test_cli_exports_review_queue_and_distinct_exit_code(tmp_path, monkeypatch):
    from corecoder.evaluation import main
    case = copy.deepcopy(CASES["refund_success_1"])
    case["offline_script"][-1]["content"] = "已经办妥。"
    source, output = tmp_path / "cases.json", tmp_path / "report"
    source.write_text(json.dumps([case], ensure_ascii=False))
    monkeypatch.setattr("sys.argv", ["evaluation", "--cases", str(source), "--output", str(output)])
    assert main() == 2
    report = json.loads((output / "scorecard.json").read_text())
    assert report["passed"] == report["failed_runs"] == 0 and report["review_runs"] == 1
    queue = json.loads((output / "review-queue.json").read_text())
    assert len(queue) == 1 and queue[0]["trace"] and queue[0]["reasons"]
    assert "REVIEW" in (output / "report.md").read_text()
