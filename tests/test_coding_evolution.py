"""Offline contract tests, not evidence of model improvement."""

from dataclasses import asdict
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecoder import coding_evolution as ev
from corecoder import coding_eval as ce
from corecoder.llm import LLMResponse
from corecoder.scenarios import SkillSpec


class Model:
    model = "test-proposer"

    def __init__(self, answer):
        self.answer, self.messages = answer, []

    def chat(self, messages):
        self.messages = messages
        return LLMResponse(content=json.dumps(self.answer))


def proposal():
    return dict(root_cause="missed caller", hypothesis="inspect callers before API changes",
                applicability="public APIs", expected_benefit="fewer regressions", risks="extra reads",
                alternative_explanations="check grader first", evidence_ids=["E1"])


def frozen(cases, content="Inspect callers and run relevant regression tests."):
    evidence = ev.seal({"development_hash": ev._hash([c for c in cases if c["split"] == "development"]),
                        "evidence": [{"evidence_id": "E1", "events": [], "source": {"case_id": "SECRET"}}]})
    hypothesis = ev.hypothesize(evidence, Model(proposal()))
    approval = ev.confirm(hypothesis, "human", "Verified trace; not an environment or grader defect")
    return ev.generate(approval, Model({"content": content, "change_summary": "caller checks"}), "v1")


