"""Tests for config_validator.py: the per-tool setup status that decides what
setup redoes, the missing-dataset check, validate_config() and the CLI exit
codes. Docker is replaced by a fake, so these tests run without it."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import config_validator as cv

ROOT = Path(__file__).resolve().parents[2]
CATALOG = {"TINY": {"url": "file:///x.tar.gz", "acquisition": "DDA", "format": "raw", "instrument": "Orbitrap"}}


@pytest.fixture
def images(monkeypatch):
    """Fake docker: the set of images that are 'present'."""
    present: set[str] = set()
    monkeypatch.setattr(cv.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cv, "_docker_image_present", lambda image: image in present)
    return present


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(CATALOG))
    return path


def tool(image, enabled=True, datasets=(), **extra):
    return {"versions": [{"id": "v1", "image": image, "enabled": enabled, **extra}], "datasets": list(datasets)}


def status(cfg, tmp_path, catalog):
    return cv.setup_status(cfg, tmp_path / "config.yaml", catalog)


def test_complete_incomplete_and_disabled(images, tmp_path, catalog):
    images.add("sage:ok")
    cfg = {"tools": {
        "sage": tool("sage:ok"),
        "metamorpheus": tool("mm:missing"),
        "alphadia": tool("ad:missing", enabled=False),
    }}
    tools = status(cfg, tmp_path, catalog)["tools"]
    assert tools["sage"]["status"] == "complete"
    assert tools["metamorpheus"]["status"] == "incomplete"
    assert [p["reason"] for p in tools["metamorpheus"]["problems"]] == ["image"]
    assert tools["metamorpheus"]["problems"][0]["image"] == "mm:missing"
    # A disabled version is never checked, so its missing image is no problem.
    assert tools["alphadia"] == {"status": "disabled", "problems": [], "fragpipe_jars_missing": False}


def test_enabled_must_be_a_real_boolean(images, tmp_path, catalog):
    """enabled: "true" (a string) is not enabled, same as in the runners."""
    cfg = {"tools": {"sage": tool("sage:missing", enabled="true")}}
    assert status(cfg, tmp_path, catalog)["tools"]["sage"]["status"] == "disabled"


def test_unset_image_or_binary_path_is_a_config_problem(images, tmp_path, catalog):
    images.add("diann:1")
    cfg = {"tools": {
        "sage": tool("CHANGE_ME"),
        "diann": tool("diann:1", diann_bin="CHANGE_ME"),
    }}
    tools = status(cfg, tmp_path, catalog)["tools"]
    assert [p["reason"] for p in tools["sage"]["problems"]] == ["config"]
    assert [p["reason"] for p in tools["diann"]["problems"]] == ["config"]


def test_fragpipe_jars(images, tmp_path, catalog):
    images.add("fragpipe:1")
    jars = tmp_path / "jars"
    jars.mkdir()
    (jars / "MSFragger-4.1.jar").write_text("")
    fp = {"image": "fragpipe:1", "fragpipe_root": "/fp", "jars_dir": str(jars)}

    tools = status({"tools": {"fragpipe": tool(**fp)}}, tmp_path, catalog)["tools"]
    assert tools["fragpipe"]["status"] == "incomplete"
    assert {p["reason"] for p in tools["fragpipe"]["problems"]} == {"jars"}
    assert len(tools["fragpipe"]["problems"]) == 2   # IonQuant and diaTracer

    (jars / "IonQuant-1.10.jar").write_text("")
    (jars / "diaTracer-1.1.jar").write_text("")
    assert status({"tools": {"fragpipe": tool(**fp)}}, tmp_path, catalog)["tools"]["fragpipe"]["status"] == "complete"


def test_disabled_fragpipe_reports_missing_jars(images, tmp_path, catalog):
    """The gate uses this to tell the user that --setup adds the JARs."""
    cfg = {"tools": {"fragpipe": tool("fragpipe:1", enabled=False, jars_dir=str(tmp_path / "none"))}}
    fp = status(cfg, tmp_path, catalog)["tools"]["fragpipe"]
    assert fp["status"] == "disabled" and fp["fragpipe_jars_missing"] is True


def test_unset_jars_dir_is_a_config_problem(images, tmp_path, catalog):
    images.add("fragpipe:1")
    cfg = {"tools": {"fragpipe": tool("fragpipe:1", fragpipe_root="/fp", jars_dir="CHANGE_ME/jars")}}
    problems = status(cfg, tmp_path, catalog)["tools"]["fragpipe"]["problems"]
    assert [p["reason"] for p in problems] == ["config"]


@pytest.mark.parametrize("state, missing", [
    ("absent", True),
    ("empty", True),
    ("partial", True),     # still carries the incomplete marker
    ("present", False),
])
def test_missing_datasets(images, tmp_path, catalog, state, missing):
    data = tmp_path / "data" / "TINY"
    if state != "absent":
        data.mkdir(parents=True)
    if state in ("partial", "present"):
        (data / "run.raw").write_text("x")
    if state == "partial":
        (data / cv.INCOMPLETE_MARKER).write_text("")
    # A relative path is relative to the folder of config.yaml.
    cfg = {"datasets": {"TINY": {"path": "data/TINY"}}, "tools": {"sage": tool("s", datasets=["TINY"])}}
    assert (status(cfg, tmp_path, catalog)["missing_datasets"] == ["TINY"]) is missing


def test_missing_datasets_ignores_disabled_tools_and_unknown_datasets(images, tmp_path, catalog):
    cfg = {"datasets": {}, "tools": {
        "sage": tool("s", enabled=False, datasets=["TINY"]),
        "metamorpheus": tool("m", datasets=["NOT_IN_CATALOG", "CHANGE_ME"]),
    }}
    assert status(cfg, tmp_path, catalog)["missing_datasets"] == []


def valid_config(tmp_path, **overrides):
    data = tmp_path / "data" / "TINY"
    data.mkdir(parents=True, exist_ok=True)
    (data / "run.raw").write_text("x")
    (data / "db.fasta").write_text(">p\nPEPTIDEK\n")
    cfg = {
        "global": {"output_dir": "results"},
        "search_params": {"enzyme": "trypsin"},
        "datasets": {
            "TINY": {"path": "data/TINY", "fasta": "data/TINY/db.fasta", "acquisition": "DDA", "format": "raw"},
            # Unused entries (e.g. the template defaults) are never checked.
            "UNUSED": {"path": "/nonexistent", "fasta": "CHANGE_ME", "acquisition": "DIA", "format": "raw"},
        },
        "tools": {"sage": tool("sage:ok", datasets=["TINY"])},
    }
    cfg.update(overrides)
    return cfg


def test_validate_config_accepts_a_good_config(images, tmp_path):
    images.add("sage:ok")
    assert cv.validate_config(valid_config(tmp_path), tmp_path / "config.yaml") == []


def test_validate_config_reports_problems(images, tmp_path):
    cfg = valid_config(tmp_path, **{"global": {"output_dir": "CHANGE_ME/results"}})
    cfg["datasets"]["TINY"]["fasta"] = "data/TINY/wrong.fasta"
    errors = cv.validate_config(cfg, tmp_path / "config.yaml")
    assert any("output_dir still contains 'CHANGE_ME'" in e for e in errors)
    assert any("FASTA file does not exist" in e for e in errors)
    assert any("docker image not pulled locally: sage:ok" in e for e in errors)
    assert not any("UNUSED" in e for e in errors)


def run_cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "config_validator.py"), *args],
                          capture_output=True, text=True)


def test_cli_exit_code_2_for_invalid_yaml(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("global:\n  output_dir: x\n tools: [\n")
    r = run_cli("--config", str(bad), "--setup-status")
    assert r.returncode == 2
    assert "is not valid YAML" in r.stderr and "line 3" in r.stderr


def test_cli_exit_code_2_for_missing_or_empty_config(tmp_path):
    assert run_cli("--config", str(tmp_path / "nope.yaml")).returncode == 2
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert run_cli("--config", str(empty), "--setup-status").returncode == 2
