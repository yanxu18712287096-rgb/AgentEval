"""Replayable after-sales Agent evaluation; scripted runs test plumbing, not model quality."""

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import statistics
import time
from types import SimpleNamespace
import uuid

from openai import RateLimitError

from . import __version__
from .agent import Agent
from .config import Config
from .demo import ScriptedLLM
from .llm import LLM, LiteLLM, LLMResponse, ToolCall
from .orders import CUSTOMER_ROLE, OrderStore, order_tools
from .trace import TraceRecorder
from .scenarios import CaseSpec, RoleSpec, ScenarioSpec, ScenarioRegistry, SkillSpec
from .order_evaluation import TOOLS, order_score, validate_order_cases
from .campus_support import (CampusSupportState, campus_tools, campus_score,
                             validate_campus_cases, CAMPUS_ROLE, campus_turn_setup)
from .incident import (IncidentState, incident_tools, incident_score,
                       validate_incident_case, INCIDENT_ROLE, NAMES as INCIDENT_NAMES)


SCHEMA_VERSION = 5


class PacedLLM:
    """Share a request clock across independent case models; retry only provider 429s."""

    def __init__(self, inner, clock, gap_seconds, rate_limit_retries):
        self.inner = inner
        self.clock = clock
        self.gap_seconds = gap_seconds
        self.rate_limit_retries = rate_limit_retries

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def chat(self, *args, **kwargs):
        for attempt in range(self.rate_limit_retries + 1):
            delay = self.clock[0] - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.clock[0] = time.monotonic() + self.gap_seconds
            try:
                return self.inner.chat(*args, **kwargs)
            except RateLimitError:
                if attempt == self.rate_limit_retries:
                    raise
                time.sleep(min(30 * (attempt + 1), 60))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def common_failures(case, events):
    """Protocol and execution checks independent of business state."""
    errors = []
    calls = [e for e in events if e["event"] == "tool_requested"]
    ends = [e for e in events if e["event"] == "tool_finished"]
    completed = {e["call_key"]: e for e in ends}
    if Counter(e["call_key"] for e in calls) != Counter(e["call_key"] for e in ends):
        errors.append("tool_result_pairing")
    for call in calls:
        end = completed.get(call["call_key"])
        if end and (any(end.get(k) != call.get(k) for k in
                        ("tool_name", "tool_call_id", "agent_id", "turn", "round"))
                    or end["seq"] <= call["seq"]):
            errors.append("tool_result_identity_or_order")
            break
    ids = [(e["agent_id"], e["turn"], e["round"], e["tool_call_id"]) for e in calls]
    if len(set(ids)) != len(ids) or any(not e["tool_call_id"] for e in calls):
        errors.append("invalid_tool_call_id")
    turns = [e for e in events if e["event"] == "turn_finished"]
    if len(turns) != len(case["messages"]) or any(e["status"] != "completed" for e in turns):
        errors.append("incomplete_run")
    if any(e["event"] == "turn_failed" for e in events):
        errors.append("runtime_error")
    if any(e["status"] in {"failed", "blocked", "aborted", "interrupted"} for e in ends):
        errors.append("tool_execution_failure")
    return errors


def default_registry():
    skill_file = Path(__file__).resolve().parent.parent / "docs/skill-drafts/order-refund/SKILL.md"
    skill_source = skill_file.read_text(encoding="utf-8")
    skill_content = skill_source.split("---", 2)[2].split("## 审查备注", 1)[0].strip()
    refund_skill = SkillSpec("order-refund", "0.1.1", skill_content, ("order-support",))
    registry = ScenarioRegistry()
    registry.register(ScenarioSpec(
        "orders", "1", RoleSpec("order-support", "3", CUSTOMER_ROLE, tuple(sorted(TOOLS)), (refund_skill,)),
        OrderStore, order_tools, lambda case: validate_order_cases([case]), order_score,
        lambda state: {"refund_count": state.refund_count, "escalation_count": state.escalation_count},
        lambda state: SimpleNamespace(**state), "8", ("orders.py", "order_evaluation.py")))
    registry.register(ScenarioSpec(
        "campus-support", "1", RoleSpec("campus-usage-assistant", "1", CAMPUS_ROLE,
                                      ("search_help", "search_service_notices")),
        CampusSupportState, campus_tools, validate_campus_cases, campus_score,
        lambda state: state.snapshot(), CampusSupportState.restore, "1",
        ("campus_support.py",), campus_turn_setup,
        ("docs/scenario-drafts/campus-support/help-center.md",
         "docs/scenario-drafts/campus-support/service-notices.md")))
    registry.register(ScenarioSpec(
        "saas-incident", "1", RoleSpec("incident-triage", "1", INCIDENT_ROLE, INCIDENT_NAMES),
        IncidentState, incident_tools, validate_incident_case, incident_score,
        lambda state: state.snapshot(), IncidentState.restore, "1", ("incident.py",)))
    return registry


