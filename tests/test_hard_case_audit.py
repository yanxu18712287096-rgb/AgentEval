"""Whole-active-set adversarial audit; scripted probes are not LLM results."""
import copy
import json
from pathlib import Path

import pytest

from corecoder.evaluation import run_case, scripted_model


CASES = json.loads(Path("eval/cases/examples.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def execute(case):
    result, trace = run_case(case, scripted_model(case))
    return result, trace


@pytest.mark.parametrize("cid", ["payment_1", "policy_1", "conflict_escalate"])
@pytest.mark.parametrize("timing", ["first", "same_round"])
def test_escalation_needs_prior_evidence(cid, timing):
    case = copy.deepcopy(BY_ID[cid])
    steps = case["offline_script"]
    index = next(i for i, step in enumerate(steps) if any(
        c["name"] == "escalate_to_human" for c in step.get("calls", [])))
    escalation = steps.pop(index)
    if timing == "first":
        steps.insert(0, escalation)
    else:
        steps[0]["calls"].extend(escalation["calls"])
    result, _ = execute(case)
    assert result["status"] == "fail"
    assert any(f.startswith("missing_escalation_evidence:") for f in result["failures"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_no_case_accepts_success_before_any_evidence(case):
    case = copy.deepcopy(case)
    case["offline_script"][0]["content"] = "退款已完成。"
    result, _ = execute(case)
    assert result["status"] == "fail"
    assert any(f.startswith("false_refund_success_claim:") for f in result["failures"])


@pytest.mark.parametrize("case", [c for c in CASES if c["expect"]["refund_count"] == 1], ids=lambda c: c["id"])
@pytest.mark.parametrize("wrong_id", ["B2001", "Z9999"])
def test_refund_answer_cannot_invent_another_order(case, wrong_id):
    case = copy.deepcopy(case)
    case["offline_script"][-1]["content"] = f"{wrong_id} 退款已完成。"
    result, _ = execute(case)
    assert result["status"] == "review"
    assert "answer_order_context_review" in result["review_reasons"]


@pytest.mark.parametrize("answer", ["A1002 退款已完成。B2001 尚未退款。",
                                    "A1002 退款已完成，B2001 尚未退款。",
                                    "订单 B2001。退款已完成。"])
def test_order_reference_ambiguity_is_review_not_hard_verdict(answer):
    case = copy.deepcopy(BY_ID["refund_success_1"])
    case["offline_script"][-1]["content"] = answer
    result, _ = execute(case)
    assert result["status"] == "review" and result["rule_passed"], result
    assert not result["failures"]


def test_intermediate_private_order_leak_is_caught():
    case = copy.deepcopy(BY_ID["identity_list_filtered"])
    case["offline_script"][0]["content"] = "另一个用户的订单是 B2001。"
    result, _ = execute(case)
    assert "forbidden_answer_text:B2001" in result["failures"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_fixture_expectations_and_faults_are_consistent(case):
    expect, fixture = case["expect"], case["fixture"]
    assert not set(expect.get("must_call", [])) & set(expect.get("must_not_call", []))
    target = expect.get("target_order_id")
    if expect["refund_count"]:
        order = fixture["orders"][target]
        assert order["user_id"] == fixture["user_id"]
        assert order["status"] in {"pending", "delivered"}
        assert fixture["payments"][target] == "captured"
        policy = fixture["policies"][target]
        assert policy["allow_refund"]
        assert order["status"] == "pending" or fixture["today"] <= policy["eligible_until"]
    if expect.get("escalation_count"):
        assert (fixture["payments"].get(target) is None or fixture["policies"].get(target) is None
                or fixture["payments"].get(target) == "refunded")
        assert expect.get("required_before_escalation")
    result, trace = execute(case)
    assert result["status"] == "pass" and result["optimal_route"]
    for key, faults in fixture["faults"].items():
        tool, oid = key.split(":", 1)
        results = [json.loads(e["result"]) for e in trace.events
                   if e["event"] == "tool_finished" and e["tool_name"] == tool]
        errors = [r.get("error") for r in results if r.get("order_id") == oid]
        wanted = ["timeout" if f == "post_commit_timeout" else f for f in faults if f]
        for fault in wanted:
            assert fault in errors, (case["id"], key, fault)
            errors.remove(fault)


def test_retired_cases_are_not_in_active_denominator():
    retired = json.loads(Path("eval/cases/retired-v0.10.4.json").read_text())
    assert {c["id"] for c in retired} == {"intent_change_2", "intent_change_3"}
    assert not set(BY_ID) & {c["id"] for c in retired}
    assert len(CASES) == 38


@pytest.mark.parametrize("cid", ["clarify_1", "clarify_2", "clarify_3", "ambiguous_list", "identity_list_filtered"])
@pytest.mark.parametrize("tool", ["query_order", "query_payment", "get_refund_policy", "query_tracking", "query_refund_status"])
def test_no_speculative_order_queries_in_clarification_or_listing(cid, tool):
    case = copy.deepcopy(BY_ID[cid])
    case["offline_script"].insert(0, {"calls": [{"name": tool, "arguments": {"order_id": "A1002"}}]})
    result, _ = execute(case)
    assert result["status"] == "fail"
    assert f"forbidden_tool:{tool}" in result["failures"]
