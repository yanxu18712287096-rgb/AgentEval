"""Compression must preserve protocol pairing and decision evidence."""

import copy
import json

from corecoder.agent import Agent
from corecoder.context import ContextManager, tool_groups
from corecoder.demo import ScriptedLLM
from corecoder.trace import TraceRecorder


def _read_group(call_id, order_id="A100", receipt="receipt-123"):
    result = json.dumps({"ok": True, "order_id": order_id, "status": "pending",
                         "query_receipt": receipt}, ensure_ascii=False, indent=2200)
    return [{"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "function": {"name": "query_order", "arguments": '{"order_id":"A100"}'}}]},
        {"role": "tool", "tool_call_id": call_id, "content": result}]


def test_parallel_group_is_atomic_and_keeps_exact_evidence():
    ctx = ContextManager(max_tokens=250)
    messages = [{"role": "user", "content": "refund A100"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "a", "function": {"name": "query_order"}},
                    {"id": "b", "function": {"name": "query_payment"}}]},
                {"role": "tool", "tool_call_id": "a", "content": _read_group("a")[1]["content"]},
                {"role": "tool", "tool_call_id": "b", "content": json.dumps({"payment": "captured"}, indent=2200)},
                {"role": "user", "content": "and what next?"},
                {"role": "assistant", "content": "checking"},
                {"role": "user", "content": "continue"},
                {"role": "assistant", "content": "okay"},
                {"role": "user", "content": "continue"},
                {"role": "assistant", "content": "okay"}]
    assert ctx.maybe_compress(messages)
    assert tool_groups(messages) is not None
    full = json.dumps(messages, ensure_ascii=False)
    assert "receipt-123" in full
    assert "captured" in full
    assert ctx.last_event["after_tokens"] < ctx.last_event["before_tokens"]
    assert len(ctx.original_groups) == 1


def test_invalid_or_partial_tool_pairs_are_never_modified():
    ctx = ContextManager(max_tokens=100)
    for messages in ([{"role": "tool", "tool_call_id": "orphan", "content": "x" * 3000}],
                     [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
                      {"role": "tool", "tool_call_id": "a", "content": "x" * 3000}],
                     [{"role": "assistant", "tool_calls": [{"id": "a"}]},
                      {"role": "tool", "tool_call_id": "wrong", "content": "x" * 3000}]):
        original = copy.deepcopy(messages)
        assert not ctx.maybe_compress(messages)
        assert messages == original
        assert ctx.last_event["status"] == "skipped_invalid_tool_pairing"


def test_write_result_survives_summary_and_trace_records_compression():
    trace = TraceRecorder()
    agent = Agent(llm=ScriptedLLM([]), tools=[], max_context_tokens=350, trace=trace)
    agent.messages = [
        {"role": "user", "content": "Refund A100. " + "x" * 800},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "w", "function": {"name": "refund_order"}}]},
        {"role": "tool", "tool_call_id": "w", "content": '{"ok":true,"refund_status":"completed"}'},
    ]
    for index in range(12):
        agent.messages.extend([{"role": "user", "content": f"followup {index} " + "y" * 80},
                               {"role": "assistant", "content": "ack"}])
    assert agent._compress_context()
    assert tool_groups(agent.messages) is not None
    assert "refund_status" in json.dumps(agent.messages)
    events = [event for event in trace.events if event["event"] == "context_compression"]
    assert events and events[-1]["status"] == "compressed"


def test_long_unstructured_tool_result_is_not_blindly_snipped():
    ctx = ContextManager(max_tokens=200)
    messages = [{"role": "assistant", "content": None,
                 "tool_calls": [{"id": "x", "function": {"name": "read_file"}}]},
                {"role": "tool", "tool_call_id": "x", "content": "critical text " * 300},
                {"role": "user", "content": "next"}, {"role": "assistant", "content": "okay"},
                {"role": "user", "content": "next"}, {"role": "assistant", "content": "okay"}]
    assert not ctx._snip_tool_outputs(messages)
    assert "critical text" in messages[1]["content"]


def test_key_fact_validator_rolls_back_a_bad_summary(monkeypatch):
    ctx = ContextManager(max_tokens=300)
    messages = [{"role": "user", "content": "Refund A100 " + "x" * 900},
                {"role": "assistant", "tool_calls": [{"id": "w", "function": {"name": "refund_order"}}]},
                {"role": "tool", "tool_call_id": "w",
                 "content": '{"ok":true,"order_id":"A100","refund_status":"completed"}'}]
    for number in range(12):
        messages.extend([{"role": "user", "content": f"followup {number} " + "y" * 70},
                         {"role": "assistant", "content": "okay"}])
    original = copy.deepcopy(messages)
    monkeypatch.setattr(ctx, "_evidence_ledger", lambda old: "(broken ledger)")
    assert not ctx.maybe_compress(messages)
    assert messages == original
    assert ctx.last_event["status"] == "skipped_no_safe_reduction"