def validate_cases(cases, registry=None):
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a nonempty list")
    registry = registry or default_registry()
    ids = set()
    for case in cases:
        spec = CaseSpec.from_dict(case)
        if spec.data["id"] in ids:
            raise ValueError("duplicate case ID")
        ids.add(spec.data["id"])
        registry.resolve(case).validate(spec.data)


def score(case, events, store, scenario=None):
    scenario = scenario or default_registry().resolve(case)
    result = scenario.scorer(case, events, store)
    common = common_failures(case, events)
    calls = [e for e in events if e["event"] == "tool_requested"]
    rounds = sum(e["event"] == "model_requested" for e in events)
    route = case["route"]
    route_errors = list(result.get("route_failures", []))
    if rounds > route["max_model_rounds"]:
        route_errors.append("excess_model_rounds")
    if len(calls) > route["max_tool_calls"]:
        route_errors.append("excess_tool_calls")
    result["route_failures"] = list(dict.fromkeys(route_errors))
    result["model_rounds"], result["tool_calls"] = rounds, len(calls)
    result.setdefault("tool_results", dict(Counter(e["status"] for e in events if e["event"] == "tool_finished")))
    result["failures"] = list(dict.fromkeys(common + result["failures"]))
    result["passed"] = not result["failures"]
    result["optimal_route"] = result["passed"] and not result["route_failures"] and result.get("optimal_route", True)
    groups = result.pop("failure_groups", {})
    dimensions = {k: list(groups.get(k, [])) for k in ("execution", "business", "safety", "evidence", "efficiency")}
    dimensions["execution"] = common
    for failure in result["failures"]:
        if failure in common:
            continue
        if not any(failure in values for values in dimensions.values()):
            dimensions["business"].append(failure)
    dimensions["efficiency"] = result.get("route_failures", [])
    result["dimensions"] = {k: {"passed": not v, "failures": v} for k, v in dimensions.items()}
    apply_review_status(result)
    return result


def apply_review_status(result):
    """A lexical miss is neither confirmed success nor confirmed task failure."""
    result["rule_passed"] = not result["failures"]
    result["rule_optimal_route"] = (result["rule_passed"] and not result.get("route_failures", [])
                                     and result.get("optimal_route", True) is not False)
    result.setdefault("review_reasons", [])
    result["needs_review"] = bool(result["review_reasons"])
    result["status"] = ("fail" if not result["rule_passed"] else
                        "review" if result["needs_review"] else "pass")
    result["passed"] = None if result["status"] == "review" else result["status"] == "pass"
    result["optimal_route"] = None if result["status"] == "review" else (
        result["status"] == "pass" and result["rule_optimal_route"])
    result["answer_check"] = {"status": "review" if result["needs_review"] else "no_lexical_miss",
                              "reasons": result["review_reasons"]}


def run_fingerprint(case, scenario, tools, model, model_config):
    root = Path(__file__).parent
    files = tuple(str(p.relative_to(root)) for p in sorted(root.rglob("*.py"))) + scenario.implementation_files
    resources = {name: hashlib.sha256((root.parent / name).read_bytes()).hexdigest()
                 for name in scenario.resource_files}
    return {"runtime_version": __version__, "schema_version": SCHEMA_VERSION,
            "code": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
            "resources": resources,
            "scenario": scenario.identity(), "tools_hash": digest([t.schema() for t in tools]),
            "case_hash": digest(case), "model": model, "model_config": model_config}


