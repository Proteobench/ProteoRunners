"""FragPipe runner.

Runs inside the fcyucn/fragpipe docker image (pulled by setup.nf). That image
ships FragPipe itself, Java, Philosopher, Python 3 and easypqp, but not the
separately-licensed MSFragger/IonQuant/diaTracer JARs (Nesvilab Academic
License) — setup.nf collects those into a host directory ('jars_dir') which
is bind-mounted onto the container's tools/ folder at run time.

Supported search_params keys:
  fdr_psm, fdr_peptide, fdr_protein, match_between_runs, normalize,
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods, variable_mods, max_mods_per_peptide,
  min_charge, max_charge, precursor_mz_range, fragment_mz_range

extra['diann_cmd_opts'] (DIA only) is passed verbatim as diann.cmd-opts, the
same raw-CLI-passthrough field the FragPipe GUI exposes for its internal
DIA-NN step.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
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
    "no-cleave":    ("nocleavage",    "@",  "@", "C"),
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

    # In-container paths; fragpipe_root is auto-detected once by setup.nf since
    # it varies with the bundled FragPipe version (e.g. .../fragpipe-24.0/fragpipe-24.0).
    def _fragpipe_root(self) -> str:
        return self.version_cfg.get("fragpipe_root", "/fragpipe_bin/fragpipe-24.0/fragpipe-24.0")

    def _container_python(self) -> str:
        return self.version_cfg.get("container_python", "/usr/bin/python3")

    def _jars_dir(self) -> Path:
        """Host directory holding the licensed MSFragger/IonQuant/diaTracer JARs
        collected interactively by setup.nf. Bind-mounted onto the container's
        tools/ folder at run time."""
        return Path(self.version_cfg.get("jars_dir", ""))

    def _licensed_jars(self) -> list[Path]:
        jars_dir = self._jars_dir()
        if not jars_dir.is_dir():
            return []
        return sorted(jars_dir.glob("*.jar"))

    def _ram_gb_per_job(self) -> int:
        """GB of heap FragPipe's internal JVMs (MSFragger, MSBooster, ...) may use.

        'workflow.ram=0' in FragPipe's template means "auto-detect and use
        (almost) all host memory" — fine for a single job, but with
        max_parallel_jobs > 1 each concurrent FragPipe process makes that same
        claim independently, so their JVMs collectively over-commit host RAM
        and one gets SIGKILLed by the OOM killer (exit code 137). Split total
        host memory across the configured parallelism instead, unless the user
        pins an explicit value via global.ram_gb_per_job.
        """
        configured = self.global_cfg.get("ram_gb_per_job")
        if configured:
            return int(configured)
        total_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3)
        parallel = max(1, self.global_cfg.get("max_parallel_jobs", 1))
        return max(4, int(total_gb * 0.85 / parallel))

    def _fasta(self) -> Path:
        """Return the FASTA path to use: fasta_decoy if set, otherwise a
        decoy FASTA generated on demand (see _generate_decoy_fasta)."""
        decoy = self.dataset_cfg.get("fasta_decoy", "")
        if decoy:
            return Path(decoy)
        return self._generate_decoy_fasta(Path(self.dataset_cfg["fasta"]))

    def _run_in_container(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run a throwaway command inside the FragPipe image (no volume mounts)."""
        cmd = ["docker", "run", "--rm", self.docker_image()] + args
        return subprocess.run(cmd, capture_output=True, text=True)

    def _philosopher_bin(self) -> str:
        """Locate the Philosopher binary bundled in the FragPipe image; its
        filename is version-suffixed (e.g. philosopher-v5.1.3-RC9)."""
        tools_dir = f"{self._fragpipe_root()}/tools"
        result = self._run_in_container(["find", f"{tools_dir}/Philosopher", "-maxdepth", "1", "-type", "f"])
        found = [line for line in result.stdout.splitlines() if line.strip()]
        if not found:
            raise RuntimeError(f"Could not locate the Philosopher binary under {tools_dir}/Philosopher")
        return found[0]

    def _generate_decoy_fasta(self, fasta: Path) -> Path:
        """FragPipe/MSFragger needs decoys already appended to the FASTA,
        unlike the other tools here (which generate decoys internally). This
        runs the same command the FragPipe GUI's "Add decoys" button runs —
        `philosopher database --custom <fasta>` — via the Philosopher CLI
        already bundled in the FragPipe image, so no separate FragPipe GUI
        step is needed. No contaminants are added, decoys only.

        Cached next to the source FASTA using Philosopher's own output naming
        convention (<date>-decoys-<fasta name>.fas), so it's generated only
        once — including reuse across dataset entries that share a FASTA,
        and reuse of a file a user already generated by hand via the GUI
        with the same naming convention.

        Philosopher writes its workspace metadata (.meta/) into its current
        working directory, which two concurrent jobs sharing the same FASTA
        would otherwise race on — so this runs in a throwaway temp directory
        and only the resulting FASTA is moved next to the source file.
        """
        cache_pattern = f"*-decoys-{fasta.name}.fas"
        existing = sorted(fasta.parent.glob(cache_pattern))
        if existing:
            return existing[-1]

        logger.info("[fragpipe] no fasta_decoy configured — generating one from %s via philosopher", fasta)
        philosopher = self._philosopher_bin()
        workdir = Path(tempfile.mkdtemp(dir=fasta.parent, prefix=".philosopher_"))
        try:
            cmd = self.docker_run_prefix(self.docker_image()) + [
                "sh", "-c",
                f'cd "{workdir}" && "{philosopher}" workspace --init --nocheck '
                f'&& "{philosopher}" database --custom "{fasta}"',
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"Philosopher decoy generation failed for {fasta}: {result.stderr.strip()}")

            generated = sorted(workdir.glob(cache_pattern))
            if not generated:
                raise RuntimeError(f"Philosopher did not produce a decoy FASTA for {fasta}")

            target = fasta.parent / generated[-1].name
            if not target.exists():
                shutil.move(str(generated[-1]), str(target))
            return target
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        errors += self.docker_preflight()

        fasta_decoy = self.dataset_cfg.get("fasta_decoy", "")
        if fasta_decoy and not Path(fasta_decoy).exists():
            errors.append(f"fasta_decoy not found: {fasta_decoy}")

        for label, needle in (("MSFragger", "msfragger"), ("IonQuant", "ionquant"), ("diaTracer", "diatracer")):
            if not any(needle in p.name.lower() for p in self._licensed_jars()):
                errors.append(
                    f"{label} JAR not found in jars_dir ({self._jars_dir()}). "
                    "Run: nextflow run setup.nf   to add it (or skip FragPipe)."
                )

        if not errors:
            workflow_name = self._select_workflow()
            workflow_path = f"{self._fragpipe_root()}/workflows/{workflow_name}.workflow"
            r = self._run_in_container(["test", "-f", workflow_path])
            if r.returncode != 0:
                errors.append(f"Workflow file not found in FragPipe image: {workflow_path}")

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
            "fdr_peptide":        sp.get("fdr_peptide", 0.01),
            "fdr_protein":        sp.get("fdr_protein", 0.01),
            "mbr":                sp.get("match_between_runs", False),
            "normalize":          sp.get("normalize", True),
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
        """Write a modified workflow file with all search parameters applied.

        The template lives inside the FragPipe image, not on the host, so it is
        read out via a throwaway container run.
        """
        workflow_name = self._select_workflow()
        template_path = f"{self._fragpipe_root()}/workflows/{workflow_name}.workflow"
        result = self._run_in_container(["cat", template_path])
        if result.returncode != 0:
            raise RuntimeError(f"Could not read workflow template from FragPipe image: {template_path}")
        template_text = result.stdout

        # Parse template into ordered key=value pairs (preserving comments/blank lines)
        header: list[str] = []
        props: dict[str, str] = {}
        prop_order: list[str] = []
        for line in template_text.splitlines():
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
            "ionquant.normalization":                   "1" if p["normalize"] else "0",
            "ionquant.ionfdr":                          str(p["fdr_psm"]),
            "phi-report.filter": (
                f"--sequential --psm {p['fdr_psm']} --pep {p['fdr_peptide']} --prot {p['fdr_protein']} --picked"
            ),
            "workflow.ram": str(self._ram_gb_per_job()),
        }

        # Tolerance 0 = automatic. MSFragger's calibrate_mass=2 does mass
        # calibration plus search-parameter optimization, which picks the
        # tolerance itself, so leave the corresponding keys at the workflow
        # template's values instead of pinning them.
        if self.auto_tolerance("precursor"):
            overrides["msfragger.calibrate_mass"] = "2"
        else:
            overrides.update({
                "msfragger.precursor_mass_lower":     str(-tol),
                "msfragger.precursor_mass_upper":     str(tol),
                "msfragger.precursor_mass_units":     "1",
                "msfragger.precursor_true_tolerance": str(tol),
            })
        if self.auto_tolerance("fragment"):
            overrides["msfragger.calibrate_mass"] = "2"
        else:
            overrides.update({
                "msfragger.fragment_mass_tolerance": str(p["fragment_tol_ppm"]),
                "msfragger.fragment_mass_units":     "1",
            })
        if self.acquisition == DIA:
            # NB: match_between_runs is deliberately NOT propagated to DIA-NN here.
            # FragPipe's library-free DIA already shares information across runs by
            # design — MSFragger-DIA builds one experimental spectral library from
            # all runs and DIA-NN does targeted extraction against it in every run.
            # DIA-NN's own MBR (--reanalyse) is a redundant, differently-defined
            # second pass; the FragPipe author states it should never be enabled
            # under FragPipe (Nesvilab/FragPipe#2825), and doing so segfaults the
            # bundled DIA-NN 1.8.2 beta 8 while writing the second-pass report.
            # match_between_runs still applies to DDA (ionquant.mbr above) and to
            # the standalone DIA-NN runner; it is simply a no-op for FragPipe DIA.
            if p["mbr"]:
                logger.info(
                    "[fragpipe] match_between_runs ignored for DIA: FragPipe's DIA "
                    "workflow shares runs via its own spectral library; DIA-NN MBR "
                    "stays off (see Nesvilab/FragPipe#2825)."
                )
            # Raw CLI options appended verbatim to FragPipe's internal DIA-NN call
            # (diaTracer/DIA_SpecLib_Quant workflows only — DDA workflows never run DIA-NN).
            overrides["diann.cmd-opts"] = (self.extra or {}).get("diann_cmd_opts", "")

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

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        fp_root       = self._fragpipe_root()
        launcher      = f"{fp_root}/bin/fragpipe"
        tools_dir     = f"{fp_root}/tools"
        threads       = self.global_cfg.get("threads_per_job", 16)
        manifest_path = self._write_manifest(input_files, output_dir)
        p             = self.map_params()
        workflow_path = self._write_workflow(self._fasta(), p, threads, output_dir)

        logger.info("[fragpipe] acquisition=%s → workflow: %s", self.acquisition, self._select_workflow())

        # Bind-mount each licensed JAR onto the container's tools/ folder, next
        # to the JARs already bundled in the image (Philosopher, DIA-Umpire, ...).
        extra_mounts = [(str(jar), f"{tools_dir}/{jar.name}") for jar in self._licensed_jars()]
        # MSFragger looks for its native readers in <jar dir>/ext (Thermo .raw via
        # BatmassIoThermoServer.exe under mono, Bruker .d via libtimsdata). setup.nf
        # copies that folder next to the jar; mount it so .raw/.d input works.
        ext_dir = self._jars_dir() / "ext"
        if ext_dir.is_dir():
            extra_mounts.append((str(ext_dir), f"{tools_dir}/ext"))
        cmd = self.docker_run_prefix(
            self.docker_image(),
            extra_mounts=extra_mounts,
            env={"JAVA_OPTS": "-Djava.awt.headless=true"},
        )
        cmd += [
            launcher,
            "--headless",
            "--workflow",            str(workflow_path),
            "--manifest",            str(manifest_path),
            "--workdir",             str(output_dir),
            "--threads",             str(threads),
            "--config-tools-folder", tools_dir,
            "--config-python",       self._container_python(),
        ]
        return cmd
