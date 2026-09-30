"""Validate config.yaml structure and paths before any runner is started.

Call validate_config() immediately after load_config() in run_proteobench.py.
Returns a list of human-readable error strings; an empty list means all checks passed.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Import registries so enzyme/mod names are validated against the same source of truth.
# Guard the import so the validator can still be imported standalone for testing.
try:
    from runners.base import ENZYME_MAP, MOD_REGISTRY
except ImportError:
    ENZYME_MAP: dict = {}
    MOD_REGISTRY: dict = {}

VALID_FORMATS = {"raw", "mzml", "d", "wiff", "mgf"}
VALID_ACQUISITIONS = {"DDA", "DIA"}

# Fields that must be 0 < value <= 1
_FDR_FIELDS = ("fdr_psm", "fdr_peptide", "fdr_protein")
# Fields that must be positive numbers, or exactly 0 meaning "automatic"
# (see BaseRunner.auto_tolerance: each tool then enables its own mass
# calibration, or falls back to its own default tolerance).
_TOL_FIELDS = ("precursor_mass_tolerance_ppm", "fragment_mass_tolerance_ppm")


def _suggest_path(path_str: str) -> str:
    """If path_str doesn't exist but its basename is on PATH, suggest it."""
    name = Path(path_str).name
    found = shutil.which(name)
    if found:
        return f" ('{name}' found on PATH at {found} — update config.yaml to use this path)"
    return ""


CATALOG_PATH = Path(__file__).parent / "nextflow" / "datasets_catalog.yaml"

# setup.nf writes this file into a dataset folder while it downloads and
# extracts it, and removes it once that finished. A folder that still has it is
# a partial download and counts as missing.
INCOMPLETE_MARKER = ".proteorunners_incomplete"


def _enabled(ver) -> bool:
    return isinstance(ver, dict) and ver.get("enabled", False) is True


def used_datasets(cfg: dict) -> list[str]:
    """Dataset names listed by tools that have at least one enabled version."""
    used: list[str] = []
    for tool_cfg in (cfg.get("tools") or {}).values():
        if not isinstance(tool_cfg, dict) or not any(_enabled(v) for v in tool_cfg.get("versions") or []):
            continue
        for name in tool_cfg.get("datasets") or []:
            if name and "CHANGE_ME" not in str(name) and name not in used:
                used.append(name)
    return used


def _resolve(value: str, config_path: Path) -> Path:
    """Relative dataset paths are relative to the directory holding config.yaml."""
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else config_path.resolve().parent / path


def _dataset_on_disk(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir()) and not (path / INCOMPLETE_MARKER).exists()


def missing_downloadable_datasets(cfg: dict, config_path: Path, catalog_path: Path = CATALOG_PATH) -> list[str]:
    """Names of datasets that an enabled tool uses but that are not on disk
    (no entry, CHANGE_ME, a missing or empty path, or a partial download),
    restricted to the ones setup.nf can download from the catalog. Missing
    datasets that are not in the catalog are left to validate_config().
    """
    import yaml

    catalog = (yaml.safe_load(catalog_path.read_text()) if catalog_path.exists() else None) or {}
    datasets = cfg.get("datasets") or {}
    missing: list[str] = []
    for name in used_datasets(cfg):
        if name not in catalog:
            continue
        path_str = str((datasets.get(name) or {}).get("path") or "")
        if not path_str or "CHANGE_ME" in path_str or not _dataset_on_disk(_resolve(path_str, config_path)):
            missing.append(name)
    return missing


