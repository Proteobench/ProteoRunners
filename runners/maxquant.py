"""MaxQuant runner.

Runs inside the quay.io/medbioinf/maxquant docker image (pulled by setup.nf);
the in-container MaxQuantCmd.dll path is recorded in 'maxquant_dll'. dotnet
ships inside the image so no host .NET runtime is required.

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

# Two images are supported: :latest (MaxQuant 2.6.3.0) and :2.8.1.0. Rather
# than depend on each version's CLI (2.6.3.0's MaxQuantCmd.dll has no
# --changeParameter, and the --create syntax differs between 2.6 and 2.8, see
# _create_mqpar), every field below is set by editing the bare mqpar.xml
# template directly, which is version-agnostic.
#
# lcmsRunType XML value for the <=2.7 CLI, whose bare `--create` always writes
# a "Standard" template that must be patched for DIA. The DIA value here is a
# best-effort guess by naming symmetry (2.6.3.0's --create cannot be told the
# type). The 2.8+ CLI takes --LCMSType at create time and writes the correct
# value itself (Standard / MaxDIA / ...), so that path does not use this map.
_MQ_LCMS_RUN_TYPE = {DDA: "Standard", DIA: "DIA"}

# instrument (dataset_cfg) -> MaxQuant 2.8 --instrumentType code.
_MQ_INSTRUMENT_CODE = {"orbitrap": "TO", "astral": "TA", "timstof": "BT", "zenotof": "SC"}


class MaxQuantRunner(BaseRunner):
    SUPPORTED_ACQUISITIONS = (DDA, DIA)

    @property
    def tool_name(self) -> str:
        return "maxquant"

    def _cmd_dll(self) -> str:
        return self.version_cfg.get("maxquant_dll", "/opt/MaxQuant/bin/MaxQuantCmd.dll")

    def _version_tuple(self) -> tuple[int, ...]:
        """Leading numeric components of the configured version id, e.g.
        "2.8.1.0" -> (2, 8, 1, 0). Used to pick the version-specific CLI form."""
        out = []
        for part in str(self.version_cfg.get("id", "")).split("."):
            if not part.isdigit():
                break
            out.append(int(part))
        return tuple(out)

    def _lcms_type_code(self) -> str:
        """MaxQuant 2.8 --LCMSType code for this dataset's acquisition/instrument."""
        tims = str(self.dataset_cfg.get("instrument", "")).lower() == "timstof"
        if self.acquisition == DIA:
            return "TDIA" if tims else "DIA"
        return "TD" if tims else "ST"

    def preflight_check(self) -> list[str]:
        errors = super().preflight_check()
        errors += self.docker_preflight()
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

    def _populate_file_lists(self, root: ET.Element, input_files: list[Path]) -> None:
        """Fill in the per-file lists a bare `--create` leaves as a single
        placeholder entry (this version's --create has no --pathRawFileFolder
        to auto-populate them)."""
        # tag -> (child element name, per-file text)
        specs = {
            'filePaths':         ('string', lambda f: str(f)),
            'experiments':       ('string', lambda f: f.stem),
            'fractions':         ('short',  lambda f: '32767'),
            'ptms':              ('boolean', lambda f: 'False'),
            'paramGroupIndices': ('int',    lambda f: '0'),
            'referenceChannel':  ('string', lambda f: ''),
        }
        for tag, (child_tag, text_fn) in specs.items():
            el = root.find(tag)
            if el is None:
                continue
            for child in list(el):
                el.remove(child)
            for f in input_files:
                ET.SubElement(el, child_tag).text = text_fn(f)

    def _patch_mqpar(self, mqpar: Path, input_files: list[Path], fasta: Path) -> None:
        """Patch the bare mqpar.xml template written by `--create` with every
        search parameter — this MaxQuant version's CLI has no --changeParameter
        to do this piecemeal, so everything is set directly in the XML."""
        tree = ET.parse(str(mqpar))
        root = tree.getroot()
        dirty = False

        # --- 0. per-file lists + FASTA path + threads + acquisition type ---
        self._populate_file_lists(root, input_files)
        fasta_path_el = root.find('.//fastaFiles/FastaFileInfo/fastaFilePath')
        if fasta_path_el is not None:
            fasta_path_el.text = str(fasta)
        threads_el = root.find('numThreads')
        if threads_el is not None:
            threads_el.text = str(self.global_cfg.get("threads_per_job", 16))
        # <=2.7 create is always "Standard" and must be patched; 2.8+ create
        # already wrote the correct lcmsRunType from --LCMSType, so leave it.
        if self._version_tuple() < (2, 8):
            run_type = _MQ_LCMS_RUN_TYPE.get(self.acquisition, "Standard")
            for el in root.findall('.//lcmsRunType'):
                el.text = run_type
        dirty = True

        # --- 1. simple top-level / per-parameterGroup overrides ---
        # (used to go through `--changeParameter`, which 2.6.3.0 does not have)
        p = self.map_params()
        for tag, val in (
            ('peptideFdr', str(p["fdr_psm"])),
            ('proteinFdr', str(p["fdr_protein"])),
            ('matchBetweenRuns', str(p["mbr"])),
            ('minPeptideLength', str(p["min_peptide_length"])),
        ):
            el = root.find(tag)
            if el is not None and el.text != val:
                el.text = val
                dirty = True
        for pg in root.findall('.//parameterGroup'):
            for tag, val in (
                ('maxMissedCleavages', str(p["missed_cleavages"])),
                ('mainSearchTol', str(p["precursor_tol_ppm"])),
                ('maxNmods', str(p["max_mods"])),
            ):
                el = pg.find(tag)
                if el is not None and el.text != val:
                    el.text = val
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
        # --create defaults to Trypsin/P; patch all parameterGroup enzyme lists
        # to the configured enzyme.
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
        dll = self._cmd_dll()
        mqpar = output_dir / "mqpar.xml"

        self._raw_folder = input_files[0].parent  # stored for post_run_hook

        # --create refuses to overwrite an existing mqpar.xml.
        mqpar.unlink(missing_ok=True)

        # The create CLI changed at 2.8. <=2.7: `<mqpar> --create` writes a bare
        # Standard template. 2.8+: `--create --newMqpar <mqpar>` is mandatory and
        # requires --LCMSType / --instrumentType / --pathFasta / --pathRawFileFolder,
        # from which it writes a template with the correct lcmsRunType already set.
        # Either way the per-file lists, fasta and search params are (re)set from
        # the XML in _patch_mqpar below, so the paths passed here only need to be
        # valid; their contents are overwritten.
        if self._version_tuple() >= (2, 8):
            instr = _MQ_INSTRUMENT_CODE.get(
                str(self.dataset_cfg.get("instrument", "")).lower(), "TO")
            create_args = [
                "--create", "--newMqpar", str(mqpar),
                "--LCMSType", self._lcms_type_code(),
                "--instrumentType", instr,
                "--pathFasta", str(fasta),
                "--pathRawFileFolder", str(self._raw_folder),
            ]
        else:
            create_args = [str(mqpar), "--create"]
        create_cmd = self.docker_run_prefix(self.docker_image()) + ["dotnet", dll] + create_args
        logger.info("Creating mqpar.xml ...")
        result = subprocess.run(create_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"MaxQuantCmd --create failed:\n{result.stderr}")

        self._patch_mqpar(mqpar, input_files, fasta)
        return mqpar

    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        mqpar = self._create_mqpar(input_files, fasta, output_dir)
        return self.docker_run_prefix(self.docker_image()) + ["dotnet", self._cmd_dll(), str(mqpar)]

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
