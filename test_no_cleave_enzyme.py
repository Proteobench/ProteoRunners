"""Self-check for the 'no-cleave' enzyme setting (pre-digested FASTA, e.g. Entrapment).

Run directly: python3 test_no_cleave_enzyme.py
"""
from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from runners.base import ENZYME_MAP
from runners.diann import DIANNRunner
from runners.fragpipe import FragPipeRunner
from runners.maxquant import MaxQuantRunner


def test_enzyme_map():
    no_cleave = ENZYME_MAP["no-cleave"]
    # diann: "" (empty --cut rule), verified empirically against a real FASTA
    # sample to return each entry unsplit with no extra spurious peptides.
    assert no_cleave["diann"] == ""
    assert no_cleave["alphadia"] == "no-cleave"
    assert no_cleave["sage_cleave_at"] == "$"
    assert no_cleave["maxquant"] is None
    assert no_cleave["metamorpheus"] == "peptidomics"
    # "non-specific" must be "" (matches Sage's fully-nonspecific fallback), not "*"
    # (which is not a valid amino acid and would panic Sage's Enzyme::new assert).
    assert ENZYME_MAP["non-specific"]["sage_cleave_at"] == ""


def test_fragpipe_maps_official_nocleavage_enzyme():
    r = FragPipeRunner.__new__(FragPipeRunner)
    r.search_params = {"enzyme": "no-cleave"}
    p = r.map_params()
    assert (p["enzyme_dropdown"], p["enzyme_cut"], p["enzyme_nocut"]) == ("nocleavage", "@", "@")


def test_diann_maps_no_cleave_to_empty_cut():
    r = DIANNRunner.__new__(DIANNRunner)
    r.search_params = {"enzyme": "no-cleave"}
    p = r.map_params()
    assert p["enzyme"] == ""


_MQPAR_TEMPLATE = """<?xml version="1.0"?>
<MaxQuantParams>
  <minPeptideLengthForUnspecificSearch>8</minPeptideLengthForUnspecificSearch>
  <maxPeptideLengthForUnspecificSearch>25</maxPeptideLengthForUnspecificSearch>
  <identifierParseRule>&gt;[^|]*\\|(.*?)\\|</identifierParseRule>
  <filePaths><string>a.raw</string></filePaths>
  <parameterGroups>
    <parameterGroup>
      <enzymeMode>0</enzymeMode>
      <enzymes><string>Trypsin/P</string></enzymes>
      <lfqNormType>1</lfqNormType>
    </parameterGroup>
  </parameterGroups>
</MaxQuantParams>
"""


def test_entrapment_forces_no_cleave():
    sp = {"enzyme": "trypsin/p"}
    common = dict(
        tool_cfg={"extra": {}}, dataset_cfg={}, version_cfg={"id": "x"},
        global_cfg={}, search_params=sp,
    )
    ent = DIANNRunner(dataset_name="Entrapment_DIA", **common)
    assert ent.search_params["enzyme"] == "no-cleave"
    # shared dict untouched, so other datasets keep the user's enzyme
    assert sp["enzyme"] == "trypsin/p"
    other = DIANNRunner(dataset_name="HYE_Astral", **common)
    assert other.search_params["enzyme"] == "trypsin/p"


def test_maxquant_patches_no_cleavage_mode():
    with tempfile.TemporaryDirectory() as tmp:
        mqpar = Path(tmp) / "mqpar.xml"
        mqpar.write_text(_MQPAR_TEMPLATE)

        r = MaxQuantRunner.__new__(MaxQuantRunner)
        r.search_params = {
            "enzyme": "no-cleave", "min_peptide_length": 6, "max_peptide_length": 30,
        }
        r.extra = {}
        r.dataset_name = "Entrapment_DIA"
        r.dataset_cfg = {"acquisition": "DIA"}
        r._patch_mqpar(mqpar, [Path("a.raw")], Path("db.fasta"))

        root = ET.parse(mqpar).getroot()
        assert root.find(".//parameterGroup/enzymeMode").text == "5"
        assert list(root.find(".//parameterGroup/enzymes")) == []
        assert root.find(".//minPeptideLengthForUnspecificSearch").text == "6"
        assert root.find(".//maxPeptideLengthForUnspecificSearch").text == "30"


if __name__ == "__main__":
    test_enzyme_map()
    test_fragpipe_maps_official_nocleavage_enzyme()
    test_diann_maps_no_cleave_to_empty_cut()
    test_entrapment_forces_no_cleave()
    test_maxquant_patches_no_cleavage_mode()
    print("OK")