def setup_status(cfg: dict, config_path: Path, catalog_path: Path = CATALOG_PATH) -> dict:
    """Everything setup.nf and the proteobench.nf setup gate need to decide what
    to (re)do, as one JSON-serialisable dict:

      tools: {name: {status, problems, fragpipe_jars_missing}}
        status   "complete"   every enabled version has its image and extras
                 "incomplete" at least one enabled version has a problem
                 "disabled"   the tool is configured but no version is enabled
        problems [{id, image, reason, message}] for the enabled versions;
                 reason is "image" (not pulled), "jars" (FragPipe licensed
                 JARs missing) or "config" (image or in-container path unset)
      missing_datasets: see missing_downloadable_datasets()
    """
    tools: dict[str, dict] = {}
    for tool_name, tool_cfg in (cfg.get("tools") or {}).items():
        if not isinstance(tool_cfg, dict):
            continue
        versions = [v for v in tool_cfg.get("versions") or [] if isinstance(v, dict)]
        problems = []
        for i, ver in enumerate(versions):
            if not _enabled(ver):
                continue
            ver_id = str(ver.get("id", f"index {i}"))
            for reason, message in _version_problems(tool_name, ver, f"tools > {tool_name} > id: {ver_id}"):
                problems.append({"id": ver_id, "image": ver.get("image", ""), "reason": reason, "message": message})
        if not any(_enabled(v) for v in versions):
            status = "disabled"
        else:
            status = "incomplete" if problems else "complete"
        jars_missing = False
        if tool_name == "fragpipe":
            jars_missing = any(_missing_fragpipe_jars(v.get("jars_dir", "")) for v in versions) if versions else True
        tools[tool_name] = {"status": status, "problems": problems, "fragpipe_jars_missing": jars_missing}
    return {
        "tools": tools,
        "missing_datasets": missing_downloadable_datasets(cfg, config_path, catalog_path),
    }


def validate_config(cfg: dict, config_path: Path) -> list[str]:
    errors: list[str] = []

    # 1. Required top-level sections
    for section in ("global", "search_params", "datasets", "tools"):
        if cfg.get(section) is None:
            errors.append(
                f"Missing or empty required section '{section}' in {config_path}. "
                f"Check that config.yaml has a '{section}:' block with content under it."
            )
    if errors:
        # Cannot safely continue without the basic structure
        return errors

    _validate_global(cfg["global"], config_path, errors)
    _validate_search_params(cfg["search_params"], config_path, errors)
    _validate_datasets(cfg["datasets"], config_path, errors, set(used_datasets(cfg)))
    _validate_tools(cfg["tools"], cfg["datasets"], config_path, errors)

    return errors


# ── Section validators ────────────────────────────────────────────────────────

def _validate_global(g: dict, config_path: Path, errors: list[str]) -> None:
    output_dir = g.get("output_dir", "")
    if not output_dir:
        errors.append(
            "global.output_dir is empty. Set it to the directory where results should be written."
        )
    elif "CHANGE_ME" in str(output_dir):
        errors.append(
            f"global.output_dir still contains 'CHANGE_ME': {output_dir!r}. "
            "Replace it with a real path on your system."
        )

    if not shutil.which("docker"):
        errors.append(
            "docker is not installed or not on PATH. Every tool now runs in a docker "
            "container — install Docker before running this pipeline."
        )

    for key in ("max_parallel_jobs", "threads_per_job"):
        val = g.get(key)
        if val is not None and (not isinstance(val, int) or val < 1):
            errors.append(f"global.{key} must be a positive integer (got {val!r}).")


def _validate_search_params(sp: dict, config_path: Path, errors: list[str]) -> None:
    enzyme = sp.get("enzyme", "")
    if enzyme and ENZYME_MAP and enzyme not in ENZYME_MAP:
        known = ", ".join(sorted(ENZYME_MAP))
        errors.append(
            f"search_params.enzyme: unknown value {enzyme!r}. "
            f"Supported enzymes: {known}."
        )

    for field in _FDR_FIELDS:
        val = sp.get(field)
        if val is not None:
            try:
                fval = float(val)
                if not (0 < fval <= 1):
                    errors.append(
                        f"search_params.{field} must be between 0 and 1 "
                        f"(e.g. 0.01 for 1% FDR); got {val!r}."
                    )
            except (TypeError, ValueError):
                errors.append(f"search_params.{field} must be a number; got {val!r}.")

    for field in _TOL_FIELDS:
        val = sp.get(field)
        if val is not None:
            try:
                if float(val) < 0:
                    errors.append(
                        f"search_params.{field} must be a positive number, "
                        f"or 0 for automatic calibration; got {val!r}."
                    )
            except (TypeError, ValueError):
                errors.append(f"search_params.{field} must be a number; got {val!r}.")

    if MOD_REGISTRY:
        for mod_key in ("fixed_mods", "variable_mods"):
            for mod in sp.get(mod_key, []):
                if mod not in MOD_REGISTRY:
                    known_mods = ", ".join(sorted(MOD_REGISTRY))
                    errors.append(
                        f"search_params.{mod_key}: unknown modification {mod!r}. "
                        f"Supported modifications: {known_mods}."
                    )