class ReceiptScriptedLLM(ScriptedLLM):
    """Resolve test-only receipt placeholders from preceding real tool messages."""

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        resp = super().chat(messages, tools, on_token, on_reasoning)
        for tc in resp.tool_calls:
            receipt = tc.arguments.get("query_receipt")
            if not isinstance(receipt, str) or not receipt.startswith("$receipt:"):
                continue
            order_id = receipt.split(":", 1)[1]
            for message in reversed(messages):
                if message.get("role") != "tool":
                    continue
                try:
                    result = json.loads(message.get("content", ""))
                except (ValueError, TypeError):
                    continue
                if result.get("order_id") == order_id and result.get("ok") is True and result.get("query_receipt"):
                    tc.arguments["query_receipt"] = result["query_receipt"]
                    break
        return resp


def scripted_model(case):
    responses = []
    for i, step in enumerate(case["offline_script"]):
        responses.append(LLMResponse(content=step.get("content", ""), tool_calls=[
            ToolCall(id=f"script_{i}_{j}", name=c["name"], arguments=copy.deepcopy(c["arguments"]))
            for j, c in enumerate(step.get("calls", []))]))
    return ReceiptScriptedLLM(responses)


def run_case(case, llm, path=None, mode="scripted", model_config=None, repeat=0, registry=None):
    registry = registry or default_registry()
    validate_cases([case], registry)
    scenario = registry.resolve(case)
    store, tools = scenario.create(case["fixture"])
    fingerprint = run_fingerprint(case, scenario, tools, llm.model, model_config or {})
    trace = TraceRecorder(path, case_id=case["id"], case_definition=case, mode=mode,
                          schema_version=SCHEMA_VERSION, repeat=repeat, runtime_version=__version__,
                          model=llm.model, model_config=model_config or {},
                          role=scenario.role.render(), case_hash=digest(case), fingerprint=fingerprint)
    agent = Agent(llm=llm, tools=tools, system_prompt_override=scenario.role.render(),
                  trace=trace, max_rounds=12)
    start = time.monotonic()
    before_prompt = getattr(llm, "total_prompt_tokens", 0)
    before_completion = getattr(llm, "total_completion_tokens", 0)
    before_missing = getattr(llm, "missing_usage_calls", 0)
    error = None
    try:
        for index, message in enumerate(case["messages"]):
            if scenario.turn_setup is not None:
                context = scenario.turn_setup(store, index, message)
                agent._system = scenario.role.render() + "\n\n" + context
            agent.chat(message["content"])
    except Exception as exc:
        error = type(exc).__name__
    elapsed = time.monotonic() - start
    trace.record("run_finished", elapsed_seconds=elapsed, state=scenario.snapshot(store),
                 error_type=error)
    result = score(case, trace.events, store, scenario)
    if error and "runtime_error" not in result["failures"]:
        result["failures"].append("runtime_error")
        result["passed"] = False
        result["optimal_route"] = False
        apply_review_status(result)
    responses = [e for e in trace.events if e["event"] == "model_response"]
    main_prompt = sum(e["prompt_tokens"] for e in responses)
    main_completion = sum(e["completion_tokens"] for e in responses)
    all_prompt = getattr(llm, "total_prompt_tokens", 0) - before_prompt
    all_completion = getattr(llm, "total_completion_tokens", 0) - before_completion
    missing_usage = getattr(llm, "missing_usage_calls", 0) - before_missing
    usage_complete = mode == "live" and missing_usage == 0 and all(e["usage_available"] for e in responses)
    result.update(
        case_id=case["id"], case_hash=digest(case), repeat=repeat, fingerprint=fingerprint,
        trace=str(path) if path else None,
        error_type=error, elapsed_seconds=elapsed, usage_available=usage_complete,
        prompt_tokens=all_prompt if usage_complete else None,
        completion_tokens=all_completion if usage_complete else None,
        total_tokens=all_prompt + all_completion if usage_complete else None,
        main_prompt_tokens=main_prompt if usage_complete else None,
        main_completion_tokens=main_completion if usage_complete else None,
        internal_prompt_tokens=all_prompt - main_prompt if usage_complete else None,
        internal_completion_tokens=all_completion - main_completion if usage_complete else None,
        model_elapsed_seconds=sum(e.get("elapsed_seconds", 0) for e in responses),
        tool_elapsed_seconds=sum(e.get("elapsed_seconds") or 0 for e in trace.events if e["event"] == "tool_finished"),
    )
    return result, trace


