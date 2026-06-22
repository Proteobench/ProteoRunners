#!/usr/bin/env python3
"""Collect run_single_job.py JSON results into a TSV summary.

Produces the same column layout as the Python pipeline's write_summary().

Usage:
    python write_summary.py result1.json result2.json ... > run_summary_nf.tsv
"""
import csv
import json
import sys

FIELDS = ["tool", "version", "dataset", "success", "skipped", "runtime_s", "output_dir", "error_msg"]

writer = csv.DictWriter(
    sys.stdout, fieldnames=FIELDS, delimiter="\t", extrasaction="ignore"
)
writer.writeheader()

for fname in sorted(sys.argv[1:]):
    try:
        with open(fname) as f:
            row = json.load(f)
        writer.writerow(row)
    except Exception as exc:
        print(f"WARNING: could not read {fname}: {exc}", file=sys.stderr)
