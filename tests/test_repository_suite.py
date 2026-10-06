"""Requirement traceability and mutation checks, not LLM accuracy measurements."""

import copy
import json

import pytest

from scripts.repository_suite import ROOT, audit, build, repaired, tasks
from corecoder.coding_eval import load_cases


def test_repository_task_contracts_and_independent_splits():
    rows = tasks()
    assert len(rows) == 6
    assert sum(t[0]["split"] == "development" for t in rows) == 3
    assert sum(t[0]["split"] == "holdout" for t in rows) == 3
    assert all(len(fixture) >= 5 and len(reference) >= 2 for _, fixture, reference, _ in rows)
    dev = {text for task, files, _, _ in rows if task["split"] == "development" for name, text in files.items()
           if name.endswith(".py") and not name.endswith("__init__.py")}
    holdout = {text for task, files, _, _ in rows if task["split"] == "holdout" for name, text in files.items()
               if name.endswith(".py") and not name.endswith("__init__.py")}
    assert not dev & holdout


def test_original_defects_reference_fixes_and_incomplete_repairs():
    report = audit()
    assert report["passed"], json.dumps(report, ensure_ascii=False, indent=2)
    assert report["model_calls"] == 0
    assert sum(len(row["partial_repairs"]) for row in report["results"]) == 12


def test_reference_replacements_fail_closed():
    _, fixture, reference, _ = tasks()[0]
    corrupted = copy.deepcopy(reference)
    name = next(iter(corrupted))
    corrupted[name][0][0] = "NOT_IN_SOURCE"
    with pytest.raises(ValueError, match="exactly once"):
        repaired(fixture, corrupted)


def test_generated_agent_workspace_excludes_answers(tmp_path):
    if not (ROOT / "suite-lock.json").exists():
        pytest.skip("initial authoring before first freeze")
    exported = tmp_path / "export"
    build(exported, "development")
    root, cases = load_cases(exported / "cases.json")
    assert len(cases) == 3 and all(c["split"] == "development" for c in cases)
    for case in cases:
        assert (root / case["hidden_test"]).exists()
        assert not any("reference" in name or "verify" in name or "verifier" in name for name in case["fixture"])
        assert "TASK.md" in case["fixture"]
        assert all("REPO-H" not in text for text in case["fixture"].values())


def test_export_refuses_frozen_source_drift(tmp_path):
    from scripts.repository_suite import source_files, digest
    (tmp_path / "development").mkdir()
    (tmp_path / "holdout").mkdir()
    files = source_files(tmp_path)
    (tmp_path / "suite-lock.json").write_text(json.dumps({"files": files, "source_hash": digest(files)}))
    (tmp_path / "development" / "new.txt").write_text("changed")
    with pytest.raises(ValueError, match="drifted"):
        build(tmp_path / "output", root=tmp_path)


def test_development_case_identity_matches_full_regression_export(tmp_path):
    from corecoder.coding_eval import _hash
    development = build(tmp_path / "development", "development")
    all_cases = build(tmp_path / "all", "all")
    assert [_hash(c) for c in development] == [_hash(c) for c in all_cases if c["split"] == "development"]
