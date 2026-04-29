"""MaxQuant runner.

Supported search_params keys:
  fdr_psm, fdr_peptide, fdr_protein, match_between_runs,
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods, variable_mods, max_mods_per_peptide,
  min_charge, max_charge

Not mapped (MaxQuant does not expose precursor/fragment m/z range as simple CLI params):
  precursor_mz_range, fragment_mz_range
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import DDA, DIA, ENZYME_MAP, MOD_REGISTRY, BaseRunner

logger = logging.getLogger(__name__)

# (LCMSType for DDA, LCMSType for DIA, MaxQuant instrumentType code) per instrument
_MQ_INSTRUMENT: dict[str, tuple[str, str, str]] = {
    "Orbitrap": ("ST",  "DIA",   "TO"),   # Standard DDA / DIA, thermoOrbi
    "Astral":   ("ST",  "DIA",   "TA"),   # Standard DDA / DIA, thermoAstral
    "timstof":  ("TD",  "DDIA",  "BT"),   # TIMSDDA / diaPASEF, BrukerTIMS
    "ZenoTOF":  ("ST",  "DIA",   "SC"),   # Standard DDA / DIA, SciexTOF
}


class MaxQuantRunner(BaseRunner):
    SUPPORTED_ACQUISITIONS = (DDA, DIA)

    @property
    def tool_name(self) -> str:
        return "maxquant"

    def _dotnet(self) -> str:
        return self.global_cfg.get("dotnet", "/home/robbe/.dotnet/dotnet")

    def _cmd_dll(self) -> Path:
        return Path(self.version_cfg["dir"]) / "bin" / "MaxQuantCmd.dll"

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        if not self._cmd_dll().exists():
            errors.append(f"MaxQuantCmd.dll not found: {self._cmd_dll()}")
        return errors

    def map_params(self) -> dict:
        sp = self.search_params
        enzyme_key = sp.get("enzyme", "trypsin")
        enzyme_info = ENZYME_MAP.get(enzyme_key, ENZYME_MAP["trypsin"])

        fixed_mods = [MOD_REGISTRY[m]["maxquant"] for m in sp.get("fixed_mods", []) if m in MOD_REGISTRY]
        var_mods   = [MOD_REGISTRY[m]["maxquant"] for m in sp.get("variable_mods", []) if m in MOD_REGISTRY]

        return {
            "enzyme":               enzyme_info["maxquant"],
            "missed_cleavages":     sp.get("missed_cleavages", 2),
            "min_peptide_length":   sp.get("min_peptide_length", 7),
            "max_peptide_length":   sp.get("max_peptide_length", 30),
            "precursor_tol_ppm":    sp.get("precursor_mass_tolerance_ppm", 20),
            "fragment_tol_ppm":     sp.get("fragment_mass_tolerance_ppm", 20),
            "fixed_mods":           fixed_mods,
            "var_mods":             var_mods,
            "max_mods":             sp.get("max_mods_per_peptide", 3),
            "fdr_psm":              sp.get("fdr_psm", 0.01),
            "fdr_peptide":          sp.get("fdr_peptide", 0.01),
            "fdr_protein":          sp.get("fdr_protein", 0.01),
            "mbr":                  sp.get("match_between_runs", False),
            "min_charge":           sp.get("min_charge", 2),
            "max_charge":           sp.get("max_charge", 4),
        }

    def _patch_mqpar(self, mqpar: Path, input_files: list[Path]) -> None:
        """Patch generated mqpar.xml for compatibility.

        1. Remove file entries not in input_files (strips _uncalibrated.mzML
           leftovers from FragPipe that cause a NullReferenceException in
           MaxQuant's mzML parser).
        2. Relax identifierParseRule to accept both UniProt (two-pipe) and
           non-UniProt headers such as Biognosys iRT entries (one pipe).
        """
        intended = {str(f) for f in input_files}
        tree = ET.parse(str(mqpar))
        root = tree.getroot()
        dirty = False

        # --- 1. filter file list ---
        file_paths_el = root.find('.//filePaths')
        if file_paths_el is not None:
            all_paths = [s.text or "" for s in file_paths_el.findall('string')]
            keep_indices = [i for i, p in enumerate(all_paths) if p in intended]
            if len(keep_indices) < len(all_paths):
                removed = len(all_paths) - len(keep_indices)
                logger.info("[maxquant] filtering mqpar.xml: keeping %d/%d files, removing %d",
                            len(keep_indices), len(all_paths), removed)
                per_file_tags = [
                    'filePaths', 'experiments', 'fractions', 'ptms',
                    'paramGroupIndices', 'referenceChannel',
                ]
                for tag in per_file_tags:
                    el = root.find(f'.//{tag}')
                    if el is None:
                        continue
                    children = list(el)
                    for child in children:
                        el.remove(child)
                    for i in keep_indices:
                        if i < len(children):
                            el.append(children[i])
                dirty = True

        # --- 2. relax FASTA identifier parse rule ---
        # Default >[^|]*\|(.*?)\| requires two pipes (UniProt format) and rejects
        # headers like >Biognosys|iRT-Kit_WR_fusion. The relaxed form accepts
        # both one-pipe and two-pipe headers.
        for el in root.findall('.//identifierParseRule'):
            if el.text and el.text.endswith(r'\|') and r'(?:' not in el.text:
                el.text = r'>[^|]*\|(.*?)(?:\||$)'
                dirty = True

        if dirty:
            tree.write(str(mqpar), encoding='unicode', xml_declaration=True)

    def _create_mqpar(self, input_files: list[Path], fasta: Path, output_dir: Path) -> Path:
        p = self.map_params()
        threads = self.global_cfg.get("threads_per_job", 16)
        instrument = self.dataset_cfg.get("instrument", "Orbitrap")
        dda_type, dia_type, mq_instrument = _MQ_INSTRUMENT.get(instrument, ("ST", "DIA", "TO"))
        lcms_type = dda_type if self.acquisition == DDA else dia_type
        dotnet = self._dotnet()
        dll = str(self._cmd_dll())
        mqpar = output_dir / "mqpar.xml"

        raw_folder = input_files[0].parent
        self._raw_folder = raw_folder  # stored for post_run_hook

        create_cmd = [
            dotnet, dll,
            "--create",
            "--newMqpar",          str(mqpar),
            "--LCMSType",          lcms_type,
            "--instrumentType",    mq_instrument,
            "--pathFasta",         str(fasta),
            "--pathRawFileFolder", str(raw_folder),
            "--numThreads",        str(threads),
        ]
        logger.info("Creating mqpar.xml ...")
        result = subprocess.run(create_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"MaxQuantCmd --create failed:\n{result.stderr}")

        self._patch_mqpar(mqpar, input_files)

        # Override search parameters not exposed by --create.
        # Syntax for MaxQuant 2.8+: dotnet dll mqpar --changeParameter param value
        overrides = {
            "maxMissedCleavages":  str(p["missed_cleavages"]),
            "mainSearchTol":       str(p["precursor_tol_ppm"]),
            "msmsToleranceInPpm":  str(p["fragment_tol_ppm"]),
            "psmFdr":              str(p["fdr_psm"]),
            "peptideFdr":          str(p["fdr_peptide"]),
            "proteinFdr":          str(p["fdr_protein"]),
            "matchBetweenRuns":    str(p["mbr"]).lower(),
            "maxModifications":    str(p["max_mods"]),
            "minPeptideLength":    str(p["min_peptide_length"]),
        }
        for param, value in overrides.items():
            _mq_set_param(str(mqpar), dotnet, dll, param, value)

        return mqpar

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        mqpar = self._create_mqpar(input_files, fasta, output_dir)
        return [self._dotnet(), str(self._cmd_dll()), str(mqpar)]

    def post_run_hook(
        self, input_files: list[Path], output_dir: Path, success: bool, error_msg: str
    ) -> tuple[bool, str]:
        """Move MaxQuant's combined/ output from the raw folder into output_dir.

        MaxQuant 2.8+ ignores fixedCombinedFolder and always writes to
        rawFolder/combined/. Move it into output_dir so results are co-located
        with the logs and mqpar.xml.
        """
        raw_folder = getattr(self, '_raw_folder', None)
        if raw_folder is None and input_files:
            raw_folder = input_files[0].parent

        if raw_folder is None:
            if success:
                return False, "MaxQuant: raw_folder unknown, cannot locate combined/ output"
            return success, error_msg

        combined_src = Path(raw_folder) / "combined"
        combined_dst = output_dir / "combined"
        peptides_txt = combined_src / "txt" / "peptides.txt"

        if not combined_src.exists():
            if success:
                return False, f"MaxQuant: combined/ not found at {combined_src}"
            return success, error_msg

        # Verify actual results were produced before declaring success.
        has_results = peptides_txt.exists() and peptides_txt.stat().st_size > 0

        if combined_dst.exists():
            shutil.rmtree(combined_dst)
        shutil.move(str(combined_src), str(combined_dst))
        logger.info("[maxquant] moved combined/ from %s to %s", combined_src, combined_dst)

        if not has_results:
            return False, f"MaxQuant: combined/txt/peptides.txt missing or empty in {combined_dst}"

        return True, ""


def _mq_set_param(mqpar: str, dotnet: str, dll: str, param: str, value: str) -> None:
    # MaxQuant 2.8+ syntax: dotnet dll mqpar --changeParameter param value
    r = subprocess.run([dotnet, dll, mqpar, "--changeParameter", param, value], capture_output=True)
    if r.returncode != 0:
        logger.warning("--changeParameter %s=%s failed (non-fatal): %s", param, value, r.stderr.decode())
