"""Resume an interrupted paired coding experiment in a new output directory.

The original experiment remains immutable. Only a final provider-error run may
be discarded; every earlier completed run is hash-checked and fingerprinted
against the current runtime before any model request is made.
"""

import argparse
from pathlib import Path

from corecoder.coding_eval import (IMAGE, _hash, coding_fingerprint, load_cases,
                                   recover_coding_result, run_case)
from corecoder.coding_evolution import (candidate_skill, read, seal, unseal,
                                        write_new)
from corecoder.config import Config
from corecoder.evaluation import PacedLLM
from corecoder.evolution import _model


TRANSIENT_PROVIDER_ERRORS = {"APIConnectionError", "APITimeoutError", "RateLimitError"}


def scheduled(cases, repeats):
    for index, case in enumerate(cases):
        for repeat in range(repeats):
            arms = ("baseline", "candidate") if (repeat + index) % 2 == 0 else ("candidate", "baseline")
            for arm in arms:
                yield arm, case, repeat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists():
        parser.error("output already exists; resume never overwrites artifacts")
    old_bundle = read(source / "experiment.json")
    old = unseal(old_bundle)
    if old.get("complete") or old.get("mode") != "live" or not old.get("runs"):
        parser.error("source must be an incomplete live experiment with receipts")
    manifest = unseal(read(source / "manifest.json"))
    metadata = {k: v for k, v in old.items() if k not in {"runs", "complete"}}
    if manifest != metadata:
        parser.error("source manifest differs from experiment")
    root, cases = load_cases(args.cases)
    if cases != old["cases"] or str(root.resolve()) != old["case_root"]:
        parser.error("case list or root changed")
    if len(cases) != 6 or old["repeats"] < 3:
        parser.error("unexpected frozen experiment design")
    candidate, baseline = candidate_skill(old["candidate"]), (
        candidate_skill(old["baseline"]) if old["baseline"] else None)
    planned = list(scheduled(cases, old["repeats"]))
    if len(old["runs"]) > len(planned):
        parser.error("source contains excess runs")
    last = old["runs"][-1]["result"]
    if last.get("error_type") not in TRANSIENT_PROVIDER_ERRORS:
        parser.error("only a final transient provider-error run can be retried")
    keep = old["runs"][:-1]
    cfg = Config.from_env()
    if cfg.provider != "litellm" and not cfg.api_key:
        parser.error("model credentials required")
    model_config = {"provider": cfg.provider, "temperature": cfg.temperature,
                    "max_tokens": cfg.max_tokens, "endpoint_hash": _hash(cfg.base_url),
                    "request_gap": 6.0, "rate_limit_retries": 2}
    for index, entry in enumerate(old["runs"]):
        arm, case, repeat = planned[index]
        row = entry["result"]
        if (entry["arm"], row["case_id"], row["repeat"]) != (arm, case["id"], repeat):
            parser.error("source run order differs from frozen schedule")
        if read(source / f"receipt-{index + 1:04d}.json") != entry:
            parser.error("source receipt differs from experiment")
        if recover_coding_result(Path(row["trace"]).parent, row["fingerprint"]) != row:
            parser.error("source run artifact differs from receipt")
        skill = candidate if arm == "candidate" else baseline
        expected = coding_fingerprint(case, root, cfg.model, IMAGE, model_config, skill)
        if expected != row["fingerprint"]:
            parser.error("runtime, model, case, image or strategy fingerprint changed")
        if index < len(keep) and (row.get("error_type") or
                                  "evaluation_infrastructure_error" in row["failures"]):
            parser.error("an earlier run has an infrastructure error")

    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "manifest.json", seal(metadata))
    write_new(output / "resume-provenance.json", seal({
        "source_experiment": str(source / "experiment.json"),
        "source_sha256": old_bundle["sha256"], "reused_runs": len(keep),
        "discarded_run": {"index": len(keep), "error_type": last["error_type"],
                          "trace": last["trace"]}}))
    experiment = {**metadata, "runs": list(keep), "complete": False}
    for index, entry in enumerate(keep):
        write_new(output / f"receipt-{index + 1:04d}.json", entry)
    clock = [0.0]
    try:
        for index in range(len(keep), len(planned)):
            arm, case, repeat = planned[index]
            skill = candidate if arm == "candidate" else baseline
            llm = PacedLLM(_model(cfg), clock, 6.0, 2)
            row = run_case(case, root, llm, output / f"run-{index:04d}",
                           repeat=repeat, image=IMAGE, model_config=model_config,
                           mode="live", skill=skill)
            entry = {"arm": arm, "result": row}
            experiment["runs"].append(entry)
            write_new(output / f"receipt-{index + 1:04d}.json", entry)
            print(f"{index + 1}/{len(planned)} {arm} {case['id']} #{repeat + 1}: {row['status']}", flush=True)
            if row.get("error_type") or "evaluation_infrastructure_error" in row["failures"]:
                raise RuntimeError("experiment stopped for runtime/provider/infrastructure error")
        experiment["complete"] = True
    finally:
        write_new(output / "experiment.json", seal(experiment))


if __name__ == "__main__":
    main()
