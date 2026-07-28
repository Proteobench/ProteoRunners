#!/usr/bin/env python3
"""Filter a completed job's output_dir down to the files ProteoBench needs on upload.

Called by the Nextflow pipeline once per job, after RUN_JOB. Reads the JSON
record written by run_single_job.py, deletes every file under its output_dir
except the tool's upload-relevant files, then re-emits the JSON unchanged so
it flows on to write_summary.py. No-ops on failed jobs (keep logs for
debugging) or when --unfiltered is passed.

Usage:
    python filter_outputs.py result.json [--unfiltered] > result.json.out
"""
import argparse
import json
import sys
from pathlib import Path

# Basenames of the files ProteoBench actually reads on upload, per tool.
# Sourced from the [upload_info] section of ProteoBench's parse_settings TOMLs
# (proteobench/io/parsing/io_parse_settings/Quant/lfq/**/parse_settings_<tool>.toml)
# and cross-checked against real output trees in results/. alphaDIA's actual
# log file is named log.txt (the TOML's "log_alphadia.txt" is only an example
# name). FragPipe's runner always uses FragPipe's built-in DIA workflows,
# which quantify via DIA-NN (report.tsv/.parquet under dia-quant-output/)
# rather than IonQuant's combined_ion.tsv (DDA only) — verified against a real
# Entrapment_DIA/fragpipe_v24.0 run. sage's datapoint file is lfq.tsv, written
# by Sage's quant.lfq output (now always enabled, see runners/sage.py) — no
# real run in results/ has this yet since existing runs predate that change.
# metamorpheus's params_file upload is "search_task_config.toml + version_result.txt"
# (two files: any TOML settings file, plus a plain-text file containing a
# version/results summary — ProteoBench tells them apart by content, not name,
# see proteobench/io/params/metamorpheus.py:identify_file_type). Our runner
# writes the settings TOML as SearchTask.toml. The version/results summary is
# expected as results.txt, MetaMorpheus's standard top-level output summary
# (matches the shape of ProteoBench/test/params/metamorpheus_version_result.txt)
# — unverified against a real run in results/; confirm the actual filename the
# first time metamorpheus completes a run and adjust here if it differs.
KEEP_BASENAMES = {
    "maxquant":     {"evidence.txt", "mqpar.xml"},
    "diann":        {"report.tsv", "report.parquet", "report.log.txt"},
    "alphadia":     {"precursors.tsv", "precursors.parquet", "precursor.matrix.tsv", "log.txt"},
    "sage":         {"lfq.tsv", "results.json", "sage_config.json"},
    "metamorpheus": {"AllQuantifiedPeaks.tsv", "SearchTask.toml", "results.txt"},
}

KEEP_BASENAMES_FRAGPIPE_BY_ACQUISITION = {
    "DDA": {"combined_ion.tsv", "fragpipe.workflow"},
    "DIA": {"report.tsv", "report.parquet", "fragpipe.workflow"},
}

# Never deleted: the pipeline's own skip/rerun marker (see BaseRunner.run()).
ALWAYS_KEEP = {".done"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_json", type=Path)
    parser.add_argument("--unfiltered", action="store_true",
                        help="Keep all raw tool outputs, do not filter.")
    args = parser.parse_args()

    line = args.result_json.read_text().strip()
    result = json.loads(line)
    print(line)  # pass the job record through unchanged

    if args.unfiltered or not result.get("success"):
        return

    output_dir = Path(result["output_dir"])
    if not output_dir.is_dir():
        return

    tool = result["tool"]
    if tool == "fragpipe":
        keep = KEEP_BASENAMES_FRAGPIPE_BY_ACQUISITION.get(result.get("acquisition"))
    else:
        keep = KEEP_BASENAMES.get(tool)
    if not keep:
        # Unknown tool or (for fragpipe) unrecognized acquisition: we don't know
        # what's safe to delete, so leave the output_dir untouched rather than
        # risk wiping everything.
        return
    keep = keep | ALWAYS_KEEP

    # Deepest paths first so a directory is already empty by the time we reach it.
    for path in sorted(output_dir.rglob("*"), reverse=True):
        if path.is_file():
            if path.name not in keep:
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


if __name__ == "__main__":
    main()
