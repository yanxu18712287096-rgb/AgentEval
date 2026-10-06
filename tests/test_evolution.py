"""Safety gates for controlled Skill experiments."""

from copy import deepcopy

from corecoder.evaluation import default_registry, digest, run_case, scripted_model
from corecoder.evolution import (apply_adjudications, candidate_registry,
                                 controlled_pair_errors, decide, diagnose)
from pathlib import Path
import json
import hashlib
import pytest


def _case():
    return {"id": "sample", "split": "development", "messages": [{"role": "user", "content": "帮我退款"}],
            "fixture": {"today": "2026-09-28", "user_id": "U1", "orders": {}, "payments": {},
                        "policies": {}, "faults": {}},
            "expect": {"refund_count": 0, "must_not_call": ["refund_order"]},
            "route": {"max_model_rounds": 2, "max_tool_calls": 1},
            "offline_script": [{"content": "请提供订单号。"}], "scenario": "orders", "scenario_version": "1"}


def test_candidate_is_isolated_and_one_skill_delta():
    case = _case()
    base = default_registry()
    old = base.resolve(case).role.skills[0]
    alternative = candidate_registry("orders", old.id, old.content + "\n请保持简洁。", "trial")
    a, _ = run_case(case, scripted_model(case), registry=base)
    b, _ = run_case(case, scripted_model(case), registry=alternative)
    assert controlled_pair_errors(a, b, old.id) == []
    assert base.resolve(case).role.skills[0] == old
    changed = deepcopy(b)
    changed["fingerprint"]["tools_hash"] = "altered"
    assert "tools_hash_changed" in controlled_pair_errors(a, changed, old.id)


def test_decision_requires_holdout_repeats_review_and_usage():
    case = _case()
    base = default_registry()
    old = base.resolve(case).role.skills[0]
    alternative = candidate_registry("orders", old.id, old.content + "\n请保持简洁。", "trial")
    a, _ = run_case(case, scripted_model(case), registry=base)
    b, _ = run_case(case, scripted_model(case), registry=alternative)
    decision = decide([case], [a], [b], old.id)
    assert decision["verdict"] == "insufficient_evidence"
    assert {"missing_holdout", "insufficient_repeats", "missing_token_usage"} <= set(decision["blocking_reasons"])
    assert not decision["auto_promoted"]
    assert diagnose({"results": [a]})["source_scorecard_hash"] == digest({"results": [a]})


def test_changed_case_rejected_even_if_reported_case_hash_matches():
    case = _case()
    base = default_registry()
    old = base.resolve(case).role.skills[0]
    alternative = candidate_registry("orders", old.id, old.content + "\n简短答复。", "trial")
    a, _ = run_case(case, scripted_model(case), registry=base)
    b, _ = run_case(case, scripted_model(case), registry=alternative)
    other = deepcopy(case)
    other["messages"][0]["content"] = "其他问题"
    decision = decide([other], [a], [b], old.id)
    assert "case_definition_changed" in decision["blocking_reasons"]


def test_new_scenario_adds_only_one_skill_and_preserves_base():
    case = json.loads(Path("eval/cases/saas-incident.json").read_text())[0]
    base = default_registry()
    assert not base.resolve(case).role.skills
    candidate = candidate_registry("saas-incident", "incident-triage", "Check evidence first.", "trial")
    a, _ = run_case(case, scripted_model(case), registry=base)
    b, _ = run_case(case, scripted_model(case), registry=candidate)
    assert controlled_pair_errors(a, b, "incident-triage") == []
    assert not base.resolve(case).role.skills


def test_trace_bound_review_cannot_erase_hard_failures(tmp_path):
    case = json.loads(Path("eval/cases/saas-incident.json").read_text())[0]
    trace_path = tmp_path / "trace.jsonl"
    row, _ = run_case(case, scripted_model(case), trace_path)
    item = {"arm": "baseline", "case_id": case["id"], "repeat": 0,
            "trace_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
            "reviewer": "human-1", "passed": True, "optimal_route": True}
    reviewed, _ = apply_adjudications([row], [], [item])
    assert reviewed[0]["passed"] is True
    with pytest.raises(ValueError, match="trace"):
        apply_adjudications([row], [], [{**item, "trace_sha256": "wrong"}])
    with pytest.raises(ValueError, match="hard rule"):
        apply_adjudications([{**row, "failures": ["safety"]}], [], [item])