def _validate_datasets(datasets: dict, config_path: Path, errors: list[str], used: set[str]) -> None:
    """Paths are only checked for datasets that an enabled tool uses, so unused
    entries (e.g. the template defaults) never block a run."""
    for ds_name, ds in datasets.items():
        if not isinstance(ds, dict):
            errors.append(f"datasets.{ds_name}: expected a mapping, got {type(ds).__name__}.")
            continue
        prefix = f"datasets > {ds_name}"

        if ds_name in used:
            for key in ("path", "fasta"):
                val = ds.get(key, "")
                if not val:
                    errors.append(
                        f"{prefix}: '{key}' is missing or empty. "
                        f"Set it in config.yaml under datasets > {ds_name}."
                    )
                elif "CHANGE_ME" in str(val):
                    errors.append(
                        f"{prefix}: '{key}' still contains 'CHANGE_ME': {val!r}. "
                        "Replace it with a real path."
                    )
                elif key == "path" and not _dataset_on_disk(_resolve(val, config_path)):
                    errors.append(
                        f"{prefix}: no data found at {_resolve(val, config_path)}. "
                        "Download it (run the pipeline with --setup), fix 'path:' in config.yaml, "
                        f"or remove {ds_name} from the tools' datasets: lists."
                    )
                elif key == "fasta" and not _resolve(val, config_path).is_file():
                    errors.append(
                        f"{prefix}: FASTA file does not exist: {_resolve(val, config_path)}. "
                        f"Check 'fasta:' under datasets > {ds_name} in config.yaml."
                    )

        fmt = ds.get("format", "")
        if fmt and fmt not in VALID_FORMATS:
            errors.append(
                f"{prefix}: 'format' is {fmt!r} but must be one of: "
                f"{', '.join(sorted(VALID_FORMATS))}."
            )

        acq = str(ds.get("acquisition", "")).upper()
        if ds.get("acquisition") and acq not in VALID_ACQUISITIONS:
            errors.append(
                f"{prefix}: 'acquisition' is {ds['acquisition']!r} but must be 'DDA' or 'DIA'."
            )


def _validate_extra_args(value, where: str, errors: list[str]) -> None:
    """extra_args is passed to the tool verbatim, so the only thing worth checking
    is that it can be shell-split at all (a stray quote would otherwise fail the
    job long after the run started)."""
    if value is None or isinstance(value, (list, tuple)):
        return
    if not isinstance(value, str):
        errors.append(f"{where}: should be a string or a list of arguments, got {value!r}.")
        return
    try:
        shlex.split(value)
    except ValueError as exc:
        errors.append(f"{where}: cannot be parsed as command-line arguments ({exc}): {value!r}")


def _validate_tools(
    tools: dict, datasets: dict, config_path: Path, errors: list[str]
) -> None:
    dataset_names = set(datasets.keys())

    for tool_name, tool_cfg in tools.items():
        if not isinstance(tool_cfg, dict):
            continue
        prefix = f"tools > {tool_name}"

        # extra_args is free-form and never interpreted, but it is shell-split, so
        # catch an unbalanced quote here rather than mid-run.
        _validate_extra_args((tool_cfg.get("extra") or {}).get("extra_args"),
                             f"{prefix} > extra > extra_args", errors)

        # Cross-check dataset names
        for ds_name in tool_cfg.get("datasets", []):
            if ds_name not in dataset_names:
                errors.append(
                    f"{prefix}: dataset '{ds_name}' is listed but not defined in the "
                    "datasets section. Check for a typo or add the dataset definition."
                )

        for i, ver in enumerate(tool_cfg.get("versions", [])):
            if not isinstance(ver, dict):
                continue
            ver_id = ver.get("id", f"index {i}")
            ver_prefix = f"{prefix} > id: {ver_id}"

            if "id" not in ver:
                errors.append(f"{prefix}: version entry at index {i} is missing 'id'.")

            _validate_extra_args(ver.get("extra_args"), f"{ver_prefix} > extra_args", errors)

            enabled = ver.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                errors.append(
                    f"{ver_prefix}: 'enabled' should be true or false (boolean), "
                    f"got {enabled!r}. Remove quotes if you used a string."
                )

            if not ver.get("enabled", False):
                continue  # skip path checks for disabled versions

            _validate_tool_docker(tool_name, ver, ver_prefix, errors)


def _docker_image_present(image: str) -> bool:
    r = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return r.returncode == 0