def recover_run(case, path, repeat, model=None, model_config=None):
    """Recover a finished run from its immutable trace; never score partial traces."""
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    started = events[0] if events else {}
    scenario = default_registry().resolve(case)
    _, tools = scenario.create(case["fixture"])
    fingerprint = run_fingerprint(case, scenario, tools, started.get("model"), started.get("model_config", {}))
    if (started.get("event") != "run_started" or started.get("case_hash") != digest(case)
            or started.get("schema_version") != SCHEMA_VERSION
            or started.get("fingerprint") != fingerprint
            or started.get("repeat") != repeat or started.get("mode") != "live"
            or started.get("role") != scenario.role.render()
            or (model is not None and started.get("model") != model)
            or (model_config is not None and started.get("model_config") != model_config)):
        raise ValueError(f"trace does not match case/repeat: {path}")
    if events[-1]["event"] != "run_finished":
        archived = path.with_name(path.stem + ".interrupted.jsonl")
        if archived.exists():
            raise FileExistsError(archived)
        path.rename(archived)
        return None
    finished = events[-1]
    store = scenario.restore(finished["state"])
    result = score(case, events, store)
    error = finished.get("error_type")
    if error and "runtime_error" not in result["failures"]:
        result["failures"].append("runtime_error")
        result["passed"] = result["optimal_route"] = False
        apply_review_status(result)
    responses = [e for e in events if e["event"] == "model_response"]
    usage_complete = bool(responses) and all(e.get("usage_available") for e in responses)
    prompt = sum(e.get("prompt_tokens", 0) for e in responses)
    completion = sum(e.get("completion_tokens", 0) for e in responses)
    result.update(
        case_id=case["id"], case_hash=digest(case), repeat=repeat, trace=str(path), fingerprint=fingerprint,
        error_type=error, elapsed_seconds=finished["elapsed_seconds"],
        usage_available=usage_complete,
        prompt_tokens=prompt if usage_complete else None,
        completion_tokens=completion if usage_complete else None,
        total_tokens=prompt + completion if usage_complete else None,
        main_prompt_tokens=prompt if usage_complete else None,
        main_completion_tokens=completion if usage_complete else None,
        internal_prompt_tokens=None, internal_completion_tokens=None,
        model_elapsed_seconds=sum(e.get("elapsed_seconds", 0) for e in responses),
        tool_elapsed_seconds=sum(e.get("elapsed_seconds") or 0 for e in events if e["event"] == "tool_finished"),
        recovered_from_trace=True,
    )
    return result


def rescore_trace(path, registry=None):
    """Score recorded evidence only. Never construct an Agent or execute tools."""
    events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    if not events or events[0].get("event") != "run_started" or events[-1].get("event") != "run_finished":
        raise ValueError("rescore requires a complete trace")
    started, finished = events[0], events[-1]
    if started.get("schema_version") not in {2, 3, 4, 5}:
        raise ValueError("unsupported source trace schema")
    if any(e.get("seq") != i or e.get("run_id") != started.get("run_id") for i, e in enumerate(events)):
        raise ValueError("trace sequence/run identity is inconsistent")
    if sum(e.get("event") == "run_finished" for e in events) != 1:
        raise ValueError("trace has multiple terminal events")
    case = started["case_definition"]
    if digest(case) != started.get("case_hash"):
        raise ValueError("trace case hash mismatch")
    registry = registry or default_registry()
    validate_cases([case], registry)
    scenario = registry.resolve(case)
    state = finished.get("state")
    if state is None and scenario.id == "orders":
        state = {k: finished[k] for k in ("refund_count", "escalation_count")}
    if state is None:
        raise ValueError("trace lacks scenario state")
    result = score(case, events, scenario.restore(state), scenario)
    if finished.get("error_type"):
        result["passed"] = result["optimal_route"] = False
        if "runtime_error" not in result["failures"]:
            result["failures"].append("runtime_error")
        result["dimensions"]["execution"] = {"passed": False, "failures": ["runtime_error"]}
        apply_review_status(result)
    return {"operation": "rescore", "source_trace": str(path),
            "source_trace_hash": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "source_runtime_version": started.get("runtime_version"),
            "source_schema_version": started.get("schema_version"),
            "source_fingerprint": started.get("fingerprint"),
            "source_mode": started.get("mode"), "case_id": case["id"],
            "scoring_runtime_version": __version__, "scoring_schema_version": SCHEMA_VERSION,
            "scorer_version": scenario.scorer_version,
            "scorer_hash": digest({name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                   for name in ("evaluation.py", "scenarios.py") + scenario.implementation_files}),
            "historical_evidence_only": True, "result": result}


