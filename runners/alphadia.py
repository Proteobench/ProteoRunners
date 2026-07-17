"""alphaDIA runner.

Supported search_params keys:
  fdr_psm (→ fdr.fdr), match_between_runs (→ search.mbr_step_enabled),
  normalize (→ search_output.normalize_directlfq),
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods, variable_mods,
  min_charge, max_charge, precursor_mz_range, fragment_mz_range

Tool-specific `extra:` keys (alphaDIA only):
  library, precursor_mass_tolerance_ppm_initial (→ search_initial.ms1_tolerance),
  fragment_mass_tolerance_ppm_initial (→ search_initial.ms2_tolerance),
  transfer_learning (→ general.transfer_step_enabled)

Not mapped (alphaDIA has no separate peptide/protein FDR; no max_mods_per_peptide config key):
  fdr_peptide, fdr_protein, max_mods_per_peptide
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .base import DIA, ENZYME_MAP, MOD_REGISTRY, BaseRunner


class AlphaDIARunner(BaseRunner):
    SUPPORTED_ACQUISITIONS = (DIA,)

    @property
    def tool_name(self) -> str:
        return "alphadia"

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        import shutil
        import subprocess
        cmd = self.version_cfg.get("command", "alphadia")
        if not (shutil.which(cmd) or Path(cmd).exists()):
            found = shutil.which("alphadia")
            if found:
                errors.append(
                    f"alphaDIA command not found at {cmd!r}. "
                    f"Found 'alphadia' on PATH at {found} — update 'command:' under "
                    f"tools > alphadia > versions > id: {self.version_id} in config.yaml."
                )
            else:
                errors.append(
                    f"alphaDIA command not found: {cmd}. "
                    "Install with: pip install alphadia   then run: which alphadia"
                )
            return errors
        # Verify the binary actually starts before the slower peptdeep check.
        r_ver = subprocess.run([cmd, "--version"], capture_output=True, timeout=30)
        if r_ver.returncode != 0:
            stderr = (r_ver.stderr or r_ver.stdout).decode(errors="replace")[-500:]
            errors.append(
                f"alphaDIA binary exists but failed to start — Python environment may be broken:\n{stderr}\n"
                f"Fix: activate the alphaDIA environment and run: pip install alphadia"
            )
            return errors
        # Pre-download peptdeep models so parallel jobs don't race on the same cache file.
        python = str(Path(cmd).parent / "python")
        r = subprocess.run(
            [python, "-c", "from peptdeep.pretrained_models import ModelManager; ModelManager()"],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            errors.append(
                f"alphaDIA: peptdeep model download/check failed:\n{r.stderr.decode(errors='replace')[-500:]}"
            )
        return errors

    def map_params(self) -> dict:
        sp = self.search_params
        enzyme_key = sp.get("enzyme", "trypsin")
        enzyme_info = ENZYME_MAP.get(enzyme_key, ENZYME_MAP["trypsin"])

        fixed_mods: list[str] = []
        for m in sp.get("fixed_mods", []):
            if m in MOD_REGISTRY:
                fixed_mods.append(MOD_REGISTRY[m]["alphadia"])

        var_mods: list[str] = []
        for m in sp.get("variable_mods", []):
            if m in MOD_REGISTRY:
                var_mods.extend(MOD_REGISTRY[m]["alphadia"].split(";"))

        precursor_mz = sp.get("precursor_mz_range", [400.0, 1200.0])
        fragment_mz  = sp.get("fragment_mz_range",  [200.0, 2000.0])

        return {
            "enzyme":          enzyme_info["alphadia"],
            "missed_cleavages": sp.get("missed_cleavages", 2),
            "precursor_len":   [sp.get("min_peptide_length", 7), sp.get("max_peptide_length", 30)],
            "precursor_charge": [sp.get("min_charge", 2), sp.get("max_charge", 4)],
            "precursor_mz":    precursor_mz,
            "fragment_mz":     fragment_mz,
            "fdr_psm":         sp.get("fdr_psm", 0.01),
            "fdr_peptide":      sp.get("fdr_peptide", 0.01),
            "fdr_protein":      sp.get("fdr_protein", 0.01),
            "max_var_mod_num": sp.get("max_mods_per_peptide", 2),
            "fixed_mods":      fixed_mods,
            "var_mods":        var_mods,
            "precursor_tol":   sp.get("precursor_mass_tolerance_ppm", 20),
            "fragment_tol":    sp.get("fragment_mass_tolerance_ppm", 20),
            "mbr":             sp.get("match_between_runs", False),
            "normalize_lfq":   sp.get("normalize", True),
        }
        
    def _decide_fdr_group_level(self, p: dict) -> str:
        """Determine the FDR group level for alphaDIA based on the search parameters."""
        if p["fdr_protein"] is not None:
            return "proteins", p["fdr_protein"]
        elif p["fdr_peptide"] is not None:
            return "peptides", p["fdr_peptide"]
        else:
            return "precursors", p["fdr_psm"]

    def _write_alphadia_config(self, input_files: list[Path], fasta: Path, output_dir: Path) -> Path:
        p = self.map_params()
        threads = self.global_cfg.get("threads_per_job", 16)
        library = (self.extra or {}).get("library", "")
        fdr_group_level, fdr_value = self._decide_fdr_group_level(p)

        cfg: dict = {
            "raw_paths":        [str(f) for f in input_files],
            "fasta_paths":      [str(fasta)],
            "output_directory": str(output_dir),
            "library_path":     library if library else None,
            "general": {
                "thread_count":      threads,
                "mbr_step_enabled":  p["mbr"],
                "transfer_step_enabled": bool(self.extra.get("transfer_learning", False)),
            },
            "library_prediction": {
                "enabled":                not bool(library),
                "enzyme":                 p["enzyme"],
                "missed_cleavages":       p["missed_cleavages"],
                "fixed_modifications":    ";".join(p["fixed_mods"]),
                "variable_modifications": ";".join(p["var_mods"]),
                "max_var_mod_num":        p["max_var_mod_num"],
                "precursor_len":          p["precursor_len"],
                "precursor_charge":       p["precursor_charge"],
                "precursor_mz":           p["precursor_mz"],
                "fragment_mz":            p["fragment_mz"],
            },
            # target_ms1/ms2_tolerance: final search tolerances that AlphaDIA logs and
            # ProteoBench reads back (CONFIG_KEY_MAPPER). Set explicitly so calibration
            # does not override the benchmark value.
            "search": {
                "target_ms1_tolerance": p["precursor_tol"],
                "target_ms2_tolerance": p["fragment_tol"],
                # Rust backend does not support Bruker .d format; fall back to Python.
                "extraction_backend": "python" if self.dataset_cfg.get("format") == "d" else "rust",
            },
            "fdr": {"fdr": fdr_value, "group_level": fdr_group_level},
            "search_output": {"normalize_directlfq": p["normalize_lfq"]},
        }

        # Optional tool-specific initial (pre-calibration) tolerances from the
        # alphadia `extra:` block. Only emitted when set, so AlphaDIA's own
        # defaults apply otherwise.
        initial = {
            k: v for k, v in (
                ("ms1_tolerance", self.extra.get("precursor_mass_tolerance_ppm_initial")),
                ("ms2_tolerance", self.extra.get("fragment_mass_tolerance_ppm_initial")),
            ) if v is not None
        }
        if initial:
            cfg["search_initial"] = initial

        cfg = {k: v for k, v in cfg.items() if v is not None}

        config_path = output_dir / "alphadia_config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, sort_keys=False)
        return config_path

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        config_path = self._write_alphadia_config(input_files, fasta, output_dir)
        command = self.version_cfg.get("command", "alphadia")
        return [command, "--config", str(config_path)]
