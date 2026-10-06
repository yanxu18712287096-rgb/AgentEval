"""Contract fixtures only: no new application scenario or deployed Skill."""
import copy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from corecoder.evaluation import (default_registry, run_case, scripted_model, score,
                                 validate_cases, rescore_trace, comparison_reasons)
from corecoder.scenarios import CaseSpec, RoleSpec, ScenarioSpec, ScenarioRegistry, SkillSpec

CASES = json.loads(Path("eval/cases/examples.json").read_text())


def test_case_copy_and_legacy_adapter():
    original = copy.deepcopy(CASES[0])
    spec = CaseSpec.from_dict(original)
    original["messages"][0]["content"] = "changed"
    assert spec.data["messages"][0]["content"] != "changed"
    assert spec.data["scenario"] == "orders"


def test_unknown_and_duplicate_scenario_rejected():
    registry = default_registry()
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(registry.resolve(CASES[0]))
    with pytest.raises(ValueError, match="unknown"):
        registry.resolve({**CASES[0], "scenario": "missing"})


def test_role_allowlist_and_skill_compatibility():
    scenario = default_registry().resolve(CASES[0])
    assert len(scenario.role.skills) == 1
    assert scenario.role.skills[0].id == "order-refund"
    assert "未发货订单不受退款截止时间限制" in scenario.role.render()
    with pytest.raises(ValueError, match="allowlist"):
        replace(scenario, role=replace(scenario.role, allowed_tools=())).create({})
    skill = SkillSpec("contract-test", "1", "test only", ("another-role",))
    with pytest.raises(ValueError, match="incompatible"):
        replace(scenario.role, skills=(skill,)).render()


def test_state_and_tool_instances_are_per_run():
    scenario = default_registry().resolve(CASES[0])
    a, at = scenario.create(CASES[0]["fixture"])
    b, bt = scenario.create(CASES[0]["fixture"])
    assert a is not b and all(x is not y for x, y in zip(at, bt))
    a.orders.clear()
    assert b.orders == CASES[0]["fixture"]["orders"]


def test_generic_contract_has_no_refund_requirement():
    case = {"id": "contract", "scenario": "contract", "scenario_version": "1",
            "split": "development", "fixture": {}, "expect": {},
            "messages": [{"role": "user", "content": "hello"}],
            "route": {"max_model_rounds": 1, "max_tool_calls": 0},
            "offline_script": [{"content": "hello"}]}
    registry = ScenarioRegistry()
    registry.register(ScenarioSpec("contract", "1", RoleSpec("contract", "1", "reply", ()),
                                   dict, lambda state: [], lambda case: None,
                                   lambda case, events, state: {"failures": []},
                                   dict, dict, "1"))
    validate_cases([case], registry)
    result, _ = run_case(case, scripted_model(case), registry=registry)
    assert result["passed"] and result["optimal_route"]
    assert "refund_count" not in result


def test_rescore_never_executes_model_or_tools(tmp_path):
    path = tmp_path / "trace.jsonl"
    original, _ = run_case(CASES[0], scripted_model(CASES[0]), path)
    before = path.read_bytes()
    with patch("corecoder.evaluation.Agent", side_effect=AssertionError("must not run")), \
         patch("corecoder.orders.OrderStore.execute", side_effect=AssertionError("must not run")):
        rescored = rescore_trace(path)
    assert rescored["result"]["passed"] == original["passed"]
    assert rescored["historical_evidence_only"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["partial", "sequence", "hash"])
def test_rescore_rejects_invalid_evidence(tmp_path, mutation):
    path = tmp_path / "trace.jsonl"
    _, trace = run_case(CASES[0], scripted_model(CASES[0]))
    events = copy.deepcopy(trace.events)
    if mutation == "partial":
        events.pop()
    elif mutation == "sequence":
        events[1]["seq"] = 99
    else:
        events[0]["case_hash"] = "bad"
    path.write_text("\n".join(json.dumps(e) for e in events))
    with pytest.raises(ValueError):
        rescore_trace(path)


def test_old_trace_rescore_is_explicitly_historical():
    path = Path("eval/runs/v0.9.4-scripted-verified/000-00.jsonl")
    if not path.exists():
        pytest.skip("optional historical artifact")
    result = rescore_trace(path)
    assert result["source_schema_version"] == 3
    assert result["scoring_schema_version"] == 5
    assert result["source_fingerprint"] is None


def test_fingerprint_changes_prevent_direct_comparison():
    row, _ = run_case(CASES[0], scripted_model(CASES[0]))
    changed = copy.deepcopy(row)
    changed["fingerprint"]["scenario"]["role"]["version"] = "changed"
    assert comparison_reasons(row, changed) == ["scenario_changed"]
    assert comparison_reasons(row, row) == []


def test_missing_tool_result_fails_execution_dimension():
    case = next(c for c in CASES if c["id"] == "refund_success_1")
    _, trace = run_case(case, scripted_model(case))
    events = [e for e in trace.events if e["event"] != "tool_finished"]
    scenario = default_registry().resolve(case)
    result = score(case, events, scenario.restore(trace.events[-1]["state"]))
    assert not result["passed"]
    assert "tool_result_pairing" in result["dimensions"]["execution"]["failures"]


@pytest.mark.parametrize("field,value", [("tool_name", "wrong"), ("tool_call_id", "wrong"), ("seq", -1)])
def test_mispaired_result_is_rejected(field, value):
    case = next(c for c in CASES if c["id"] == "refund_success_1")
    _, trace = run_case(case, scripted_model(case))
    events = copy.deepcopy(trace.events)
    next(e for e in events if e["event"] == "tool_finished")[field] = value
    scenario = default_registry().resolve(case)
    result = score(case, events, scenario.restore(trace.events[-1]["state"]))
    assert not result["passed"]
    assert "tool_result_identity_or_order" in result["failures"]


def test_registered_scorer_can_reject_business_outcome():
    scenario = default_registry().resolve(CASES[0])
    scenario = replace(scenario, scorer=lambda *args: {"failures": ["business_rejected"]})
    registry = ScenarioRegistry()
    registry.register(scenario)
    result, _ = run_case(CASES[0], scripted_model(CASES[0]), registry=registry)
    assert not result["passed"] and not result["optimal_route"]
    assert result["dimensions"]["business"]["failures"] == ["business_rejected"]
