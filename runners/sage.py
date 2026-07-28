"""Sage runner.

Runs inside the ghcr.io/lazear/sage docker image (pulled by setup.nf); the
in-container binary lives at the path recorded in 'sage_bin' (default /app/sage).

Supported search_params keys:
  fdr_psm (→ protein_grouping_peptide_fdr; Sage uses one FDR threshold),
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods (→ static_mods), variable_mods,
  max_mods_per_peptide (→ database.max_variable_mods),
  min_charge, max_charge, fragment_mz_range

Not mapped (no separate concept in Sage):
  fdr_peptide, fdr_protein, precursor_mz_range, match_between_runs
  (Sage's LFQ always runs with its own MBR-like alignment; not toggable)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .base import DDA, ENZYME_MAP, MOD_REGISTRY, BaseRunner

logger = logging.getLogger(__name__)


class SageRunner(BaseRunner):
    SUPPORTED_ACQUISITIONS = (DDA,)

    @property
    def tool_name(self) -> str:
        return "sage"

    def _sage_bin(self) -> str:
        return self.version_cfg.get("sage_bin", "/app/sage")

    def requires_mzml(self) -> bool:
        # Sage reads mzML or MGF directly; anything else needs mzML redirection.
        return self.dataset_cfg.get("format") != "mgf"

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        errors += self.docker_preflight()
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

        # Sage expects variable_mods as a map of residue -> list of masses,
        # same shape as static_mods (not a list of {mass, residues} objects).
        variable_mods: dict[str, list[float]] = {}
        for m in sp.get("variable_mods", []):
            if m in MOD_REGISTRY:
                entry = MOD_REGISTRY[m]
                for res in entry["sage_residues"]:
                    variable_mods.setdefault(res, []).append(entry["sage_mass"])

        fragment_mz = sp.get("fragment_mz_range", [200.0, 2000.0])

        return {
            "enzyme_cleave_at":   enzyme_info["sage_cleave_at"],
            "enzyme_restrict":    enzyme_info.get("sage_restrict"),
            # sage_c_terminal: True = C-terminal cleavage (trypsin, lysc, …); False = N-terminal (aspn)
            "enzyme_c_terminal":  enzyme_info.get("sage_c_terminal", True),
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

        # min_len/max_len and c_terminal belong inside the enzyme sub-dict; this is
        # what Sage's config format requires and what ProteoBench reads back.
        enzyme: dict = {
            "cleave_at":        p["enzyme_cleave_at"],
            "missed_cleavages": p["missed_cleavages"],
            "min_len":          p["min_len"],
            "max_len":          p["max_len"],
            "c_terminal":       p["enzyme_c_terminal"],
            "semi_enzymatic":   False,
        }
        if p.get("enzyme_restrict"):
            enzyme["restrict"] = p["enzyme_restrict"]

        cfg: dict = {
            "database": {
                "fasta":              str(fasta),
                "enzyme":             enzyme,
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

        cfg["quant"] = {"lfq": True, "lfq_settings": {"combine_charge_states": False}}

        extra = self.extra or {}
        if extra.get("write_pin"):
            cfg["write_pin"] = True

        config_path = output_dir / "sage_config.json"
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=2)
        return config_path

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        config_path = self._write_sage_config(input_files, fasta, output_dir)
        threads = self.global_cfg.get("threads_per_job", 16)

        cmd = self.docker_run_prefix(self.docker_image())
        cmd += [self._sage_bin(), "--batch-size", str(threads), str(config_path)]

        extra = self.extra or {}
        if extra.get("parquet"):
            cmd.append("--parquet")
        if extra.get("write_pin"):
            cmd.append("--write-pin")

        return cmd
