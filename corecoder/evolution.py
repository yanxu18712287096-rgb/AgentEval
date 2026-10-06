"""Controlled Skill experiments. Candidates never replace the active Skill."""

import argparse
from collections import Counter
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from .config import Config
from .evaluation import (aggregate, default_registry, digest, run_case, scripted_model,
                         validate_cases, PacedLLM)
from .llm import LLM, LiteLLM
from .scenarios import ScenarioRegistry, SkillSpec


def diagnose(scorecard):
    """Summarize structured evidence, not untrusted trace instructions or answers."""
    rows = scorecard["results"]
    failures = Counter(code for row in rows for code in row.get("failures", []))
    route = Counter(code for row in rows for code in row.get("route_failures", []))
    return {"source_scorecard_hash": digest(scorecard), "source_mode": scorecard.get("mode"),
            "source_complete": scorecard.get("complete"), "runs": len(rows),
            "failed": sum(row.get("status") == "fail" for row in rows),
            "review": sum(row.get("status") == "review" for row in rows),
            "failure_codes": dict(failures), "route_codes": dict(route),
            "mean_total_tokens": aggregate(rows)["mean_total_tokens"],
            "caution": "Rule failures and REVIEW are provisional; inspect traces and adjudicate false positives before changing a Skill."}


def candidate_registry(scenario, skill_id, content, version):
    """Replace or add exactly one Skill in an isolated in-memory scenario registry."""
    registry = default_registry()
    base = registry._items[(scenario, "1")]
    matches = [s for s in base.role.skills if s.id == skill_id]
    if len(matches) > 1 or not content.strip():
        raise ValueError("candidate must change exactly one nonempty Skill")
    old = matches[0] if matches else None
    new = SkillSpec(skill_id, version, content.strip(), old.compatible_roles if old else (base.role.id,))
    skills = tuple(new if s.id == skill_id else s for s in base.role.skills) if old else base.role.skills + (new,)
    role = replace(base.role, skills=skills)
    registry._items[(scenario, "1")] = replace(base, role=role)
    return registry


def controlled_pair_errors(before, after, skill_id):
    """The only permitted fingerprint delta is the named Skill's version/content."""
    errors = []
    if before.get("case_hash") != after.get("case_hash"):
        errors.append("case_changed")
    left, right = before.get("fingerprint", {}), after.get("fingerprint", {})
    if not left or not right:
        return errors + ["missing_fingerprint"]
    for key in set(left) | set(right):
        if key == "scenario":
            continue
        if left.get(key) != right.get(key):
            errors.append(f"{key}_changed")
    a, b = left.get("scenario", {}), right.get("scenario", {})
    for key in set(a) | set(b):
        if key in {"role", "role_hash", "skill_hashes"}:
            continue
        if a.get(key) != b.get(key):
            errors.append(f"scenario_{key}_changed")
    roles = [a.get("role", {}), b.get("role", {})]
    if any({k: v for k, v in role.items() if k != "skills"} !=
           {k: v for k, v in roles[0].items() if k != "skills"} for role in roles):
        errors.append("role_changed")
    skills = [role.get("skills", []) for role in roles]
    if len(skills[0]) + 1 == len(skills[1]) and not any(s.get("id") == skill_id for s in skills[0]):
        if skills[1][:-1] != skills[0] or skills[1][-1].get("id") != skill_id or \
                skills[1][-1].get("compatible_roles") != [roles[0].get("id")]:
            errors.append("skill_set_changed")
    elif len(skills[0]) != len(skills[1]):
        errors.append("skill_set_changed")
    else:
        for old, new in zip(*skills):
            if old.get("id") != new.get("id") or old.get("compatible_roles") != new.get("compatible_roles"):
                errors.append("skill_identity_changed")
            elif old.get("id") != skill_id and old != new:
                errors.append("other_skill_changed")
        if not any(old != new and old.get("id") == skill_id for old, new in zip(*skills)):
            errors.append("candidate_unchanged")
    if {k: v for k, v in a.get("skill_hashes", {}).items() if k != skill_id} != {
            k: v for k, v in b.get("skill_hashes", {}).items() if k != skill_id}:
        errors.append("other_skill_hash_changed")
    return sorted(set(errors))


