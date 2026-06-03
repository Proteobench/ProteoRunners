"""Validate config.yaml structure and paths before any runner is started.

Call validate_config() immediately after load_config() in run_proteobench.py.
Returns a list of human-readable error strings; an empty list means all checks passed.
"""

from __future__ import annotations

import shutil
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
# Fields that must be positive numbers
_TOL_FIELDS = ("precursor_mass_tolerance_ppm", "fragment_mass_tolerance_ppm")


def _suggest_path(path_str: str) -> str:
    """If path_str doesn't exist but its basename is on PATH, suggest it."""
    name = Path(path_str).name
    found = shutil.which(name)
    if found:
        return f" ('{name}' found on PATH at {found} — update config.yaml to use this path)"
    return ""


def validate_config(cfg: dict, config_path: Path) -> list[str]:
    errors: list[str] = []

    # 1. Required top-level sections
    for section in ("global", "search_params", "datasets", "tools"):
        if section not in cfg:
            errors.append(
                f"Missing required section '{section}' in {config_path}. "
                f"Check that config.yaml has a '{section}:' block."
            )
    if errors:
        # Cannot safely continue without the basic structure
        return errors

    _validate_global(cfg["global"], config_path, errors)
    _validate_search_params(cfg["search_params"], config_path, errors)
    _validate_datasets(cfg["datasets"], config_path, errors)
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

    dotnet = g.get("dotnet", "dotnet")
    if dotnet and dotnet != "dotnet" and not Path(dotnet).exists():
        hint = _suggest_path(dotnet)
        errors.append(
            f"global.dotnet not found: {dotnet}{hint}. "
            "Set to 'dotnet' if it is on your PATH, or provide the full path."
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
                if float(val) <= 0:
                    errors.append(
                        f"search_params.{field} must be a positive number; got {val!r}."
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


def _validate_datasets(datasets: dict, config_path: Path, errors: list[str]) -> None:
    for ds_name, ds in datasets.items():
        if not isinstance(ds, dict):
            errors.append(f"datasets.{ds_name}: expected a mapping, got {type(ds).__name__}.")
            continue
        prefix = f"datasets > {ds_name}"

        # Required keys
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
            elif Path(val).is_absolute() and not Path(val).exists():
                errors.append(
                    f"{prefix}: '{key}' path does not exist: {val}. "
                    "Is the data directory mounted? Check 'path:' in config.yaml."
                    if key == "path" else
                    f"{prefix}: FASTA file does not exist: {val}. "
                    f"Check 'fasta:' under datasets > {ds_name} in config.yaml."
                )

        fmt = ds.get("format", "")
        if fmt and fmt not in VALID_FORMATS:
            errors.append(
                f"{prefix}: 'format' is {fmt!r} but must be one of: "
                f"{', '.join(sorted(VALID_FORMATS))}."
            )

        acq = ds.get("acquisition", "").upper()
        if ds.get("acquisition") and acq not in VALID_ACQUISITIONS:
            errors.append(
                f"{prefix}: 'acquisition' is {ds['acquisition']!r} but must be 'DDA' or 'DIA'."
            )


def _validate_tools(
    tools: dict, datasets: dict, config_path: Path, errors: list[str]
) -> None:
    dataset_names = set(datasets.keys())

    for tool_name, tool_cfg in tools.items():
        if not isinstance(tool_cfg, dict):
            continue
        prefix = f"tools > {tool_name}"

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

            enabled = ver.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                errors.append(
                    f"{ver_prefix}: 'enabled' should be true or false (boolean), "
                    f"got {enabled!r}. Remove quotes if you used a string."
                )

            if not ver.get("enabled", False):
                continue  # skip path checks for disabled versions

            _validate_tool_binary(tool_name, ver, ver_prefix, errors)


def _validate_tool_binary(
    tool_name: str, ver: dict, ver_prefix: str, errors: list[str]
) -> None:
    """Check that the binary/dir/command for an enabled tool version exists."""

    if tool_name == "diann":
        binary = ver.get("binary", "")
        if not binary:
            errors.append(f"{ver_prefix}: 'binary' is missing. Set the path to the DIA-NN executable.")
        elif "CHANGE_ME" in binary:
            errors.append(
                f"{ver_prefix}: 'binary' still contains 'CHANGE_ME'. "
                "Run: python setup.py --download-diann   to download automatically."
            )
        elif not Path(binary).exists():
            hint = _suggest_path(binary)
            errors.append(
                f"{ver_prefix}: DIA-NN binary not found: {binary}{hint}. "
                "Check 'binary:' under tools > diann > versions in config.yaml. "
                "Run: python setup.py --download-diann   to download automatically."
            )

    elif tool_name == "alphadia":
        command = ver.get("command", "")
        if not command:
            errors.append(
                f"{ver_prefix}: 'command' is missing. "
                "Install AlphaDIA with: pip install alphadia   then run: which alphadia"
            )
        elif "CHANGE_ME" in command:
            errors.append(
                f"{ver_prefix}: 'command' still contains 'CHANGE_ME'. "
                "Run 'which alphadia' after installation and set that path here."
            )
        elif not Path(command).exists():
            found = shutil.which("alphadia")
            if found:
                errors.append(
                    f"{ver_prefix}: AlphaDIA command not found at {command}. "
                    f"Found 'alphadia' on PATH at {found} — update config.yaml."
                )
            else:
                errors.append(
                    f"{ver_prefix}: AlphaDIA command not found: {command}. "
                    "Install with: pip install alphadia"
                )

    elif tool_name == "sage":
        binary = ver.get("binary", "")
        source_dir = ver.get("source_dir", "")
        if not binary and not source_dir:
            errors.append(
                f"{ver_prefix}: set either 'binary' (compiled path) or 'source_dir' "
                "(Sage git repository for compilation via setup.py --sage-only)."
            )
        elif binary and "CHANGE_ME" in binary:
            errors.append(
                f"{ver_prefix}: 'binary' still contains 'CHANGE_ME'. "
                "Set 'source_dir' to the Sage git repo and run: python setup.py --sage-only"
            )
        elif binary and not Path(binary).exists():
            errors.append(
                f"{ver_prefix}: Sage binary not found: {binary}. "
                "Compile it with: python setup.py --sage-only"
            )

    elif tool_name == "fragpipe":
        fp_dir = ver.get("dir", "")
        msfragger_jar = ver.get("msfragger_jar", "")
        if not fp_dir:
            errors.append(f"{ver_prefix}: 'dir' is missing. Set it to the FragPipe installation directory.")
        elif "CHANGE_ME" in fp_dir:
            errors.append(
                f"{ver_prefix}: 'dir' still contains 'CHANGE_ME'. "
                "Download FragPipe from https://github.com/Nesvilab/FragPipe/releases"
            )
        elif not Path(fp_dir).is_dir():
            errors.append(
                f"{ver_prefix}: FragPipe directory not found: {fp_dir}. "
                "Check 'dir:' under tools > fragpipe > versions in config.yaml."
            )
        if not msfragger_jar:
            errors.append(
                f"{ver_prefix}: 'msfragger_jar' is empty. "
                "Run: python setup.py --accept-license   to download MSFragger automatically."
            )
        elif "CHANGE_ME" in msfragger_jar:
            errors.append(
                f"{ver_prefix}: 'msfragger_jar' still contains 'CHANGE_ME'. "
                "Run: python setup.py --accept-license"
            )
        elif not Path(msfragger_jar).exists():
            errors.append(
                f"{ver_prefix}: MSFragger JAR not found: {msfragger_jar}. "
                "Run: python setup.py --accept-license"
            )

    elif tool_name == "maxquant":
        mq_dir = ver.get("dir", "")
        if not mq_dir:
            errors.append(f"{ver_prefix}: 'dir' is missing. Set it to the MaxQuant installation directory.")
        elif "CHANGE_ME" in mq_dir:
            errors.append(
                f"{ver_prefix}: 'dir' still contains 'CHANGE_ME'. "
                "Download MaxQuant from https://www.maxquant.org/ and extract the zip."
            )
        elif not Path(mq_dir).is_dir():
            errors.append(
                f"{ver_prefix}: MaxQuant directory not found: {mq_dir}. "
                "Check 'dir:' under tools > maxquant > versions in config.yaml."
            )

    elif tool_name == "metamorpheus":
        mm_dir = ver.get("dir", "")
        if not mm_dir:
            errors.append(f"{ver_prefix}: 'dir' is missing. Set it to the MetaMorpheus installation directory.")
        elif "CHANGE_ME" in mm_dir:
            errors.append(
                f"{ver_prefix}: 'dir' still contains 'CHANGE_ME'. "
                "Download MetaMorpheus from https://github.com/smith-chem-wisc/MetaMorpheus/releases"
            )
        elif not Path(mm_dir).is_dir():
            errors.append(
                f"{ver_prefix}: MetaMorpheus directory not found: {mm_dir}. "
                "Check 'dir:' under tools > metamorpheus > versions in config.yaml."
            )
