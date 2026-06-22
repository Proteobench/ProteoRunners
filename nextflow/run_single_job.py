#!/usr/bin/env python3
"""Run a single ProteoBench job (one tool × version × dataset).

Called by the Nextflow pipeline once per job. The result is always written as
JSON to stdout so Nextflow can capture it even when the job fails gracefully.
The process always exits 0; success/failure is recorded in the JSON 'success'
field. Only hard configuration errors (missing config file, unknown tool, etc.)
exit non-zero.

Usage:
    python run_single_job.py --config /path/to/config.yaml \\
        --tool diann --version 2.5.0 --dataset HYE_Astral [--no-preflight]
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from runners import RUNNER_MAP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _emit(tool, version, dataset, *, success, skipped=False, runtime_s=0.0,
          output_dir="", error_msg="", stderr_log=""):
    """Write a JSON result record to stdout."""
    print(json.dumps({
        "tool":       tool,
        "version":    version,
        "dataset":    dataset,
        "success":    success,
        "skipped":    skipped,
        "runtime_s":  round(float(runtime_s), 1),
        "output_dir": str(output_dir),
        "error_msg":  error_msg,
        "stderr_log": str(stderr_log) if stderr_log else "",
    }))


def main():
    parser = argparse.ArgumentParser(description="Run one ProteoBench job.")
    parser.add_argument("--config",       type=Path, required=True)
    parser.add_argument("--tool",         required=True)
    parser.add_argument("--version",      required=True)
    parser.add_argument("--dataset",      required=True)
    parser.add_argument("--no-preflight", action="store_true",
                        help="Skip preflight checks before running")
    args = parser.parse_args()

    # --- config loading (hard errors exit non-zero) ---
    if not args.config.exists():
        sys.exit(f"Config not found: {args.config}")

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    global_cfg    = cfg.get("global", {})
    search_params = cfg.get("search_params", {})
    datasets      = cfg.get("datasets", {})
    tool_cfg      = cfg.get("tools", {}).get(args.tool)

    if not tool_cfg:
        _emit(args.tool, args.version, args.dataset,
              success=False, error_msg=f"Tool '{args.tool}' not found in config")
        sys.exit(0)

    version_cfg = next(
        (v for v in tool_cfg.get("versions", []) if str(v["id"]) == args.version),
        None,
    )
    if not version_cfg:
        _emit(args.tool, args.version, args.dataset,
              success=False, error_msg=f"Version '{args.version}' not found in config")
        sys.exit(0)

    dataset_cfg = datasets.get(args.dataset)
    if not dataset_cfg:
        _emit(args.tool, args.version, args.dataset,
              success=False, error_msg=f"Dataset '{args.dataset}' not found in config")
        sys.exit(0)

    RunnerClass = RUNNER_MAP.get(args.tool)
    if RunnerClass is None:
        _emit(args.tool, args.version, args.dataset,
              success=False, error_msg=f"No runner class for tool '{args.tool}'")
        sys.exit(0)

    runner = RunnerClass(
        tool_cfg=tool_cfg,
        dataset_name=args.dataset,
        dataset_cfg=dataset_cfg,
        version_cfg=version_cfg,
        global_cfg=global_cfg,
        search_params=search_params,
    )

    # --- optional preflight (mirrors Python pipeline behaviour) ---
    if not args.no_preflight:
        errors = runner.preflight_check()
        if errors:
            for err in errors:
                logger.error("[%s v%s / %s] preflight FAIL: %s",
                             args.tool, args.version, args.dataset, err)
            _emit(args.tool, args.version, args.dataset,
                  success=False, error_msg="Preflight failed: " + "; ".join(errors))
            sys.exit(0)

    # --- run ---
    result = runner.run()

    _emit(
        result.tool, result.version, result.dataset,
        success=result.success,
        skipped=result.skipped,
        runtime_s=result.runtime_s,
        output_dir=result.output_dir,
        error_msg=result.error_msg,
        stderr_log=result.stderr_log,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
