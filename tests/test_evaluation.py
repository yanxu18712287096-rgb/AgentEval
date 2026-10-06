import concurrent.futures
import copy
import json
from pathlib import Path

import pytest

from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.evaluation import (aggregate, compare_reports, recover_run, run_case, score,
                                  scripted_model, validate_cases)
from corecoder.llm import LLMResponse, ToolCall
from corecoder.orders import OrderStore, order_tools
from corecoder.permissions import Permission
from corecoder.trace import TraceRecorder


CASES = json.loads((Path(__file__).parents[1] / "eval/cases/examples.json").read_text())
BY_ID = {case["id"]: case for case in CASES}


def test_all_38_offline_cases_are_plumbing_checks_only():
    assert len(CASES) == 38
    assert all(c["split"] == "development" for c in CASES)
    validate_cases(CASES)
    rows = []
    for case in CASES:
        result, trace = run_case(case, scripted_model(case))
        assert result["passed"] and result["optimal_route"], (case["id"], result)
        assert result["total_tokens"] is None and not result["usage_available"]
        assert trace.events[-1]["event"] == "run_finished"
        rows.append(result)
    assert aggregate(rows)["optimal_route_runs"] == 38


@pytest.mark.parametrize("cid", ["pending_unpaid", "policy_2", "policy_3"])
def test_negative_business_answers_are_not_negated_topic_labels(cid):
    result, _ = run_case(BY_ID[cid], scripted_model(BY_ID[cid]))
    assert result["passed"], result["failures"]


def test_policy_date_is_visible_to_model():
    case = BY_ID["policy_3"]
    _, trace = run_case(case, scripted_model(case))
    requests = [e for e in trace.events if e["event"] == "model_requested"]
    assert any('as_of_date' in json.dumps(e["messages"]) and
               case["fixture"]["today"] in json.dumps(e["messages"]) for e in requests)


def test_status_before_timeout_is_not_confirmation():
    case = copy.deepcopy(BY_ID["uncertain_refund_1"])
    case["offline_script"][2], case["offline_script"][3] = case["offline_script"][3], case["offline_script"][2]
    result, _ = run_case(case, scripted_model(case))
    assert not result["passed"] and not result["optimal_route"]
    assert "unresolved_refund_outcome" in result["failures"]


def test_later_refund_does_not_justify_earlier_success_claim():
    case = copy.deepcopy(BY_ID["intent_switch_refund"])
    case["offline_script"][1]["content"] = "退款已完成。"
    result, _ = run_case(case, scripted_model(case))
    assert not result["passed"]
    assert any(f.startswith("false_refund_success_claim:") for f in result["failures"])


def test_refund_before_user_authorization_is_rejected():
    case = copy.deepcopy(BY_ID["intent_switch_refund"])
    steps = case["offline_script"]
    case["offline_script"] = [steps[0], steps[2], steps[3], steps[4], steps[5],
                              {"content": "退款已完成。"}]
    result, _ = run_case(case, scripted_model(case))
    assert not result["passed"] and not result["optimal_route"]
    assert "forbidden_turn_tool:1:refund_order" in result["failures"]
    assert "missing_turn_tool:2:refund_order" in result["failures"]


@pytest.mark.parametrize("rules", [[], [{}], [{"must_call": ["unknown"]}, {}],
                                  [{"must_call": ["refund_order"], "must_not_call": ["refund_order"]}, {}]])
def test_invalid_turn_assertions_are_rejected(rules):
    case = copy.deepcopy(BY_ID["intent_switch_refund"])
    case["expect"]["turns"] = rules
    with pytest.raises(ValueError):
        validate_cases([case])


def test_duplicate_refund_can_reuse_confirmed_result():
    case = copy.deepcopy(BY_ID["duplicate_refund"])
    del case["offline_script"][4]
    result, _ = run_case(case, scripted_model(case))
    assert result["passed"] and result["optimal_route"], result["failures"]


@pytest.mark.parametrize("cid", ["intent_change_1"])
def test_eligibility_check_includes_payment_within_budget(cid):
    case = BY_ID[cid]
    result, trace = run_case(case, scripted_model(case))
    first_answer = next(e["seq"] for e in trace.events if e["event"] == "turn_finished")
    assert any(e["event"] == "tool_finished" and e["tool_name"] == "query_payment"
               and e["seq"] < first_answer for e in trace.events)
    assert result["passed"] and result["optimal_route"]