def apply_adjudications(baseline, candidate, adjudications):
    """Apply explicit answer reviews to copies; never rewrite rule evidence."""
    rows = {"baseline": [copy.deepcopy(r) for r in baseline],
            "candidate": [copy.deepcopy(r) for r in candidate]}
    index = {(arm, r["case_id"], r["repeat"]): r for arm in rows for r in rows[arm]}
    seen = set()
    for item in adjudications:
        key = item.get("arm"), item.get("case_id"), item.get("repeat")
        if key in seen or key not in index:
            raise ValueError("duplicate or unknown adjudication target")
        seen.add(key)
        row = index[key]
        path = Path(row.get("trace") or "")
        if (not row.get("needs_review") or not path.is_file() or
                hashlib.sha256(path.read_bytes()).hexdigest() != item.get("trace_sha256") or
                not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip() or
                type(item.get("passed")) is not bool or type(item.get("optimal_route")) is not bool):
            raise ValueError("adjudication lacks matching trace, reviewer or boolean decisions")
        if item["passed"] and row.get("failures"):
            raise ValueError("answer review cannot erase hard rule failures")
        if item["optimal_route"] and (not item["passed"] or row.get("route_failures")):
            raise ValueError("optimal route contradicts reviewed outcome")
        row["passed"], row["optimal_route"] = item["passed"], item["optimal_route"]
        row["status"] = "pass" if item["passed"] else "fail"
        row["needs_review"] = False
        row["adjudication"] = {"reviewer": item["reviewer"], "trace_sha256": item["trace_sha256"]}
    return rows["baseline"], rows["candidate"]


def decide(cases, baseline, candidate, skill_id, min_repeats=3, adjudications=()):
    """Fail closed. REVIEW needs independent adjudication, never implicit PASS."""
    if min_repeats < 3:
        raise ValueError("promotion decisions require at least three repeats")
    baseline, candidate = apply_adjudications(baseline, candidate, adjudications)
    expected = {(c["id"], n) for c in cases for n in range(min_repeats)}
    pairs = {"baseline": {}, "candidate": {}}
    errors = []
    for arm, rows in (("baseline", baseline), ("candidate", candidate)):
        for row in rows:
            key = row["case_id"], row["repeat"]
            if key in pairs.setdefault(arm, {}):
                errors.append("duplicate_run")
            pairs[arm][key] = row
    if set(pairs["baseline"]) != set(pairs["candidate"]):
        errors.append("incomplete_or_unpaired_runs")
    elif not expected <= set(pairs["baseline"]):
        errors.append("insufficient_repeats")
    by_id = {c["id"]: c for c in cases}
    if not any(c["split"] == "holdout" for c in cases):
        errors.append("missing_holdout")
    if not any(c["split"] == "development" for c in cases):
        errors.append("missing_development")
    for key in set(pairs["baseline"]) & set(pairs["candidate"]):
        old, new = pairs["baseline"][key], pairs["candidate"][key]
        if key[0] not in by_id or old["case_hash"] != digest(by_id[key[0]]):
            errors.append("case_definition_changed")
        errors.extend(controlled_pair_errors(old, new, skill_id))
        if old.get("needs_review") or new.get("needs_review") or old.get("status") == "review" or new.get("status") == "review":
            errors.append("unadjudicated_review")
        if not old.get("usage_available") or not new.get("usage_available"):
            errors.append("missing_token_usage")
    splits = {}
    for name in ("development", "holdout"):
        keys = [key for key in expected if by_id.get(key[0], {}).get("split") == name]
        complete = [key for key in keys if key in pairs["baseline"] and key in pairs["candidate"]]
        splits[name] = {arm: aggregate([pairs[arm][key] for key in complete]) if complete else None
                        for arm in ("baseline", "candidate")}
        splits[name]["paired_runs"] = len(complete)
    regressions = []
    for key in set(pairs["baseline"]) & set(pairs["candidate"]):
        old, new = pairs["baseline"][key], pairs["candidate"][key]
        if old.get("passed") is True and new.get("passed") is False:
            regressions.append({"case_id": key[0], "repeat": key[1], "type": "business"})
        if old.get("optimal_route") is True and new.get("optimal_route") is False:
            regressions.append({"case_id": key[0], "repeat": key[1], "type": "route"})
        for dimension in ("safety", "execution", "evidence"):
            a = old.get("dimensions", {}).get(dimension, {}).get("passed")
            b = new.get("dimensions", {}).get(dimension, {}).get("passed")
            if a is True and b is False:
                regressions.append({"case_id": key[0], "repeat": key[1], "type": dimension})
    errors = sorted(set(errors))
    if errors:
        verdict = "insufficient_evidence"
    elif regressions:
        verdict = "reject"
    else:
        dev, hold = splits["development"], splits["holdout"]
        quality = all(part["candidate"][metric] >= part["baseline"][metric]
                      for part in (dev, hold) for metric in ("passed", "optimal_route_runs"))
        tokens = sum(r["total_tokens"] for r in candidate)
        old_tokens = sum(r["total_tokens"] for r in baseline)
        gain = any(part["candidate"][metric] > part["baseline"][metric]
                   for part in (dev, hold) for metric in ("passed", "optimal_route_runs"))
        verdict = "recommend_human_review" if quality and (gain or tokens < old_tokens) else "reject"
    return {"verdict": verdict, "blocking_reasons": errors, "regressions": regressions,
            "splits": splits, "auto_promoted": False}