def comparison_reasons(before, after):
    if before.get("case_hash") != after.get("case_hash"):
        return ["case_changed"]
    left, right = before.get("fingerprint"), after.get("fingerprint")
    if not left or not right:
        return ["missing_fingerprint"]
    # Strict equality is deliberate; controlled experiments can inspect raw results.
    return [f"{key}_changed" for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key)]


def compare_reports(report, baseline):
    if report["mode"] != baseline["mode"]:
        raise ValueError("cannot compare scripted and live evaluation")
    old = {(r["case_id"], r.get("repeat", 0)): r for r in baseline["results"]}
    changes = []
    for row in report["results"]:
        key = row["case_id"], row.get("repeat", 0)
        before = old.get(key)
        reasons = ["missing_baseline"] if before is None else comparison_reasons(before, row)
        if reasons:
            changes.append({"case_id": key[0], "repeat": key[1], "change": "not_comparable", "reasons": reasons})
        else:
            changes.append({"case_id": key[0], "repeat": key[1],
                            "before": before["passed"], "after": row["passed"],
                            "before_status": before.get("status"), "after_status": row.get("status"),
                            "before_review": before.get("review_reasons", []),
                            "after_review": row.get("review_reasons", []),
                            "before_optimal": before.get("optimal_route"), "after_optimal": row["optimal_route"],
                            "before_failures": before["failures"], "after_failures": row["failures"]})
    return {
        "changes": changes,
        "removed_cases": sorted([{"case_id": k[0], "repeat": k[1]} for k in set(old) -
                                 {(r["case_id"], r.get("repeat", 0)) for r in report["results"]}],
                                key=lambda v: (v["case_id"], v["repeat"])),
        "baseline_model": baseline.get("model"), "candidate_model": report.get("model"),
        "configuration_changed": baseline.get("model_config") != report.get("model_config"),
        "role_changed": baseline.get("role_hash") != report.get("role_hash"),
        "score_schema_changed": baseline.get("schema_version") != report.get("schema_version"),
    }


def _mean(values):
    return round(statistics.mean(values), 3) if values else None


def _median(values):
    return round(statistics.median(values), 3) if values else None