def source_run(tmp_path, case, status="fail", skill=None, tokens=100, repeat=0, mode="live"):
    directory = tmp_path
    directory.mkdir(parents=True)
    spec = json.loads(json.dumps(asdict(skill))) if skill else None
    fp = {"case": ev._hash(case), "strategy": spec, "strategy_hash": ev._hash(spec),
          "prompt_hash": ev._hash(ce.coding_role(skill).render()), "model": "fixed-model"}
    events = [{"seq": 0, "event": "run_started", "fingerprint": fp, "mode": mode,
               "case_definition": "HIDDEN_SENTINEL"},
              {"seq": 1, "event": "tool_finished", "tool_name": "bash", "result": "public failure"},
              {"seq": 2, "event": "run_finished", "hidden": "HIDDEN_SENTINEL"}]
    (directory / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    (directory / "patch.json").write_text("{}")
    (directory / "test-results.json").write_text('{"hidden":"HIDDEN_SENTINEL"}')
    row = {"case_id": case["id"], "repeat": repeat, "split": case["split"], "fingerprint": fp,
           "status": status, "failures": ["hidden_test_failed"] if status == "fail" else [],
           "trace": str(directory / "trace.jsonl"), "total_tokens": tokens, "tool_calls": 2,
           "model_rounds": 2, "elapsed_seconds": 1,
           "trace_sha256": ce._file_hash(directory / "trace.jsonl"),
           "patch_sha256": ce._file_hash(directory / "patch.json"),
           "tests_sha256": ce._file_hash(directory / "test-results.json")}
    (directory / "result.json").write_text(json.dumps(row))
    return row


@pytest.fixture
def cases():
    return [{"id": "dev", "split": "development", "issue": "fix caller compatibility"},
            {"id": "SECRET_HOLDOUT", "split": "holdout", "issue": "SECRET_HOLDOUT_TASK"}]


def test_evidence_excludes_holdout_and_hidden_outputs(tmp_path, cases):
    row = source_run(tmp_path / "dev", cases[0])
    report = {"mode": "live", "results": [row, {"case_id": "SECRET_HOLDOUT"}]}
    bundle = ev.collect(cases, report)
    text = json.dumps(bundle)
    assert "HIDDEN_SENTINEL" not in text and "SECRET_HOLDOUT" not in text
    assert "public failure" in text


def test_evidence_rejects_changed_artifact(tmp_path, cases):
    row = source_run(tmp_path / "dev", cases[0])
    Path(row["trace"]).write_text("tampered")
    with pytest.raises(ValueError, match="artifact differs"):
        ev.collect(cases, {"results": [row]})


def test_rescore_receipt_ignores_only_unittest_wall_time():
    original = {"status": "pass", "hidden_test": {"exit_code": 0, "output": "Ran 4 tests in 0.000s\nOK"}}
    same = copy.deepcopy(original)
    same["hidden_test"]["output"] = "Ran 4 tests in 0.002s\nOK"
    assert ev.same_rescored_row(original, same)
    same["hidden_test"]["output"] = "Ran 4 tests in 0.002s\nFAILED"
    assert not ev.same_rescored_row(original, same)
    same["hidden_test"]["output"] = "Ran 4 tests in 0.002s\nOK"
    same["status"] = "fail"
    assert not ev.same_rescored_row(original, same)


def test_no_failures_no_fake_evolution(tmp_path, cases):
    row = source_run(tmp_path / "dev", cases[0], "pass")
    with pytest.raises(ValueError, match="no eligible"):
        ev.collect(cases, {"results": [row]})


def test_proposer_cannot_see_source_paths_or_case_ids(cases):
    evidence = ev.seal({"evidence": [{"evidence_id": "E1", "source": {"case_id": "SECRET", "directory": "/secret"}}]})
    model = Model(proposal())
    ev.hypothesize(evidence, model)
    assert "SECRET" not in json.dumps(model.messages)
    assert "/secret" not in json.dumps(model.messages)


def test_proposer_receives_bounded_events_and_empty_response_is_explicit():
    evidence = ev.seal({"evidence": [{"evidence_id": "E1", "events": [
        {"seq": 1, "excerpt": "x" * 3000, "truncated": False}]}]})
    model = Model(proposal())
    ev.hypothesize(evidence, model)
    event = json.loads(model.messages[1]["content"])["evidence"][0]["events"][0]
    assert len(event["excerpt"]) == 1200 and event["truncated"]

    class EmptyModel(Model):
        def chat(self, messages):
            return LLMResponse(content="", completion_tokens=4096, usage_available=True)

    with pytest.raises(ValueError, match="empty content.*completion_tokens=4096"):
        ev.hypothesize(evidence, EmptyModel(proposal()))


def test_unknown_evidence_reference_rejected():
    model = Model(dict(proposal(), evidence_ids=["invented"]))
    with pytest.raises(ValueError, match="valid development"):
        ev.hypothesize(ev.seal({"evidence": [{"evidence_id": "E1"}]}), model)


def test_approval_and_nested_hash_required(cases):
    candidate = frozen(cases)
    assert ev.candidate_skill(candidate).compatible_roles == ("coding",)
    changed = copy.deepcopy(candidate)
    changed["payload"]["skill"]["content"] = "changed"
    with pytest.raises(ValueError, match="hash mismatch"):
        ev.candidate_skill(changed)
    approval = ev.seal({"decision": "rejected"})
    with pytest.raises(ValueError, match="confirmation"):
        ev.generate(approval, Model({}), "v2")


def test_candidate_is_actually_rendered():
    skill = SkillSpec("coding-strategy", "v1", "UNIQUE_STRATEGY", ("coding",))
    assert "UNIQUE_STRATEGY" in ce.coding_role(skill).render()
    with pytest.raises(ValueError, match="incompatible"):
        ce.coding_role(SkillSpec("x", "v1", "bad", ("orders",))).render()


def test_exclusive_output(tmp_path):
    target = tmp_path / "frozen.json"
    ev.write_new(target, {"original": True})
    with pytest.raises(FileExistsError):
        ev.write_new(target, {})
    assert ev.read(target) == {"original": True}


def experiment(tmp_path, cases, candidate_pass=True, mode="live"):
    candidate = frozen(cases)
    skill = ev.candidate_skill(candidate)
    rows = []
    for case in cases:
        for repeat in range(3):
            for arm in ("baseline", "candidate"):
                status = "fail" if case["split"] == "development" and arm == "baseline" else "pass"
                if not candidate_pass and arm == "candidate":
                    status = "fail"
                row = source_run(tmp_path / f"{case['id']}-{repeat}-{arm}", case, status,
                                 skill if arm == "candidate" else None, repeat=repeat, mode=mode)
                rows.append({"arm": arm, "result": row})
    return ev.seal({"cases": cases, "repeats": 3, "candidate": candidate, "baseline": None,
                    "runs": rows, "mode": mode, "complete": True})


def test_objective_gain_requires_human_review_not_auto_promotion(tmp_path, cases):
    bundle = experiment(tmp_path, cases)
    verdict = ev.unseal(ev.decide(bundle))
    assert verdict["verdict"] == "recommend_human_review"
    assert verdict["auto_promoted"] is False and verdict["answer_review_required"]
    approved = ev.unseal(ev.review(bundle, "human", "Reviewed all answers and evidence", True))
    assert approved["accepted"] and not approved["auto_promoted"]


def test_regression_rejected_and_cannot_be_approved(tmp_path, cases):
    bundle = experiment(tmp_path, cases, candidate_pass=False)
    assert ev.unseal(ev.decide(bundle))["verdict"] == "reject"
    with pytest.raises(ValueError, match="cannot accept"):
        ev.review(bundle, "human", "override", True)


def test_scripted_evidence_never_promotable(tmp_path, cases):
    bundle = experiment(tmp_path, cases, mode="scripted")
    assert "not_live_model_evidence" in ev.unseal(ev.decide(bundle))["blocking_reasons"]


def test_incomplete_experiment_blocked(tmp_path, cases):
    payload = ev.unseal(experiment(tmp_path, cases))
    payload["runs"].pop()
    verdict = ev.unseal(ev.decide(ev.seal(payload)))
    assert verdict["verdict"] == "insufficient_evidence" and "missing_runs" in verdict["blocking_reasons"]


def test_paired_runner_fresh_models_and_alternating_arms(tmp_path, cases, monkeypatch):
    seen, models = [], []

    def model_factory():
        model = object()
        models.append(model)
        return model

    def fake_run(case, root, model, output, **kwargs):
        seen.append((case["id"], kwargs["repeat"], kwargs["skill"] is not None))
        return {"failures": [], "error_type": None}

    monkeypatch.setattr(ev, "run_case", fake_run)
    result = ev.paired_run(cases, tmp_path, frozen(cases), tmp_path / "runs", model_factory)
    assert ev.unseal(result)["complete"] and len(models) == 12
    assert len({id(m) for m in models}) == 12
    assert seen[:4] == [("dev", 0, False), ("dev", 0, True), ("dev", 1, True), ("dev", 1, False)]


def test_interruption_preserves_manifest_and_partial_report(tmp_path, cases, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(ev, "run_case", fail)
    with pytest.raises(RuntimeError):
        ev.paired_run(cases, tmp_path, frozen(cases), tmp_path / "runs", lambda: None)
    result = ev.unseal(ev.read(tmp_path / "runs" / "experiment.json"))
    assert not result["complete"]
    assert (tmp_path / "runs" / "manifest.json").is_file()


def test_runtime_receives_skill_without_second_agent_implementation(tmp_path, monkeypatch):
    root, cases = ce.load_cases(Path("eval/coding/cases.json"))
    seen = {}
    class Agent:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
        def chat(self, task):
            return "No changes"
    monkeypatch.setattr(ce, "CodingAgent", Agent)
    monkeypatch.setattr(ce, "require_docker", lambda image: None)
    monkeypatch.setattr(ce, "image_digest", lambda image: "sha256:test")
    monkeypatch.setattr(ce, "_container_command", lambda *a, **k: {"exit_code": 0, "infra_error": False})
    skill = SkillSpec("coding-strategy", "v1", "UNIQUE_STRATEGY", ("coding",))
    row = ce.run_case(cases[0], root, SimpleNamespace(model="fake"), tmp_path / "run", mode="scripted", skill=skill)
    assert "UNIQUE_STRATEGY" in seen["system_prompt_override"]
    assert row["fingerprint"]["strategy_hash"] == ev._hash(asdict(skill))


def replace_source(entry, **changes):
    row = entry["result"]
    row.update(changes)
    directory = Path(row["trace"]).parent
    events = [json.loads(line) for line in Path(row["trace"]).read_text().splitlines()]
    events[0]["fingerprint"] = row["fingerprint"]
    Path(row["trace"]).write_text("\n".join(json.dumps(e) for e in events))
    row["trace_sha256"] = ce._file_hash(row["trace"])
    (directory / "result.json").write_text(json.dumps(row))


def test_model_change_cannot_be_attributed_to_skill(tmp_path, cases):
    payload = ev.unseal(experiment(tmp_path, cases))
    entry = payload["runs"][1]
    replace_source(entry, fingerprint={**entry["result"]["fingerprint"], "model": "stronger-model"})
    verdict = ev.unseal(ev.decide(ev.seal(payload)))
    assert verdict["verdict"] == "insufficient_evidence"
    assert "non_strategy_fingerprint_changed" in verdict["blocking_reasons"]


def test_missing_usage_blocks_claim(tmp_path, cases):
    payload = ev.unseal(experiment(tmp_path, cases))
    replace_source(payload["runs"][0], total_tokens=None)
    assert "missing_usage" in ev.unseal(ev.decide(ev.seal(payload)))["blocking_reasons"]


def test_no_gain_rejected(tmp_path, cases):
    payload = ev.unseal(experiment(tmp_path, cases))
    for entry in payload["runs"]:
        replace_source(entry, status="pass", failures=[])
    assert ev.unseal(ev.decide(ev.seal(payload)))["verdict"] == "reject"


def test_duplicate_run_blocked(tmp_path, cases):
    payload = ev.unseal(experiment(tmp_path, cases))
    payload["runs"].append(payload["runs"][0])
    assert "duplicate_or_unknown_run" in ev.unseal(ev.decide(ev.seal(payload)))["blocking_reasons"]


def test_edited_scorecard_cannot_override_original(tmp_path, cases):
    row = source_run(tmp_path / "dev", cases[0])
    row["failures"] = ["invented"]
    with pytest.raises(ValueError, match="original result"):
        ev.collect(cases, {"results": [row]})


def test_changed_development_set_blocked_before_model_call(tmp_path, cases):
    candidate = frozen(cases)
    changed = copy.deepcopy(cases)
    changed[0]["issue"] = "different"
    with pytest.raises(ValueError, match="development set"):
        ev.paired_run(changed, tmp_path, candidate, tmp_path / "run", lambda: pytest.fail("no model calls"))


@pytest.mark.skipif(os.getenv("RUN_DOCKER_CODING_TESTS") != "1", reason="requires Docker daemon")
def test_real_runtime_container_and_skill_snapshot(tmp_path):
    from corecoder.demo import ScriptedLLM
    from corecoder.llm import ToolCall

    class InspectModel(ScriptedLLM):
        def chat(self, messages, **kwargs):
            assert "CHECK_CALLERS_MARKER" in messages[0]["content"]
            return super().chat(messages, **kwargs)

    root, cases = ce.load_cases(Path("eval/coding/cases.json"))
    model = InspectModel([
        LLMResponse(tool_calls=[ToolCall("fix", "write_file", {
            "file_path": "/workspace/target.py",
            "content": "def average(values):\n    return sum(values) / len(values) if values else 0.0\n"})]),
        LLMResponse(tool_calls=[ToolCall("test", "bash", {"command": "python -B test_public.py"})]),
        LLMResponse(content="Public test passed.")])
    skill = SkillSpec("coding-strategy", "integration-v1", "CHECK_CALLERS_MARKER", ("coding",))
    row = ce.run_case(cases[0], root, model, tmp_path / "run", mode="scripted", skill=skill)
    assert row["status"] == "pass" and row["agent_ran_public_test"]
    assert ce.recover_coding_result(tmp_path / "run", row["fingerprint"]) == row