def _missing_fragpipe_jars(jars_dir: str) -> list[str]:
    """Labels of the licensed FragPipe JARs that are not in jars_dir."""
    if not jars_dir or "CHANGE_ME" in str(jars_dir):
        return ["MSFragger", "IonQuant", "diaTracer"]
    d = Path(str(jars_dir)).expanduser()
    found = {p.name.lower() for p in d.glob("*.jar")} if d.is_dir() else set()
    return [label for label, needle in (("MSFragger", "msfragger"), ("IonQuant", "ionquant"), ("diaTracer", "diatracer"))
            if not any(needle in n for n in found)]


def _version_problems(tool_name: str, ver: dict, ver_prefix: str) -> list[tuple[str, str]]:
    """(reason, message) for everything missing from one enabled tool version's
    docker setup. reason is "image", "jars" or "config" (see setup_status)."""
    problems: list[tuple[str, str]] = []
    image = str(ver.get("image") or "")
    if not image or "CHANGE_ME" in image:
        return [("config", f"{ver_prefix}: 'image' is missing or still 'CHANGE_ME'. Run the pipeline with --setup to set this tool up again.")]
    if shutil.which("docker") and not _docker_image_present(image):
        problems.append(("image", f"{ver_prefix}: docker image not pulled locally: {image}. "
                                  "The pipeline offers to pull it on its next run."))

    path_key = {"diann": "diann_bin", "fragpipe": "fragpipe_root"}.get(tool_name)
    if path_key and (not ver.get(path_key) or "CHANGE_ME" in str(ver.get(path_key))):
        problems.append(("config", f"{ver_prefix}: '{path_key}' is missing or still 'CHANGE_ME'. "
                                   "Run the pipeline with --setup to set this tool up again."))

    if tool_name == "fragpipe":
        jars_dir = ver.get("jars_dir", "")
        if not jars_dir or "CHANGE_ME" in str(jars_dir):
            problems.append(("config", f"{ver_prefix}: 'jars_dir' is missing or still 'CHANGE_ME'. "
                                       "Run the pipeline with --setup to collect the licensed JARs."))
            return problems
        for label in _missing_fragpipe_jars(jars_dir):
            problems.append(("jars", f"{ver_prefix}: {label} JAR not found in jars_dir ({ver.get('jars_dir', '')}). "
                                     "Run the pipeline with --setup to add it (or set enabled: false)."))
    return problems


def _validate_tool_docker(
    tool_name: str, ver: dict, ver_prefix: str, errors: list[str]
) -> None:
    """Check that the docker image (and any tool-specific extras) for an enabled version exist."""
    errors.extend(message for _reason, message in _version_problems(tool_name, ver, ver_prefix))


# ── CLI: used by proteobench.nf and setup.nf ─────────────────────────────────
# Exit codes: 0 = no problems, 1 = problems found (printed one per line),
# 2 = config.yaml could not be read, 3 = pyyaml is not installed. The callers
# stop on 2 and 3 instead of treating an empty answer as "all fine".

if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Check config.yaml completeness.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH,
                        help="Dataset catalog used to decide which missing datasets can be downloaded.")
    parser.add_argument(
        "--setup-status", action="store_true",
        help="Print the per-tool docker setup status and the missing downloadable datasets as JSON.",
    )
    args = parser.parse_args()

    try:
        import yaml
    except ImportError:
        print("Python package 'pyyaml' is not installed for this python3 "
              f"({sys.executable}). Activate the pipeline's environment "
              "(conda activate proteobench-pipeline) or run: python3 -m pip install pyyaml",
              file=sys.stderr)
        sys.exit(3)

    if not args.config.exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(2)
    try:
        loaded_cfg = yaml.safe_load(args.config.read_text())
    except yaml.YAMLError as exc:
        print(f"{args.config} is not valid YAML. The problem is here:\n{exc}\n"
              f"A copy of the previous version may be in {args.config}.bak", file=sys.stderr)
        sys.exit(2)
    if not isinstance(loaded_cfg, dict):
        print(f"{args.config} is empty or not a YAML mapping.", file=sys.stderr)
        sys.exit(2)

    if args.setup_status:
        print(json.dumps(setup_status(loaded_cfg, args.config, args.catalog)))
        sys.exit(0)

    found_errors = validate_config(loaded_cfg, args.config)
    for e in found_errors:
        print(e)
    sys.exit(1 if found_errors else 0)