def aggregate(results):
    total = len(results)
    completed = sum(r["passed"] is True for r in results)
    optimal = sum(r["optimal_route"] is True for r in results)
    category_totals = Counter()
    for row in results:
        category_totals.update(row["tool_results"])
    tokens = [r["total_tokens"] for r in results if r["total_tokens"] is not None]
    return {
        "total_runs": total, "passed": completed, "success_rate": completed / total,
        "failed_runs": sum(r["passed"] is False for r in results),
        "review_runs": sum(r["passed"] is None for r in results),
        "needs_review_runs": sum(r.get("needs_review", False) for r in results),
        "rule_passed_runs": sum(r.get("rule_passed", r["passed"] is True) for r in results),
        "rule_pass_rate": sum(r.get("rule_passed", r["passed"] is True) for r in results) / total,
        "rule_optimal_runs": sum(r.get("rule_optimal_route", r["optimal_route"] is True) for r in results),
        "optimal_route_runs": optimal, "optimal_route_rate": optimal / total,
        "optimal_among_successful": optimal / completed if completed else None,
        "tool_results": dict(category_totals),
        "mean_model_rounds": _mean([r["model_rounds"] for r in results]),
        "median_model_rounds": _median([r["model_rounds"] for r in results]),
        "mean_tool_calls": _mean([r["tool_calls"] for r in results]),
        "median_tool_calls": _median([r["tool_calls"] for r in results]),
        "mean_elapsed_seconds": _mean([r["elapsed_seconds"] for r in results]),
        "median_elapsed_seconds": _median([r["elapsed_seconds"] for r in results]),
        "token_observed_runs": len(tokens), "token_unavailable_runs": total - len(tokens),
        "mean_total_tokens": _mean(tokens), "median_total_tokens": _median(tokens),
        "dimension_failed_runs": {name: sum(not r.get("dimensions", {}).get(name, {"passed": True})["passed"]
                                             for r in results)
                                  for name in ("execution", "business", "safety", "evidence", "efficiency")},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cases", type=Path)
    source.add_argument("--rerun", "--replay", dest="replay", type=Path,
                        help="run a historical case again; NOT recorded-response playback")
    source.add_argument("--rescore", type=Path, help="score a completed trace without model/tool calls")
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--repeat", type=int, default=1, help="independent runs per case")
    parser.add_argument("--request-gap", type=float, default=0,
                        help="minimum seconds between live model requests")
    parser.add_argument("--rate-limit-retries", type=int, default=0,
                        help="extra attempts after provider 429 (30-60 second backoff)")
    parser.add_argument("--output", type=Path, help="new output directory; refuses overwriting")
    parser.add_argument("--resume", action="store_true", help="reuse finished traces in --output")
    parser.add_argument("--baseline", type=Path, help="previous scorecard.json")
    args = parser.parse_args()
    if args.rescore:
        if args.resume or args.baseline or args.mode != "scripted":
            parser.error("rescore does not accept resume, baseline or live mode")
        result = rescore_trace(args.rescore)
        out = args.output or Path("eval/rescores") / uuid.uuid4().hex
        out.mkdir(parents=True, exist_ok=False)
        (out / "rescore.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Historical rescore (no execution): {out / 'rescore.json'}")
        return 0
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    if args.request_gap < 0 or args.rate_limit_retries < 0:
        parser.error("request gap and rate-limit retries must be nonnegative")
    if args.replay:
        with args.replay.open(encoding="utf-8") as stream:
            cases = [json.loads(next(stream))["case_definition"]]
    else:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
    validate_cases(cases)
    if args.mode == "scripted" and any(not case.get("offline_script") for case in cases):
        parser.error("scripted mode requires offline_script for every case")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    if baseline and baseline["mode"] != args.mode:
        parser.error("baseline mode must match candidate mode")
    config = Config.from_env() if args.mode == "live" else None
    if config and config.provider != "litellm" and not config.api_key:
        parser.error("live mode requires a configured API key")
    out = args.output or Path("eval/runs") / uuid.uuid4().hex
    if args.resume:
        if not args.output or not out.is_dir():
            parser.error("--resume requires an existing --output directory")
        if json.loads((out / "cases.json").read_text(encoding="utf-8")) != cases:
            parser.error("resume case definitions differ from saved cases.json")
    else:
        out.mkdir(parents=True, exist_ok=False)
        (out / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    model_config = {"temperature": config.temperature, "max_tokens": config.max_tokens,
                    "provider": config.provider, "request_gap": args.request_gap,
                    "rate_limit_retries": args.rate_limit_retries,
                    "endpoint_hash": digest(config.base_url)} if config else {"type": "scripted"}
    results = []
    model_name = "scripted-demo"
    request_clock = [0.0]
    stopped_for_provider = False
    for index, case in enumerate(cases):
        for repeat in range(args.repeat):
            trace_path = out / f"{index:03d}-{repeat:02d}.jsonl"
            if args.resume and trace_path.exists():
                recovered = recover_run(case, trace_path, repeat, config.model if config else None,
                                        model_config)
                if recovered is not None:
                    results.append(recovered)
                    print(f"REUSED {case['id']} #{repeat+1}: optimal={recovered['optimal_route']}")
                    continue
            if config:
                cls = LiteLLM if config.provider == "litellm" else LLM
                llm = cls(model=config.model, api_key=config.api_key, base_url=config.base_url,
                          temperature=config.temperature, max_tokens=config.max_tokens)
                llm = PacedLLM(llm, request_clock, args.request_gap, args.rate_limit_retries)
            else:
                llm = scripted_model(case)
            model_name = llm.model
            row, _ = run_case(case, llm, trace_path,
                              args.mode, model_config, repeat)
            results.append(row)
            print(f"{row['status'].upper()} {case['id']} #{repeat+1}: "
                  f"optimal={row['optimal_route']} {', '.join(row['failures'])}")
            if args.mode == "live" and row["error_type"] in {"RateLimitError", "APIConnectionError", "APITimeoutError"}:
                stopped_for_provider = True
                print(f"Stopped after provider error: {row['error_type']}; partial report follows")
                break
        if stopped_for_provider:
            break
    totals = aggregate(results)
    report = {"schema_version": SCHEMA_VERSION, "runtime_version": __version__, "mode": args.mode,
              "model": model_name, "model_config": model_config,
              "role_hash": digest([r["fingerprint"]["scenario"]["role_hash"] for r in results]),
              "case_count": len(cases), "repeats": args.repeat, "cases_hash": digest(cases),
              "expected_runs": len(cases) * args.repeat, "complete": not stopped_for_provider,
              **totals, "results": results}
    if baseline:
        report["comparison"] = compare_reports(report, baseline)
    (out / "scorecard.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    label = "脚本演练（不代表模型效果）" if args.mode == "scripted" else "真实模型评测"
    lines = [f"# {label}", "",
             f"版本：{__version__}；通过：{totals['passed']}/{totals['total_runs']}；"
             f"高效路线：{totals['optimal_route_runs']}/{totals['total_runs']}", "",
             f"硬性断言通过：{totals['rule_passed_runs']}/{totals['total_runs']}；"
             f"失败：{totals['failed_runs']}；仅待复核：{totals['review_runs']}；"
             f"含短语疑点（含失败运行）：{totals['needs_review_runs']}", "",
             "通过表示当前规则未发现问题，不代表完整语义正确；待复核不计为通过或失败，仍保留在总分母。", "",
             "分维度失败运行数：" + json.dumps(totals["dimension_failed_runs"], ensure_ascii=False), "",
             "| 案例 | 次数 | 任务 | 高效路线 | 模型轮次 | 工具次数 | 被拒绝 | token | 耗时(s) | 失败原因 |",
             "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in results:
        rejected = sum(row["tool_results"].get(k, 0) for k in
                       ("policy_rejected", "permission_blocked", "invalid_arguments"))
        token = row["total_tokens"] if row["total_tokens"] is not None else "N/A"
        lines.append(f"| {row['case_id']} | {row['repeat']+1} | {row['status'].upper()} | "
                     f"{('PENDING' if row['optimal_route'] is None else 'YES' if row['optimal_route'] else 'NO')} | {row['model_rounds']} | "
                     f"{row['tool_calls']} | {rejected} | {token} | "
                     f"{row['elapsed_seconds']:.3f} | {'; '.join(row['failures'] + row['review_reasons']).replace('|', '/')} |")
    lines += ["", "各类拒绝、暂时故障、成功率与成本分布见 scorecard.json。"]
    if baseline:
        lines += ["", "逐案例基线比较见 scorecard.json 的 comparison；案例哈希不同则不可直接比较。"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    review_queue = [{"case_id": r["case_id"], "repeat": r["repeat"], "trace": r["trace"],
                     "status": r["status"], "failures": r["failures"], "reasons": r["review_reasons"]}
                    for r in results if r["needs_review"]]
    (out / "review-queue.json").write_text(json.dumps(review_queue, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {out / 'report.md'}")
    if stopped_for_provider or totals["failed_runs"]:
        return 1
    return 2 if totals["review_runs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
