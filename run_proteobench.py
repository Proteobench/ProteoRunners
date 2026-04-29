#!/usr/bin/env python3
"""ProteoBench pipeline runner.

Usage:
    python run_proteobench.py [options]

Examples:
    # Dry run — show all jobs without executing
    python run_proteobench.py --dry-run

    # Run only DIA-NN on HYE_Astral
    python run_proteobench.py --tool diann --dataset HYE_Astral

    # Run everything enabled in config
    python run_proteobench.py

    # Use a custom config file
    python run_proteobench.py --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml

# Ensure runners package is importable when script is run directly
sys.path.insert(0, str(Path(__file__).parent))

from runners import RUNNER_MAP
from runners.base import BaseRunner, RunResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_jobs(cfg: dict, tool_filter: str | None, dataset_filter: str | None) -> list[BaseRunner]:
    global_cfg = cfg.get("global", {})
    search_params = cfg.get("search_params", {})
    datasets = cfg.get("datasets", {})
    tools = cfg.get("tools", {})

    jobs: list[BaseRunner] = []

    for tool_name, tool_cfg in tools.items():
        if tool_filter and tool_name != tool_filter:
            continue

        RunnerClass = RUNNER_MAP.get(tool_name)
        if RunnerClass is None:
            logger.warning("No runner implemented for tool '%s'; skipping.", tool_name)
            continue

        for version_cfg in tool_cfg.get("versions", []):
            if not version_cfg.get("enabled", False):
                continue

            for dataset_name in tool_cfg.get("datasets", []):
                if dataset_filter and dataset_name != dataset_filter:
                    continue

                if dataset_name not in datasets:
                    logger.warning("Dataset '%s' listed under tool '%s' not found in datasets config; skipping.",
                                   dataset_name, tool_name)
                    continue

                dataset_cfg = datasets[dataset_name]
                runner = RunnerClass(
                    tool_cfg=tool_cfg,
                    dataset_name=dataset_name,
                    dataset_cfg=dataset_cfg,
                    version_cfg=version_cfg,
                    global_cfg=global_cfg,
                    search_params=search_params,
                )
                if not runner.is_compatible():
                    logger.debug("Skipping incompatible job: %s v%s on %s",
                                 tool_name, version_cfg["id"], dataset_name)
                    continue
                jobs.append(runner)

    return jobs


def preflight_all(jobs: list[BaseRunner]) -> bool:
    all_ok = True
    for job in jobs:
        errors = job.preflight_check()
        if errors:
            all_ok = False
            for err in errors:
                logger.error("[%s v%s / %s] preflight FAIL: %s",
                             job.tool_name, job.version_id, job.dataset_name, err)
    return all_ok


def write_summary(results: list[RunResult], output_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = output_dir / f"run_summary_{ts}.tsv"
    fieldnames = ["tool", "version", "dataset", "success", "skipped", "runtime_s", "output_dir", "error_msg"]
    with open(summary_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for r in results:
            writer.writerow({
                "tool": r.tool,
                "version": r.version,
                "dataset": r.dataset,
                "success": r.success,
                "skipped": r.skipped,
                "runtime_s": f"{r.runtime_s:.1f}",
                "output_dir": r.output_dir,
                "error_msg": r.error_msg,
            })
    return summary_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run proteomics search engines on ProteoBench datasets.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to YAML config (default: config.yaml next to this script)")
    parser.add_argument("--tool", help="Run only this tool (e.g. diann, sage)")
    parser.add_argument("--dataset", help="Run only this dataset (e.g. HYE_Astral)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show planned jobs and run preflight checks without executing")
    parser.add_argument("--no-preflight", action="store_true",
                        help="Skip preflight checks and run all enabled jobs regardless")
    args = parser.parse_args()

    if not args.config.exists():
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    cfg = load_config(args.config)
    global_cfg = cfg.get("global", {})
    output_dir = Path(global_cfg.get("output_dir", "results"))
    output_dir.mkdir(parents=True, exist_ok=True)
    max_workers = global_cfg.get("max_parallel_jobs", 2)

    jobs = build_jobs(cfg, tool_filter=args.tool, dataset_filter=args.dataset)
    if not jobs:
        logger.warning("No enabled jobs found. Check config.yaml enabled flags and filters.")
        sys.exit(0)

    logger.info("Found %d job(s) to run.", len(jobs))
    for j in jobs:
        logger.info("  %-15s v%-8s  %s", j.tool_name, j.version_id, j.dataset_name)

    if not args.no_preflight:
        logger.info("Running preflight checks...")
        ok = preflight_all(jobs)
        if not ok:
            logger.error("Preflight checks failed. Fix the errors above or use --no-preflight to skip.")
            sys.exit(1)
        logger.info("All preflight checks passed.")

    if args.dry_run:
        logger.info("Dry run complete. No jobs were executed.")
        sys.exit(0)

    results: list[RunResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_job = {pool.submit(job.run): job for job in jobs}
        for future in as_completed(future_to_job):
            result = future.result()
            results.append(result)
            if result.skipped:
                status = "SKIP"
            elif result.success:
                status = "OK"
            else:
                status = "FAIL"
            logger.info("[%s] %-15s v%-8s  %s  %.1fs  → %s",
                        status, result.tool, result.version, result.dataset,
                        result.runtime_s, result.output_dir)

    summary_path = write_summary(results, output_dir)
    n_skip = sum(r.skipped for r in results)
    n_ok   = sum(r.success and not r.skipped for r in results)
    n_fail = sum(not r.success for r in results)
    logger.info("Done. %d ran OK, %d skipped, %d failed. Summary: %s",
                n_ok, n_skip, n_fail, summary_path)

    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
