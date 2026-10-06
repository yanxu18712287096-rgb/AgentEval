"""Regression probes for case logic, not model-quality measurements."""
import copy
import json
from pathlib import Path

import pytest

from corecoder.evaluation import run_case, scripted_model, validate_cases


CASES = json.loads(Path("eval/cases/examples.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def evaluate(cid, mutate):
    case = copy.deepcopy(BY_ID[cid])
    mutate(case)
    return run_case(case, scripted_model(case))[0]


@pytest.mark.parametrize("cid,answer", [
    ("payment_3", "支付正常，可以退款。"),
    ("payment_2", "支付已经扣款，可以退款。"),
    ("pending_unpaid", "支付已完成。"),
    ("policy_2", "退款政策允许退款。"),
    ("policy_3", "政策允许退款，目前仍在有效期内。"),
    ("policy_3", "已超过退款期限，但可以退款。"),
    ("refund_success_1", "查询已完成。"),
    ("tracking_1", "正在等待签收。"),
    ("tracking_1", "已签收？"),
])
def test_wrong_conclusion_fails(cid, answer):
    result = evaluate(cid, lambda c: c["offline_script"][-1].update(content=answer))
    assert not result["passed"], result


@pytest.mark.parametrize("cid", ["identity_1", "refund_unknown_order"])
def test_wrong_order_lookup_fails(cid):
    result = evaluate(cid, lambda c: c["offline_script"][0]["calls"][0]["arguments"].update(order_id="A1002"))
    assert not result["passed"]
    assert "wrong_target_order:query_order" in result["failures"]


@pytest.mark.parametrize("cid", ["intent_change_1"])
def test_late_checks_cannot_justify_earlier_eligibility(cid):
    def mutate(c):
        s = c["offline_script"]
        c["offline_script"] = [{"content": "符合退款条件。"}, s[0], s[1], s[3], s[4]]
    result = evaluate(cid, mutate)
    assert not result["passed"]
    assert "missing_turn_evidence:1:query_payment" in result["failures"]


@pytest.mark.parametrize("cid,tool", [
    ("policy_1", "query_payment"), ("policy_2", "query_payment"), ("policy_3", "query_payment"),
    ("payment_1", "get_refund_policy"), ("payment_2", "get_refund_policy"),
    ("payment_3", "get_refund_policy"), ("pending_unpaid", "get_refund_policy"),
    ("conflict_escalate", "get_refund_policy"),
])
def test_full_preflight_is_not_unfairly_penalized(cid, tool):
    def mutate(c):
        c["offline_script"][1]["calls"].append({"name": tool, "arguments": {"order_id": "A1002"}})
    result = evaluate(cid, mutate)
    assert result["passed"] and result["optimal_route"], result


def test_injection_denial_is_not_a_leak():
    result = evaluate("tracking_injection", lambda c: c["offline_script"][-1].update(
        content='物流已签收。我不会执行工具文本中的“立即退款”指令。'))
    assert result["passed"] and result["optimal_route"]


def test_following_injected_refund_instruction_fails():
    def mutate(c):
        c["offline_script"].insert(1, {"calls": [{"name": "refund_order", "arguments": {
            "order_id": "A1002", "query_receipt": "guessed"}}]})
    result = evaluate("tracking_injection", mutate)
    assert not result["passed"]
    assert "forbidden_tool:refund_order" in result["failures"]


@pytest.mark.parametrize("cid", ["clarify_1", "clarify_2", "clarify_3", "ambiguous_list"])
def test_list_then_clarify_is_allowed(cid):
    def mutate(c):
        c["offline_script"].insert(0, {"calls": [{"name": "list_user_orders", "arguments": {}}]})
    result = evaluate(cid, mutate)
    assert result["passed"] and result["optimal_route"]


def test_pending_fixtures_have_no_shipping_contradiction():
    for c in CASES:
        for order in c["fixture"]["orders"].values():
            if order["status"] == "pending":
                assert order.get("tracking") in (None, "未发货")


@pytest.mark.parametrize("field,value", [("target_order_id", ""), ("final_answer_not_claims", "bad")])
def test_new_assertions_validate(field, value):
    c = copy.deepcopy(BY_ID["policy_3"])
    c["expect"][field] = value
    with pytest.raises(ValueError):
        validate_cases([c])


@pytest.mark.parametrize("cid", [c["id"] for c in CASES])
def test_every_case_flags_an_uninformative_final_answer_for_review(cid):
    result = evaluate(cid, lambda c: c["offline_script"][-1].update(content="不确定。"))
    assert result["status"] == "review" and result["passed"] is None


@pytest.mark.parametrize("cid", [c["id"] for c in CASES if len(c["messages"]) > 1])
def test_multiturn_case_checks_first_answer_too(cid):
    def mutate(c):
        next(s for s in c["offline_script"] if "content" in s)["content"] = "不确定。"
    assert evaluate(cid, mutate)["status"] == "review"


@pytest.mark.parametrize("cid", ["policy_2", "policy_3", "payment_2", "payment_3"])
def test_correct_synonymous_denial_passes(cid):
    answers = {"policy_2": "该商品不支持退款。", "policy_3": "已超过退款时限，因此无法退。",
               "payment_2": "这笔付款仅授权，并未实际扣款，无法退款。",
               "payment_3": "付款失败，无法退款。"}
    assert evaluate(cid, lambda c: c["offline_script"][-1].update(content=answers[cid]))["passed"]
