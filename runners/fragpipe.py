"""FragPipe runner.

Supported search_params keys:
  fdr_psm, fdr_peptide, fdr_protein, match_between_runs,
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods, variable_mods, max_mods_per_peptide,
  min_charge, max_charge, precursor_mz_range, fragment_mz_range
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import DDA, DIA, ENZYME_MAP, MOD_REGISTRY, BaseRunner

logger = logging.getLogger(__name__)

# MSFragger enzyme: (dropdown_name, cut_residues, nocut_residues, direction)
_FP_ENZYME: dict[str, tuple[str, str, str, str]] = {
    "trypsin":      ("trypsin", "KR", "P", "C"),
    "trypsin/p":    ("stricttrypsin", "KR", "", "C"),
    "lysc":         ("lysc",         "K",  "P", "C"),
    "gluc":         ("gluc",         "DE", "P", "C"),
    "chymotrypsin": ("chymotrypsin",  "FLWY", "P", "C"),
    "aspn":         ("aspn",         "D",  "", "N"),
    "argc":         ("argc",         "R",  "P", "C"),
    "non-specific": ("nonspecific",   "@",  "",  "C"),
}

# Fixed mod table residue order (MSFragger canonical order)
_FIXED_MOD_RESIDUES: list[tuple[str, str | None]] = [
    ("C-Term Peptide", None),
    ("N-Term Peptide", None),
    ("C-Term Protein", None),
    ("N-Term Protein", None),
    ("G (glycine)",    "G"),
    ("A (alanine)",    "A"),
    ("S (serine)",     "S"),
    ("P (proline)",    "P"),
    ("V (valine)",     "V"),
    ("T (threonine)",  "T"),
    ("C (cysteine)",   "C"),
    ("L (leucine)",    "L"),
    ("I (isoleucine)", "I"),
    ("N (asparagine)", "N"),
    ("D (aspartic acid)", "D"),
    ("Q (glutamine)",  "Q"),
    ("K (lysine)",     "K"),
    ("E (glutamic acid)", "E"),
    ("M (methionine)", "M"),
    ("H (histidine)",  "H"),
    ("F (phenylalanine)", "F"),
    ("R (arginine)",   "R"),
    ("Y (tyrosine)",   "Y"),
    ("W (tryptophan)", "W"),
    ("B ", None),
    ("J",  None),
    ("O",  None),
    ("U",  None),
    ("X",  None),
    ("Z",  None),
]

# Maps MOD_REGISTRY mod names to MSFragger variable-mod residue string notation
_VAR_MOD_RESIDUES: dict[str, str] = {
    "Oxidation (M)":            "M",
    "Phospho (STY)":            "STY",
    "Acetyl (Protein N-term)":  "[^",
    "Deamidation (NQ)":         "NQ",
}

_NUM_VAR_MOD_SLOTS = 16


def _build_fix_mods_table(fixed_mod_names: list[str]) -> str:
    """Build the msfragger.table.fix-mods string from configured fixed mod names."""
    masses: dict[str, float] = {}
    for name in fixed_mod_names:
        if name not in MOD_REGISTRY:
            logger.warning("[fragpipe] unknown fixed mod '%s', skipping", name)
            continue
        mass = MOD_REGISTRY[name]["sage_mass"]
        for aa in MOD_REGISTRY[name].get("sage_residues", []):
            masses[aa] = mass

    entries = []
    for res_name, aa_code in _FIXED_MOD_RESIDUES:
        m = masses.get(aa_code, 0.0) if aa_code else 0.0
        entries.append(f"{m},{res_name},true,-1")
    return "; ".join(entries)


def _build_var_mods_table(var_mod_names: list[str]) -> str:
    """Build the msfragger.table.var-mods string from configured variable mod names."""
    slots: list[tuple[float, str, bool, int]] = []
    for name in var_mod_names:
        if name not in MOD_REGISTRY or name not in _VAR_MOD_RESIDUES:
            logger.warning("[fragpipe] unknown or unsupported variable mod '%s', skipping", name)
            continue
        mass = MOD_REGISTRY[name]["sage_mass"]
        residues = _VAR_MOD_RESIDUES[name]
        slots.append((mass, residues, True, 3))

    # Pad to _NUM_VAR_MOD_SLOTS with empty disabled slots
    for i in range(len(slots), _NUM_VAR_MOD_SLOTS):
        slots.append((0.0, f"site_{i + 1}", False, 1))

    parts = []
    for mass, res, enabled, max_count in slots:
        parts.append(f"{mass},{res},{'true' if enabled else 'false'},{max_count}")
    return "; ".join(parts)


class FragPipeRunner(BaseRunner):
    SUPPORTED_ACQUISITIONS = (DDA, DIA)

    @property
    def tool_name(self) -> str:
        return "fragpipe"

    def _ionquant_jar(self) -> Path | None:
        tools_dir = Path(self.version_cfg["dir"]) / "tools"
        jars = sorted(tools_dir.glob("IonQuant*.jar"))
        return jars[-1] if jars else None

    def _diatracer_jar(self) -> Path | None:
        import re
        tools_dir = Path(self.version_cfg["dir"]) / "tools"
        pattern = re.compile(r"diatracer-(Commercial-)?[\d.]+.*\.jar", re.IGNORECASE)
        jars = sorted(f for f in tools_dir.iterdir() if pattern.match(f.name))
        return jars[-1] if jars else None

    def _fasta(self) -> Path:
        """Return the FASTA path to use: fasta_decoy if set, otherwise fasta."""
        decoy = self.dataset_cfg.get("fasta_decoy", "")
        return Path(decoy) if decoy else Path(self.dataset_cfg["fasta"])

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        fasta_decoy = self.dataset_cfg.get("fasta_decoy", "")
        if fasta_decoy and not Path(fasta_decoy).exists():
            errors.append(f"fasta_decoy not found: {fasta_decoy}")
        fp_dir = Path(self.version_cfg["dir"])
        if not fp_dir.exists():
            errors.append(
                f"FragPipe directory not found: {fp_dir}. "
                f"Check 'dir:' under tools > fragpipe > versions > id: {self.version_id} in config.yaml. "
                "Download FragPipe from https://github.com/Nesvilab/FragPipe/releases"
            )
            return errors

        launcher = fp_dir / "bin" / "fragpipe"
        if not launcher.exists():
            errors.append(f"FragPipe launcher not found: {launcher}")

        msfragger_jar = self.version_cfg.get("msfragger_jar", "")
        if not msfragger_jar:
            errors.append("msfragger_jar is not set. Run: python setup.py --accept-license --msfragger-only")
        elif not Path(msfragger_jar).exists():
            errors.append(f"MSFragger JAR not found: {msfragger_jar}")

        if self._ionquant_jar() is None:
            errors.append(
                f"IonQuant JAR not found in {fp_dir / 'tools'}. "
                "Run: python setup.py --accept-license --ionquant-only"
            )

        if self._diatracer_jar() is None:
            errors.append(
                f"DiaTracer JAR not found in {fp_dir / 'tools'}. "
                "Run: python setup.py --diatracer-only"
            )

        phil_dir = fp_dir / "tools" / "Philosopher"
        if not phil_dir.is_dir() or not any(phil_dir.glob("philosopher*")):
            errors.append(f"Philosopher binary not found in: {phil_dir}")

        workflow_name = self._select_workflow()
        workflow_file = fp_dir / "workflows" / f"{workflow_name}.workflow"
        if not workflow_file.exists():
            errors.append(f"Workflow file not found: {workflow_file}")

        return errors

    def map_params(self) -> dict:
        sp = self.search_params
        enzyme_key  = sp.get("enzyme", "trypsin")
        enzyme_info = _FP_ENZYME.get(enzyme_key, _FP_ENZYME["trypsin"])

        return {
            "enzyme_dropdown":    enzyme_info[0],
            "enzyme_cut":         enzyme_info[1],
            "enzyme_nocut":       enzyme_info[2],
            "enzyme_direction":   enzyme_info[3],
            "missed_cleavages":   sp.get("missed_cleavages", 2),
            "min_pep_length":     sp.get("min_peptide_length", 7),
            "max_pep_length":     sp.get("max_peptide_length", 30),
            "precursor_tol_ppm":  sp.get("precursor_mass_tolerance_ppm", 20),
            "fragment_tol_ppm":   sp.get("fragment_mass_tolerance_ppm", 20),
            "max_mods":           sp.get("max_mods_per_peptide", 3),
            "fdr_psm":            sp.get("fdr_psm", 0.01),
            "fdr_protein":        sp.get("fdr_protein", 0.01),
            "mbr":                sp.get("match_between_runs", False),
            "min_charge":         sp.get("min_charge", 2),
            "max_charge":         sp.get("max_charge", 4),
            "fixed_mods":         sp.get("fixed_mods", []),
            "variable_mods":      sp.get("variable_mods", []),
        }

    def _write_manifest(self, input_files: list[Path], output_dir: Path) -> Path:
        acquisition = self.acquisition
        manifest_path = output_dir / "fragpipe_manifest.fp-manifest"
        with open(manifest_path, "w") as f:
            for fp in input_files:
                # No headers; required columns: Filepath \t basename \t DIAorDDA
                f.write(f"{fp}\t{fp.stem}\t\t{acquisition}\n")
        return manifest_path

    def _write_workflow(self, fasta: Path, p: dict, threads: int, output_dir: Path) -> Path:
        """Write a modified workflow file with all search parameters applied."""
        fp_dir = Path(self.version_cfg["dir"])
        workflow_name = self._select_workflow()
        template = fp_dir / "workflows" / f"{workflow_name}.workflow"

        # Parse template into ordered key=value pairs (preserving comments/blank lines)
        header: list[str] = []
        props: dict[str, str] = {}
        prop_order: list[str] = []
        for line in template.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                header.append(line)
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                props[k] = v
                prop_order.append(k)

        # Build overrides dict
        tol = p["precursor_tol_ppm"]
        overrides: dict[str, str] = {
            "database.db-path":                         str(fasta),
            "msfragger.num_threads":                    str(threads),
            "msfragger.precursor_mass_lower":           str(-tol),
            "msfragger.precursor_mass_upper":           str(tol),
            "msfragger.precursor_mass_units":           "1",
            "msfragger.fragment_mass_tolerance":        str(p["fragment_tol_ppm"]),
            "msfragger.fragment_mass_units":            "1",
            "msfragger.precursor_true_tolerance":       str(tol),
            "msfragger.allowed_missed_cleavage_1":      str(p["missed_cleavages"]),
            "msfragger.allowed_missed_cleavage_2":      str(p["missed_cleavages"]),
            "msfragger.digest_min_length":              str(p["min_pep_length"]),
            "msfragger.digest_max_length":              str(p["max_pep_length"]),
            "msfragger.max_variable_mods_per_peptide":  str(p["max_mods"]),
            "msfragger.misc.fragger.precursor-charge-lo": str(p["min_charge"]),
            "msfragger.misc.fragger.precursor-charge-hi": str(p["max_charge"]),
            "msfragger.misc.fragger.enzyme-dropdown-1": p["enzyme_dropdown"],
            "msfragger.search_enzyme_name_1":           p["enzyme_dropdown"],
            "msfragger.search_enzyme_cut_1":            p["enzyme_cut"],
            "msfragger.search_enzyme_nocut_1":          p["enzyme_nocut"],
            "msfragger.search_enzyme_sense_1":          p["enzyme_direction"],
            "msfragger.table.fix-mods":                 _build_fix_mods_table(p["fixed_mods"]),
            "msfragger.table.var-mods":                 _build_var_mods_table(p["variable_mods"]),
            "ionquant.mbr":                             "1" if p["mbr"] else "0",
            "ionquant.ionfdr":                          str(p["fdr_psm"]),
            "phi-report.filter": (
                f"--sequential --prot {p['fdr_protein']} --picked"
            ),
        }

        # Apply overrides; append new keys not in template at the end
        props.update(overrides)
        for k in overrides:
            if k not in prop_order:
                prop_order.append(k)

        out_workflow = output_dir / "fragpipe_workflow.workflow"
        lines = header + [f"{k}={props[k]}" for k in prop_order]
        out_workflow.write_text("\n".join(lines) + "\n")
        return out_workflow

    def _select_workflow(self) -> str:
        extra = self.extra or {}
        if self.acquisition == DDA:
            return extra.get("dda_workflow", "LFQ-MBR")
        instrument = self.dataset_cfg.get("instrument", "")
        if instrument.lower() == "timstof":
            return extra.get("dia_pasef_workflow", "DIA_SpecLib_Quant_diaPASEF")
        return extra.get("dia_workflow", "DIA_SpecLib_Quant")

    def extra_env(self) -> dict[str, str]:
        return {"JAVA_OPTS": "-Djava.awt.headless=true"}

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        fp_dir        = Path(self.version_cfg["dir"])
        launcher      = fp_dir / "bin" / "fragpipe"
        threads       = self.global_cfg.get("threads_per_job", 16)
        manifest_path = self._write_manifest(input_files, output_dir)
        p             = self.map_params()
        workflow_path = self._write_workflow(self._fasta(), p, threads, output_dir)

        logger.info("[fragpipe] acquisition=%s → workflow: %s", self.acquisition, self._select_workflow())

        python = self.global_cfg.get("python", "python3")
        return [
            str(launcher),
            "--headless",
            "--workflow",      str(workflow_path),
            "--manifest",      str(manifest_path),
            "--workdir",       str(output_dir),
            "--threads",       str(threads),
            "--config-python", str(python),
        ]
