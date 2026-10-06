"""Behavioral checks for the independent, read-only campus scenario."""

import copy
import json
from pathlib import Path

from corecoder.campus_support import CampusSupportState
from corecoder.evaluation import default_registry, run_case, scripted_model, validate_cases, rescore_trace, score


CASES = json.loads((Path(__file__).resolve().parent.parent / "eval/cases/campus-support.json").read_text(encoding="utf-8"))
CHALLENGE_CASES = json.loads((Path(__file__).resolve().parent.parent / "eval/cases/campus-support-challenge.json").read_text(encoding="utf-8"))


def test_corpus_and_notice_visibility_across_publication_boundary():
    state = CampusSupportState({})
    assert len(state.help) == 5
    assert len(state.notices) == 3
    state.as_of = "2026-09-28T09:42:00+08:00"
    current = state.execute("search_service_notices", {"service": "upload"})
    assert current["items"][0]["evidence_id"].startswith("NOTICE-UPLOAD-001/U1@")
    assert len(current["items"][0]["updates"]) == 1
    state.as_of = "2026-09-28T10:00:00+08:00"
    assert state.execute("search_service_notices", {"service": "upload"})["items"] == []
    past = state.execute("search_service_notices", {"service": "upload", "occurred_at": "2026-09-28T09:15:00+08:00"})
    assert past["items"][0]["evidence_id"].startswith("NOTICE-UPLOAD-001/U2@")
    assert len(past["items"][0]["updates"]) == 2
    assert state.execute("search_service_notices", {"service": "upload", "occurred_at": "2026-09-28T10:01:00+08:00"})["error"]["code"] == "FUTURE_TIME"


def test_timeout_is_not_empty_and_fixtures_are_independent():
    first = CampusSupportState({"faults": {"search_service_notices": "TIMEOUT"}})
    second = CampusSupportState({})
    for state in (first, second):
        state.as_of = "2026-09-28T09:20:00+08:00"
    timeout = first.execute("search_service_notices", {"service": "upload"})
    assert timeout["ok"] is False and "items" not in timeout
    assert second.execute("search_service_notices", {"service": "upload"})["items"]
    assert first.execute("search_service_notices", {"service": "bad"})["error"]["code"] == "INVALID_ARGUMENT"
    assert first.execute("search_help", {"query": "PDF"})["ok"] is True


def test_all_twelve_cases_execute_and_semantics_remain_review(tmp_path):
    validate_cases(CASES)
    for case in CASES:
        path = tmp_path / f"{case['id']}.jsonl"
        result, trace = run_case(case, scripted_model(case), path)
        assert result["status"] == "review", (case["id"], result["failures"])
        assert not result["failures"]
        assert len([e for e in trace.events if e["event"] == "turn_finished"]) == len(case["messages"])
        assert rescore_trace(path)["result"]["status"] == "review"


def test_wrong_notice_query_and_missing_evidence_fail_hard():
    case = next(c for c in CASES if c["id"] == "CS-D08")
    _, trace = run_case(case, scripted_model(case))
    wrong = copy.deepcopy(trace.events)
    for event in wrong:
        if event["event"] == "tool_requested":
            event["arguments"] = {"service": "login"}
        if event["event"] == "tool_finished":
            payload = json.loads(event["result"])
            payload["items"] = []
            event["result"] = json.dumps(payload)
    scenario = default_registry().resolve(case)
    bad = score(case, wrong, scenario.restore(trace.events[-1]["state"]), scenario)
    assert bad["status"] == "fail"
    assert "T1:missing_expected_notice_query" in bad["failures"]
    assert "T1:missing_evidence:NOTICE-UPLOAD-001/U2" in bad["failures"]


def test_empty_notice_and_timeout_have_distinct_expected_outcomes():
    case = next(c for c in CASES if c["id"] == "CS-D12")
    _, trace = run_case(case, scripted_model(case))
    altered = copy.deepcopy(trace.events)
    for event in altered:
        if event["event"] == "tool_finished":
            event["result"] = json.dumps({"ok": False, "as_of": case["messages"][0]["as_of"],
                                          "error": {"code": "TIMEOUT", "retryable": True}})
    scenario = default_registry().resolve(case)
    bad = score(case, altered, scenario.restore(trace.events[-1]["state"]), scenario)
    assert "T1:missing_expected_notice_outcome" in bad["failures"]


def test_challenge_cases_and_timeout_then_success(tmp_path):
    validate_cases(CHALLENGE_CASES)
    for case in CHALLENGE_CASES:
        result, _ = run_case(case, scripted_model(case), tmp_path / f"{case['id']}.jsonl")
        assert result["status"] == "review", (case["id"], result["failures"])
        assert not result["failures"]
    case = next(c for c in CHALLENGE_CASES if c["id"] == "CS-H06")
    _, trace = run_case(case, scripted_model(case))
    ends = [e for e in trace.events if e["event"] == "tool_finished"
            and e["tool_name"] == "search_service_notices"]
    assert [json.loads(e["result"])["ok"] for e in ends] == [False, True]
    assert ends[0]["seq"] < ends[1]["seq"]


def test_timeout_then_success_must_be_causal_not_parallel():
    case = next(c for c in CHALLENGE_CASES if c["id"] == "CS-H06")
    _, trace = run_case(case, scripted_model(case))
    altered = copy.deepcopy(trace.events)
    notices = [e for e in altered if e["event"] == "tool_requested"
               and e["tool_name"] == "search_service_notices"]
    ends = {e["call_key"]: e for e in altered if e["event"] == "tool_finished"}
    notices[1]["seq"] = ends[notices[0]["call_key"]]["seq"] - 1
    scenario = default_registry().resolve(case)
    bad = score(case, altered, scenario.restore(trace.events[-1]["state"]), scenario)
    assert "T1:missing_timeout_then_match" in bad["failures"]


def test_timeout_retry_accepts_historical_incident_time():
    case = copy.deepcopy(next(c for c in CHALLENGE_CASES if c["id"] == "CS-H06"))
    for step in case["offline_script"]:
        for call in step.get("calls", []):
            if call["name"] == "search_service_notices":
                call["arguments"]["occurred_at"] = "2026-09-28T14:02:00+08:00"
    validate_cases([case])
    result, _ = run_case(case, scripted_model(case))
    assert result["status"] == "review"
    assert result["failures"] == []
