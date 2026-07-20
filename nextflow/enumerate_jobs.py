#!/usr/bin/env python3
"""Enumerate enabled ProteoBench jobs for the Nextflow pipeline.

Outputs one JSON object per line (NDJSON). Each object has 'tool', 'version',
and 'dataset' keys that identify one search job.

Usage:
    python enumerate_jobs.py --config /path/to/config.yaml [--tool diann] [--dataset HYE_Astral]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from runners import RUNNER_MAP


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def enumerate_jobs(cfg: dict, tool_filter=None, dataset_filter=None) -> list[dict]:
    global_cfg    = cfg.get("global") or {}
    search_params = cfg.get("search_params") or {}
    datasets      = cfg.get("datasets") or {}
    tools         = cfg.get("tools") or {}

    jobs: list[dict] = []
    for tool_name, tool_cfg in tools.items():
        if tool_filter and tool_name != tool_filter:
            continue

        RunnerClass = RUNNER_MAP.get(tool_name)
        if RunnerClass is None:
            print(f"WARNING: no runner for tool '{tool_name}'; skipping.", file=sys.stderr)
            continue

        for version_cfg in tool_cfg.get("versions", []):
            if not version_cfg.get("enabled", False):
                continue

            for dataset_name in tool_cfg.get("datasets", []):
                if dataset_filter and dataset_name != dataset_filter:
                    continue
                if dataset_name not in datasets:
                    print(
                        f"WARNING: dataset '{dataset_name}' listed under '{tool_name}' not found; skipping.",
                        file=sys.stderr,
                    )
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
                    continue

                jobs.append({
                    "tool":    tool_name,
                    "version": str(version_cfg["id"]),
                    "dataset": dataset_name,
                })

    return jobs


def main():
    parser = argparse.ArgumentParser(description="List enabled ProteoBench jobs as NDJSON.")
    parser.add_argument("--config",  type=Path, required=True)
    parser.add_argument("--tool",    default=None, help="Filter to a single tool name")
    parser.add_argument("--dataset", default=None, help="Filter to a single dataset name")
    args = parser.parse_args()

    cfg  = load_config(args.config)
    jobs = enumerate_jobs(cfg, tool_filter=args.tool, dataset_filter=args.dataset)
    for job in jobs:
        print(json.dumps(job))


if __name__ == "__main__":
    main()
