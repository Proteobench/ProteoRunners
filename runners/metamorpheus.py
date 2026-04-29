"""MetaMorpheus runner.

Supported search_params keys:
  fdr_psm (→ QValueThreshold), match_between_runs (→ MatchBetweenRuns),
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods, variable_mods, max_mods_per_peptide,
  min_charge, max_charge
"""

from __future__ import annotations

from pathlib import Path

from .base import DDA, ENZYME_MAP, MOD_REGISTRY, BaseRunner

# MetaMorpheus mod category + name pairs for the ListOfMods fields
_MM_MOD_CATEGORY_FIXED    = "Common Fixed"
_MM_MOD_CATEGORY_VARIABLE = "Common Variable"


def _mm_mods_string(mod_names: list[str], registry_key: str, category: str) -> str:
    """Build the tab-separated mods string MetaMorpheus expects.

    Format: "Category<TAB>ModName<TAB><TAB>Category<TAB>ModName..."
    """
    parts = []
    for name in mod_names:
        if name in MOD_REGISTRY and registry_key in MOD_REGISTRY[name]:
            parts.append(f"{category}\t{MOD_REGISTRY[name][registry_key]}")
    return "\t\t".join(parts)


class MetaMorpheusRunner(BaseRunner):
    SUPPORTED_ACQUISITIONS = (DDA,)

    @property
    def tool_name(self) -> str:
        return "metamorpheus"

    def _dotnet(self) -> str:
        return self.global_cfg.get("dotnet", "/home/robbe/.dotnet/dotnet")

    def _cmd_dll(self) -> Path:
        return Path(self.version_cfg["dir"]) / "CMD.dll"

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        if not self._cmd_dll().exists():
            errors.append(f"MetaMorpheus CMD.dll not found: {self._cmd_dll()}")
        return errors

    def map_params(self) -> dict:
        sp = self.search_params
        enzyme_key  = sp.get("enzyme", "trypsin")
        enzyme_info = ENZYME_MAP.get(enzyme_key, ENZYME_MAP["trypsin"])

        fixed_mods = _mm_mods_string(
            sp.get("fixed_mods", []), "metamorpheus_fixed", _MM_MOD_CATEGORY_FIXED
        )
        var_mods = _mm_mods_string(
            sp.get("variable_mods", []), "metamorpheus_variable", _MM_MOD_CATEGORY_VARIABLE
        )

        return {
            "enzyme":             enzyme_info["metamorpheus"],
            "missed_cleavages":   sp.get("missed_cleavages", 2),
            "min_peptide_length": sp.get("min_peptide_length", 7),
            "max_peptide_length": sp.get("max_peptide_length", 30),
            "precursor_tol_ppm":  sp.get("precursor_mass_tolerance_ppm", 20),
            "fragment_tol_ppm":   sp.get("fragment_mass_tolerance_ppm", 20),
            "fixed_mods":         fixed_mods,
            "var_mods":           var_mods,
            "max_mods":           sp.get("max_mods_per_peptide", 3),
            "fdr":                sp.get("fdr_psm", 0.01),
            "mbr":                sp.get("match_between_runs", False),
            "max_charge":         sp.get("max_charge", 4),
            "threads":            self.global_cfg.get("threads_per_job", 16),
        }

    def _write_search_task(self, output_dir: Path) -> Path:
        p = self.map_params()

        toml_content = f"""\
TaskType = "Search"

[SearchParameters]
DoParsimony = true
NoOneHitWonders = false
ModPeptidesAreDifferent = false
Normalize = false
QuantifyPpmTol = {float(p['precursor_tol_ppm'])}
MatchBetweenRuns = {str(p['mbr']).lower()}
DoLabelFreeQuantification = true
WriteMzId = true
WritePepXml = false
WriteDecoys = true
WriteContaminants = true
MassDiffAcceptorType = "OneMM"
WritePrunedDatabase = false
KeepAllUniprotMods = true
DoLocalizationAnalysis = true
DoHistogramAnalysis = false
SearchTarget = true
DecoyType = "Reverse"
MaxFragmentSize = 30000.0
SearchType = "Classic"

[CommonParameters]
MaxThreadsToUsePerFile = {p['threads']}
ListOfModsFixed = "{p['fixed_mods']}"
ListOfModsVariable = "{p['var_mods']}"
DoPrecursorDeconvolution = true
UseProvidedPrecursorInfo = true
DeconvolutionMaxAssumedChargeState = {p['max_charge']}
TotalPartitions = 1
ProductMassTolerance = "±{p['fragment_tol_ppm']:.4f} PPM"
PrecursorMassTolerance = "±{p['precursor_tol_ppm']:.4f} PPM"
QValueThreshold = {p['fdr']}
PepQValueThreshold = 1.0
ReportAllAmbiguity = true
TrimMs1Peaks = false
TrimMsMsPeaks = true

[CommonParameters.DigestionParams]
MaxMissedCleavages = {p['missed_cleavages']}
MinPeptideLength = {p['min_peptide_length']}
MaxPeptideLength = {p['max_peptide_length']}
MaxModificationIsoforms = 1024
MaxModsForPeptide = {p['max_mods']}
Protease = "{p['enzyme']}"
SearchModeType = "Full"
FragmentationTerminus = "Both"
"""
        task_path = output_dir / "SearchTask.toml"
        task_path.write_text(toml_content)
        return task_path

    def subprocess_stdin(self) -> bytes:
        # MetaMorpheus prompts to accept the Thermo RAW file license on first use.
        return b"y\n"

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        task_path = self._write_search_task(output_dir)
        return [
            self._dotnet(), str(self._cmd_dll()),
            "-t", str(task_path),
            "-d", str(fasta),
            "-s", *[str(f) for f in input_files],
            "-o", str(output_dir),
        ]
