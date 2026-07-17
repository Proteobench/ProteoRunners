"""MaxQuant runner.

Supported search_params keys:
  fdr_psm, fdr_peptide, fdr_protein, match_between_runs, normalize,
  precursor_mass_tolerance_ppm, fragment_mass_tolerance_ppm,
  missed_cleavages, min_peptide_length, max_peptide_length,
  fixed_mods, variable_mods, max_mods_per_peptide,
  min_charge, max_charge

min_charge/max_charge are applied to feature-detection maxCharge and, for DIA
groups, diaMinCharge/diaMaxCharge. max_mods_per_peptide sets both maxNmods and
(for DIA groups) diaMaxModifications.

precursor_mz_range upper bound → diaMaxPrecursorMz (DIA groups).

Not mapped:
  precursor_mz_range lower bound, fragment_mz_range (MaxQuant exposes no matching param)
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
            errors.append(
                f"MaxQuantCmd.dll not found: {self._cmd_dll()}. "
                f"Check 'dir:' under tools > maxquant > versions > id: {self.version_id} in config.yaml. "
                "Download MaxQuant from https://www.maxquant.org/ and extract the zip."
            )
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
            "normalize":            sp.get("normalize", True),
            "min_charge":           sp.get("min_charge", 2),
            "max_charge":           sp.get("max_charge", 4),
            "precursor_mz_max":     sp.get("precursor_mz_range", [0.0, 0.0])[1],
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

        # --- 3. set enzyme ---
        # --create defaults to Trypsin/P regardless of --LCMSType; patch all
        # parameterGroup enzyme lists to the configured enzyme.
        p = self.map_params()
        enzyme_name = p["enzyme"]
        if enzyme_name is None:
            # "no-cleave": FASTA entries are already final peptides (e.g. a
            # pre-digested Entrapment database). MaxQuant's dedicated enzymeMode=5
            # ("No cleavage") treats each entry as one peptide, no digestion —
            # confirmed via MqUtil.dll's EnzymeMode enum (Specific=0 ... None=5).
            # It reads peptide length bounds from *ForUnspecificSearch, not
            # minPeptideLength/maxPeptideLength.
            for enzymes_el in root.findall('.//parameterGroup/enzymes'):
                for child in list(enzymes_el):
                    enzymes_el.remove(child)
                dirty = True
            for mode_el in root.findall('.//parameterGroup/enzymeMode'):
                if mode_el.text != "5":
                    mode_el.text = "5"
                    dirty = True
            for lo_el in root.findall('.//minPeptideLengthForUnspecificSearch'):
                if lo_el.text != str(p["min_peptide_length"]):
                    lo_el.text = str(p["min_peptide_length"])
                    dirty = True
            for hi_el in root.findall('.//maxPeptideLengthForUnspecificSearch'):
                if hi_el.text != str(p["max_peptide_length"]):
                    hi_el.text = str(p["max_peptide_length"])
                    dirty = True
        else:
            for enzymes_el in root.findall('.//parameterGroup/enzymes'):
                strings = enzymes_el.findall('string')
                if len(strings) == 1 and strings[0].text != enzyme_name:
                    strings[0].text = enzyme_name
                    dirty = True

        # --- 4. set fragment mass tolerance ---
        # msmsParamsArray contains one entry per MS2 type (FTMS, ITMS, etc.).
        # --changeParameter cannot address nested array members, so patch them here.
        frag_tol = str(p["fragment_tol_ppm"])
        for msms_params_el in root.findall('.//msmsParamsArray/msmsParams'):
            tol_el = msms_params_el.find('MatchTolerance')
            ppm_el = msms_params_el.find('MatchToleranceInPpm')
            if tol_el is not None and tol_el.text != frag_tol:
                tol_el.text = frag_tol
                dirty = True
            if ppm_el is not None and ppm_el.text != 'True':
                ppm_el.text = 'True'
                dirty = True

        # --- 5. LFQ normalization ---
        # lfqNormType lives inside parameterGroup: 1=enabled, 0=disabled.
        lfq_norm = "1" if p["normalize"] else "0"
        for pg in root.findall('.//parameterGroup'):
            lfq_el = pg.find('lfqNormType')
            if lfq_el is not None and lfq_el.text != lfq_norm:
                lfq_el.text = lfq_norm
                dirty = True

        # --- 6. fixed and variable modifications ---
        # --create writes MaxQuant's own default variable mods (Oxidation (M) +
        # Acetyl (Protein N-term)); without this, unconfigured mods (notably
        # Acetyl) silently widen the search. Overwrite both lists from config.
        # restrictMods (mods excluded from protein-quantification peptides)
        # defaults to the variable-mod list, so keep it in sync.
        def _set_string_list(parent, tag, values):
            nonlocal dirty
            el = parent.find(tag)
            if el is None:
                return
            if [c.text for c in el.findall('string')] == list(values):
                return
            for c in list(el):
                el.remove(c)
            for v in values:
                ET.SubElement(el, 'string').text = v
            dirty = True

        for pg in root.findall('.//parameterGroup'):
            _set_string_list(pg, 'fixedModifications', p["fixed_mods"])
            _set_string_list(pg, 'variableModifications', p["var_mods"])
        _set_string_list(root, 'restrictMods', p["var_mods"])

        # --- 7b. MBR matching/alignment windows ---
        # --create leaves these at 0; enabling matchBetweenRuns via
        # --changeParameter does not populate them, so MBR would run with a
        # zero-width matching window and transfer nothing. Set MaxQuant's
        # standard windows when MBR is on (ion-mobility windows only bite on
        # PASEF data, harmless otherwise).
        if p["mbr"]:
            for tag, val in (('matchingTimeWindow', '0.4'),
                             ('matchingIonMobilityWindow', '0.05'),
                             ('alignmentTimeWindow', '20'),
                             ('alignmentIonMobilityWindow', '1')):
                el = root.find(f'.//{tag}')
                if el is not None and el.text != val:
                    el.text = val
                    dirty = True

        # --- 7. charge range and max modifications ---
        # From search_params: max_charge → feature-detection maxCharge and DIA
        # diaMaxCharge; min_charge → diaMinCharge; max_mods_per_peptide →
        # diaMaxModifications (DIA library), alongside maxNmods set via
        # --changeParameter. diaMinCharge/diaMaxCharge/diaMaxModifications exist
        # only in DIA parameterGroups; maxCharge in all.
        mz_max = p["precursor_mz_max"]
        mz_max_str = str(int(mz_max)) if float(mz_max).is_integer() else str(mz_max)
        for pg in root.findall('.//parameterGroup'):
            for tag, val in (('maxCharge', str(p["max_charge"])),
                             ('diaMinCharge', str(p["min_charge"])),
                             ('diaMaxCharge', str(p["max_charge"])),
                             ('diaMaxModifications', str(p["max_mods"])),
                             # DIA initial precursor/fragment mass tolerances
                             # from precursor/fragment_mass_tolerance_ppm.
                             ('diaInitialPrecMassTolPpm', str(p["precursor_tol_ppm"])),
                             ('diaInitialFragMassTolPpm', str(p["fragment_tol_ppm"])),
                             # upper bound of precursor_mz_range.
                             ('diaMaxPrecursorMz', mz_max_str)):
                el = pg.find(tag)
                if el is not None and el.text != val:
                    el.text = val
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
        # Note: fragment tolerance lives in msmsParamsArray (nested), which --changeParameter
        # cannot reach; it is patched directly in the XML by _patch_mqpar above.
        # MaxQuant confusingly names the PSM-level FDR "peptideFdr" in mqpar.xml.
        overrides = {
            "maxMissedCleavages":  str(p["missed_cleavages"]),
            "mainSearchTol":       str(p["precursor_tol_ppm"]),
            "peptideFdr":          str(p["fdr_psm"]),
            "proteinFdr":          str(p["fdr_protein"]),
            "matchBetweenRuns":    str(p["mbr"]).lower(),
            "maxNmods":            str(p["max_mods"]),
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