def test_refund_requires_valid_order_receipt_and_atomic_state():
    fixture = BY_ID["refund_success_1"]["fixture"]
    original = copy.deepcopy(fixture)
    store = OrderStore(fixture)
    assert store.execute("refund_order", order_id="A1002")["error"] == "missing_or_invalid_query_receipt"
    receipt = store.execute("query_order", order_id="A1002")["query_receipt"]
    assert store.execute("refund_order", order_id="A1002", query_receipt="wrong")["error"] == "missing_or_invalid_query_receipt"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.execute("refund_order", order_id="A1002", query_receipt=receipt), range(20)))
    assert sum(r["ok"] for r in results) == store.refund_count == 1
    assert fixture == original


def test_same_round_query_refund_is_rejected_and_not_optimal():
    case = BY_ID["refund_success_1"]
    llm = ScriptedLLM([LLMResponse(tool_calls=[
        ToolCall("q", "query_order", {"order_id": "A1002"}),
        ToolCall("r", "refund_order", {"order_id": "A1002", "query_receipt": "guessed"})]),
        LLMResponse(content="退款已完成。")])
    result, _ = run_case(case, llm)
    assert not result["passed"] and not result["optimal_route"]
    assert result["tool_results"]["policy_rejected"] == 1
    assert result["refund_count"] == 0


@pytest.mark.parametrize("change,error", [
    ({"payments": {"A1002": "authorized"}}, "payment_not_captured"),
    ({"policies": {"A1002": {"allow_refund": False}}}, "policy_disallows_refund"),
    ({"policies": {"A1002": {"allow_refund": True, "eligible_until": "2026-09-27"}}}, "policy_expired"),
    ({"orders": {"A1002": {"status": "in_transit"}}}, "not_eligible"),
])
def test_backend_policy_rejection(change, error):
    fixture = copy.deepcopy(BY_ID["refund_success_1"]["fixture"])
    fixture.update(change)
    store = OrderStore(fixture)
    receipt = store.execute("query_order", order_id="A1002")["query_receipt"]
    assert store.execute("refund_order", order_id="A1002", query_receipt=receipt)["error"] == error
    assert store.refund_count == 0


def test_receipt_cannot_be_used_for_other_order_and_identity_isolation():
    fixture = copy.deepcopy(BY_ID["refund_success_1"]["fixture"])
    fixture["orders"]["B2001"] = {"status": "delivered", "user_id": "U1"}
    fixture["payments"]["B2001"] = "captured"
    fixture["policies"]["B2001"] = {"allow_refund": True, "eligible_until": "2026-10-01"}
    fixture["orders"]["C3001"] = {"status": "delivered", "user_id": "U2"}
    store = OrderStore(fixture)
    receipt = store.execute("query_order", order_id="A1002")["query_receipt"]
    assert store.execute("refund_order", order_id="B2001", query_receipt=receipt)["error"] == "missing_or_invalid_query_receipt"
    assert store.execute("query_order", order_id="C3001")["error"] == "not_authorized"
    assert "C3001" not in [x["order_id"] for x in store.execute("list_user_orders")["orders"]]


def test_timeout_after_commit_requires_status_lookup_not_duplicate_refund():
    case = BY_ID["uncertain_refund_1"]
    result, trace = run_case(case, scripted_model(case))
    assert result["refund_count"] == 1 and result["tool_calls"] == 5
    assert result["tool_results"]["transient_failure"] == 1
    assert any(e["tool_name"] == "query_refund_status" for e in trace.events if e["event"] == "tool_requested")


def test_live_usage_present_vs_missing_is_not_zero():
    class Metered(ScriptedLLM):
        def __init__(self, available):
            super().__init__([LLMResponse(content="请提供订单号。", prompt_tokens=7,
                                          completion_tokens=3, usage_available=available)])
            self.available = available
            self.missing_usage_calls = 0

        def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
            response = super().chat(messages, tools, on_token, on_reasoning)
            self.total_prompt_tokens += response.prompt_tokens
            self.total_completion_tokens += response.completion_tokens - len(response.content.split())
            if not self.available:
                self.missing_usage_calls += 1
            return response

    case = BY_ID["clarify_1"]
    present, _ = run_case(case, Metered(True), mode="live")
    missing, _ = run_case(case, Metered(False), mode="live")
    assert present["total_tokens"] == 10 and present["usage_available"]
    assert missing["total_tokens"] is None and not missing["usage_available"]


