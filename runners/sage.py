"""Sage runner.

Supported search_params keys:
  fdr_psm (→ protein_grouping_peptide_fdr; Sage uses one FDR threshold),
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods (→ static_mods), variable_mods,
  max_mods_per_peptide (→ database.max_variable_mods),
  min_charge, max_charge, fragment_mz_range,
  match_between_runs (→ quant.lfq.combine_charge_states; Sage LFQ performs MBR-like alignment)

Not mapped (no separate concept in Sage):
  fdr_peptide, fdr_protein, precursor_mz_range
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .base import DDA, ENZYME_MAP, MOD_REGISTRY, BaseRunner

logger = logging.getLogger(__name__)


def _compile_sage(source_dir: Path, git_tag: str = "") -> bool:
    if git_tag:
        logger.info("Checking out Sage tag %s", git_tag)
        r = subprocess.run(["git", "-C", str(source_dir), "checkout", git_tag], capture_output=True)
        if r.returncode != 0:
            logger.error("git checkout %s failed: %s", git_tag, r.stderr.decode())
            return False

    logger.info("Compiling Sage in %s ...", source_dir)
    r = subprocess.run(
        ["cargo", "build", "--release", "--manifest-path", str(source_dir / "Cargo.toml")],
        capture_output=False,
    )
    return r.returncode == 0


class SageRunner(BaseRunner):
    SUPPORTED_ACQUISITIONS = (DDA,)

    @property
    def tool_name(self) -> str:
        return "sage"

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        binary = Path(self.version_cfg["binary"])
        source_dir = self.version_cfg.get("source_dir", "")

        if not binary.exists():
            if source_dir and Path(source_dir).exists():
                git_tag = self.version_cfg.get("git_tag", "")
                logger.info("Sage binary not found; compiling from %s", source_dir)
                ok = _compile_sage(Path(source_dir), git_tag)
                if not ok or not binary.exists():
                    errors.append(f"Sage compilation failed; binary not found at {binary}")
            else:
                errors.append(f"Sage binary not found and no source_dir set: {binary}")

        fmt = self.dataset_cfg["format"]
        if fmt not in ("mzml", "mgf"):
            errors.append(
                f"Sage requires mzML input; dataset format is '{fmt}'. "
                "Convert RAW/.d files to mzML first (e.g. with ThermoRawFileParser or msconvert)."
            )
        return errors

    def map_params(self) -> dict:
        sp = self.search_params
        enzyme_key = sp.get("enzyme", "trypsin")
        enzyme_info = ENZYME_MAP.get(enzyme_key, ENZYME_MAP["trypsin"])

        static_mods: dict[str, float] = {}
        for m in sp.get("fixed_mods", []):
            if m in MOD_REGISTRY:
                entry = MOD_REGISTRY[m]
                for res in entry["sage_residues"]:
                    static_mods[res] = entry["sage_mass"]

        variable_mods: list[dict] = []
        for m in sp.get("variable_mods", []):
            if m in MOD_REGISTRY:
                entry = MOD_REGISTRY[m]
                variable_mods.append({"mass": entry["sage_mass"], "residues": entry["sage_residues"]})

        fragment_mz = sp.get("fragment_mz_range", [200.0, 2000.0])

        return {
            "enzyme_cleave_at":   enzyme_info["sage_cleave_at"],
            "enzyme_restrict":    enzyme_info.get("sage_restrict"),
            "missed_cleavages":   sp.get("missed_cleavages", 2),
            "min_len":            sp.get("min_peptide_length", 7),
            "max_len":            sp.get("max_peptide_length", 30),
            "precursor_tol_ppm":  sp.get("precursor_mass_tolerance_ppm", 20),
            "fragment_tol_ppm":   sp.get("fragment_mass_tolerance_ppm", 20),
            "precursor_charge":   [sp.get("min_charge", 2), sp.get("max_charge", 4)],
            "fragment_min_mz":    fragment_mz[0],
            "fragment_max_mz":    fragment_mz[1],
            "fdr":                sp.get("fdr_psm", 0.01),
            "max_variable_mods":  sp.get("max_mods_per_peptide", 3),
            "static_mods":        static_mods,
            "variable_mods":      variable_mods,
            "mbr":                sp.get("match_between_runs", False),
        }

    def _write_sage_config(self, input_files: list[Path], fasta: Path, output_dir: Path) -> Path:
        p = self.map_params()
        threads = self.global_cfg.get("threads_per_job", 16)

        enzyme: dict = {
            "cleave_at":       p["enzyme_cleave_at"],
            "missed_cleavages": p["missed_cleavages"],
            "c_terminal":     p["enzyme_c_terminal"] if p.get("enzyme_c_terminal") else False,
        }
        if p.get("enzyme_restrict"):
            enzyme["restrict"] = p["enzyme_restrict"]

        cfg: dict = {
            "database": {
                "fasta":              str(fasta),
                "enzyme":             enzyme,
                "peptide_min_len":    p["min_len"],
                "peptide_max_len":    p["max_len"],
                "fragment_min_mz":    p["fragment_min_mz"],
                "fragment_max_mz":    p["fragment_max_mz"],
                "static_mods":        p["static_mods"],
                "variable_mods":      p["variable_mods"],
                "max_variable_mods":  p["max_variable_mods"],
                "generate_decoys":    True,
                "decoy_tag":          "rev_",
            },
            "precursor_tol":              {"ppm": [-p["precursor_tol_ppm"], p["precursor_tol_ppm"]]},
            "fragment_tol":               {"ppm": [-p["fragment_tol_ppm"],  p["fragment_tol_ppm"]]},
            "precursor_charge":           p["precursor_charge"],
            "protein_grouping_peptide_fdr": p["fdr"],
            "output_directory":           str(output_dir),
            "mzml_paths":                 [str(f) for f in input_files],
        }

        if p["mbr"]:
            cfg["quant"] = {"lfq": True, "lfq_settings": {"combine_charge_states": True}}

        extra = self.extra or {}
        if extra.get("write_pin"):
            cfg["write_pin"] = True

        config_path = output_dir / "sage_config.json"
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=2)
        return config_path

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        config_path = self._write_sage_config(input_files, fasta, output_dir)
        binary  = str(self.version_cfg["binary"])
        threads = self.global_cfg.get("threads_per_job", 16)

        cmd = [binary, "--batch-size", str(threads), str(config_path)]

        extra = self.extra or {}
        if extra.get("parquet"):
            cmd.append("--parquet")
        if extra.get("write_pin"):
            cmd.append("--write-pin")

        return cmd