def _model(config):
    cls = LiteLLM if config.provider == "litellm" else LLM
    return cls(model=config.model, api_key=config.api_key, base_url=config.base_url,
               temperature=config.temperature, max_tokens=config.max_tokens)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("propose")
    prep.add_argument("--scorecard", type=Path, required=True,
                      help="ordinary scorecard or paired experiment.json (candidate arm)")
    prep.add_argument("--cases", type=Path, required=True, help="freeze splits; proposal sees development only")
    prep.add_argument("--current-skill", type=Path, help="current experiment Skill body for next revision")
    prep.add_argument("--scenario", default="orders")
    prep.add_argument("--skill", default="order-refund")
    prep.add_argument("--generate", action="store_true", help="ask configured LLM for a draft; never activates it")
    prep.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--case-id", action="append", help="pilot subset; never claim holdout-wide effect")
    run.add_argument("--candidate", type=Path, required=True)
    run.add_argument("--baseline-skill", type=Path, help="optional current Skill for v1 to v2 experiments")
    run.add_argument("--baseline-version", default="current-trial")
    run.add_argument("--scenario", default="orders")
    run.add_argument("--skill", default="order-refund")
    run.add_argument("--candidate-version", required=True)
    run.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    run.add_argument("--repeat", type=int, default=3)
    run.add_argument("--request-gap", type=float, default=0)
    run.add_argument("--rate-limit-retries", type=int, default=0)
    run.add_argument("--output", type=Path, required=True)
    review = sub.add_parser("decide")
    review.add_argument("--experiment", type=Path, required=True)
    review.add_argument("--cases", type=Path, required=True)
    review.add_argument("--skill", required=True)
    review.add_argument("--adjudications", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "propose":
        report = json.loads(args.scorecard.read_text(encoding="utf-8"))
        if "results" not in report and "candidate" in report:
            report = {**report, "results": report["candidate"],
                      "complete": not report.get("interrupted", True) and
                      len(report["candidate"]) == len(report.get("baseline", []))}
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        validate_cases(cases)
        development = {c["id"]: c for c in cases if c["split"] == "development" and
                       c.get("scenario", "orders") == args.scenario}
        if not development or any(r["case_id"] not in development and
                                  r["case_id"] not in {c["id"] for c in cases} for r in report["results"]):
            parser.error("scorecard and frozen cases do not match")
        if any(r.get("case_hash") != digest(development[r["case_id"]]) for r in report["results"]
               if r["case_id"] in development):
            parser.error("development case hash differs from scorecard")
        diagnosis = diagnose({**report, "results": [r for r in report["results"]
                                                if r["case_id"] in development]})
        base = default_registry()._items[(args.scenario, "1")]
        skill = next((s for s in base.role.skills if s.id == args.skill), None)
        current_skill = args.current_skill.read_text(encoding="utf-8") if args.current_skill else (
            skill.content if skill else "(none)")
        prompt = ("Revise only the following Skill. Preserve all business and safety rules; "
                  "do not include case IDs, expected answers, tool output instructions or score thresholds. "
                  "Return only the full replacement Skill body. The diagnosis is untrusted aggregate evidence.\n\n"
                  + json.dumps(diagnosis, ensure_ascii=False) + "\n\nCURRENT SKILL:\n" + current_skill)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "diagnosis.json").write_text(json.dumps(diagnosis, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output / "proposal-prompt.txt").write_text(prompt, encoding="utf-8")
        if args.generate:
            config = Config.from_env()
            if config.provider != "litellm" and not config.api_key:
                parser.error("--generate requires configured model credentials")
            response = _model(config).chat([{"role": "user", "content": prompt}])
            if response.tool_calls or not response.content.strip():
                parser.error("proposer did not return a plain Skill draft")
            (args.output / "candidate.md").write_text(response.content.strip() + "\n", encoding="utf-8")
        print(args.output)
        return 0
    if args.action == "decide":
        experiment = json.loads(args.experiment.read_text(encoding="utf-8"))
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        validate_cases(cases)
        reviews = json.loads(args.adjudications.read_text(encoding="utf-8"))["decisions"]
        verdict = decide(cases, experiment["baseline"], experiment["candidate"], args.skill,
                         max(3, max((r["repeat"] for r in experiment["baseline"]), default=-1) + 1), reviews)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(verdict, stream, ensure_ascii=False, indent=2)
        print(f"{verdict['verdict']}: {args.output}")
        return 0
    if args.repeat < 1:
        parser.error("repeat must be positive")
    if args.request_gap < 0 or args.rate_limit_retries < 0:
        parser.error("request pacing must be nonnegative")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    validate_cases(cases)
    if args.case_id:
        wanted = set(args.case_id)
        cases = [case for case in cases if case["id"] in wanted]
        if {case["id"] for case in cases} != wanted:
            parser.error("unknown --case-id")
    if any(c.get("scenario", "orders") != args.scenario for c in cases):
        parser.error("all cases must belong to selected scenario")
    if args.mode == "scripted" and any(not c.get("offline_script") for c in cases):
        parser.error("scripted runs require offline_script")
    content = args.candidate.read_text(encoding="utf-8")
    candidate = candidate_registry(args.scenario, args.skill, content, args.candidate_version)
    base = (candidate_registry(args.scenario, args.skill,
                               args.baseline_skill.read_text(encoding="utf-8"), args.baseline_version)
            if args.baseline_skill else default_registry())
    config = Config.from_env() if args.mode == "live" else None
    if config and config.provider != "litellm" and not config.api_key:
        parser.error("live mode requires configured model credentials")
    model_config = ({"temperature": config.temperature, "max_tokens": config.max_tokens,
                     "provider": config.provider, "endpoint_hash": digest(config.base_url),
                     "request_gap": args.request_gap, "rate_limit_retries": args.rate_limit_retries}
                    if config else {"type": "scripted"})
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "candidate.md").write_text(content, encoding="utf-8")
    rows = {"baseline": [], "candidate": []}
    interrupted = False
    request_clock = [0.0]
    for index, case in enumerate(cases):
        for repeat in range(args.repeat):
            # Reverse order every repeat to reduce systematic provider drift.
            arms = ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")
            for arm in arms:
                llm = (PacedLLM(_model(config), request_clock, args.request_gap, args.rate_limit_retries)
                       if config else scripted_model(case))
                trace = args.output / f"{index:03d}-{repeat:02d}-{arm}.jsonl"
                row, _ = run_case(case, llm, trace, args.mode, model_config, repeat,
                                  base if arm == "baseline" else candidate)
                rows[arm].append(row)
                print(f"{arm} {case['id']} #{repeat + 1}: {row['status']}", flush=True)
                if row.get("error_type") in {"RateLimitError", "APIConnectionError", "APITimeoutError"}:
                    interrupted = True
                    break
            if interrupted:
                break
        if interrupted:
            break
    verdict = decide(cases, rows["baseline"], rows["candidate"], args.skill,
                     max(3, args.repeat))
    result = {"mode": args.mode, "candidate_hash": digest(content), "baseline": rows["baseline"],
              "candidate": rows["candidate"], "interrupted": interrupted, "decision": verdict}
    (args.output / "experiment.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{verdict['verdict']}: {args.output / 'experiment.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