def test_blocked_and_bad_arguments_have_terminal_evidence():
    for permission, args, status in [(Permission(), {"order_id": "A1002"}, "blocked"),
                                     (None, {"wrong": "A1002"}, "failed")]:
        trace = TraceRecorder()
        agent = Agent(ScriptedLLM([LLMResponse(tool_calls=[ToolCall("c", "query_order", args)]),
                                   LLMResponse(content="done")]), trace=trace,
                      permission=permission, tools=order_tools(OrderStore({})))
        agent.chat("hi")
        ends = [e for e in trace.events if e["event"] == "tool_finished"]
        assert len(ends) == 1 and ends[0]["status"] == status


def test_trace_replay_and_copy_survive_history_mutation(tmp_path):
    case = BY_ID["duplicate_refund"]
    _, trace = run_case(case, scripted_model(case), tmp_path / "trace.jsonl")
    extracted = json.loads((tmp_path / "trace.jsonl").read_text().splitlines()[0])["case_definition"]
    assert extracted == case and run_case(extracted, scripted_model(extracted))[0]["passed"]
    data = {"messages": [{"content": "original"}]}
    trace.record("copy_test", **data)
    data["messages"][0]["content"] = "changed"
    assert trace.events[-1]["messages"][0]["content"] == "original"


def test_resume_recovers_completed_trace_and_archives_partial(tmp_path):
    case = BY_ID["clarify_1"]
    path = tmp_path / "000-00.jsonl"
    original, _ = run_case(case, scripted_model(case), path, mode="live")
    recovered = recover_run(case, path, 0)
    assert recovered["passed"] == original["passed"]
    assert recovered["recovered_from_trace"]
    with pytest.raises(ValueError):
        recover_run(case, path, 0, model="different-model")
    partial = tmp_path / "000-01.jsonl"
    first_line = path.read_text().splitlines()[0]
    event = json.loads(first_line)
    event["repeat"] = 1
    partial.write_text(json.dumps(event) + "\n")
    assert recover_run(case, partial, 1) is None
    assert not partial.exists() and (tmp_path / "000-01.interrupted.jsonl").exists()


def test_interruption_has_one_terminal_result():
    from corecoder.tools.base import Tool

    class Interrupt(Tool):
        name = "interrupt"
        description = "interrupt"
        parameters = {}

        def execute(self):
            raise KeyboardInterrupt()

    trace = TraceRecorder()
    agent = Agent(ScriptedLLM([LLMResponse(tool_calls=[ToolCall("c", "interrupt", {})])]),
                  tools=[Interrupt()], trace=trace)
    with pytest.raises(KeyboardInterrupt):
        agent.chat("go")
    ends = [e for e in trace.events if e["event"] == "tool_finished"]
    assert len(ends) == 1 and ends[0]["status"] == "interrupted"
    assert agent.messages[-1]["tool_call_id"] == "c"


def test_runtime_unknown_tool_and_missing_pairing_fail():
    case = BY_ID["clarify_1"]
    assert "runtime_error" in run_case(case, ScriptedLLM([]))[0]["failures"]
    llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall("x", "made_up", {})]),
                       LLMResponse(content="请提供订单号")])
    result, trace = run_case(case, llm)
    assert "tool_execution_failure" in result["failures"]
    events = [e for e in trace.events if e["event"] != "tool_finished"]
    assert "tool_result_pairing" in score(case, events, OrderStore({}))["failures"]


def test_validation_and_comparison_are_version_aware():
    with pytest.raises(ValueError):
        validate_cases(CASES + [CASES[0]])
    with pytest.raises(ValueError):
        compare_reports({"mode": "live"}, {"mode": "scripted"})
    old = {"mode": "live", "results": [{"case_id": "a", "case_hash": "x", "passed": True,
                                        "optimal_route": True, "failures": []}]}
    new = {"mode": "live", "results": [{"case_id": "a", "case_hash": "x", "passed": False,
                                        "optimal_route": False, "failures": ["bug"]}]}
    assert compare_reports(new, old)["changes"][0]["reasons"] == ["missing_fingerprint"]
    old["results"][0]["fingerprint"] = {"schema_version": 4}
    new["results"][0]["fingerprint"] = {"schema_version": 4}
    assert compare_reports(new, old)["changes"][0]["before_optimal"] is True
    new["results"][0]["case_hash"] = "new"
    assert compare_reports(new, old)["changes"][0]["change"] == "not_comparable"
