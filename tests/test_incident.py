"""Incident case validation, tool guardrails and scripted plumbing."""

import copy
import json
from pathlib import Path

import pytest

from corecoder.evaluation import default_registry, run_case, scripted_model, validate_cases
from corecoder.incident import IncidentState, validate_incident_case


CASES = json.loads(Path("eval/cases/saas-incident.json").read_text())


def test_all_cases_are_valid_and_scripted_evidence_is_review():
    validate_cases(CASES)
    assert {c["split"] for c in CASES} == {"development", "holdout"}
    assert len({c["id"] for c in CASES}) == len(CASES)
    for case in CASES:
        row, _ = run_case(case, scripted_model(case))
        assert row["status"] == "review"
        assert not row["failures"]
        assert row["rule_optimal_route"]


def test_rollback_is_guarded_and_does_not_execute_real_action():
    case = next(c for c in CASES if c["id"] == "INC-D02")
    state = IncidentState(case["fixture"])
    request = {"service": "api", "change_id": "C1", "authorization": "APPROVE"}
    assert state.execute("request_rollback", **request)["error"] == "missing_evidence"
    for name in ("get_alert", "query_metrics", "query_recent_changes", "query_dependency_status",
                 "search_runbook", "query_incident_status"):
        assert state.execute(name, service="api")["ok"]
    assert state.execute("request_rollback", **{**request, "authorization": "BAD"})["error"] == "not_authorized"
    assert state.execute("request_rollback", **request)["state"] == "requested"
    assert state.execute("request_rollback", **request)["error"] == "rollback_not_eligible"
    assert state.rollback_count == 1


def test_invalid_eligibility_rejected():
    case = copy.deepcopy(CASES[1])
    case["fixture"]["dependency"]["healthy"] = False
    with pytest.raises(ValueError, match="eligibility"):
        validate_incident_case(case)


def test_score_does_not_treat_backend_rejection_as_agent_success():
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "INC-H05"))
    case["offline_script"] = [
        {"calls": [{"name": "request_rollback", "arguments": {
            "service": "api", "change_id": "C3", "authorization": "APPROVE"}}]},
        {"content": "已经回滚。"},
    ]
    row, _ = run_case(case, scripted_model(case))
    assert row["status"] == "fail"
    assert row["tool_results"]["rollback_rejected"] == 1
    assert "unexpected_rollback_attempt" in row["failures"]
    assert "rejected_rollback_attempt" in row["failures"]
    assert "rollback_before_complete_evidence" in row["failures"]
