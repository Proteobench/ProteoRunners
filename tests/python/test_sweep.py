"""Tests for runners/sweep.py and how the sweep reaches job enumeration, the
runner's search_params and the config validator. Docker is not needed."""

import pytest

import config_validator as cv
from enumerate_jobs import enumerate_jobs
from run_proteobench import build_jobs
from runners.diann import DIANNRunner
from runners.sweep import expand_versions, sweep_id, sweep_sets

VERSIONS = [
    {"id": "2.5.0", "image": "diann:2.5.0", "diann_bin": "/d", "enabled": True},
    {"id": "2.6.0", "image": "diann:2.6.0", "diann_bin": "/d", "enabled": True, "extra_args": "--a 1"},
]


def test_no_sweep_leaves_versions_unchanged():
    assert expand_versions({"versions": VERSIONS}, "diann") == VERSIONS


def test_grid_runs_every_combination_for_every_version():
    tool = {"versions": VERSIONS, "sweep": {"fragment_mass_tolerance_ppm": [10, 15], "extra_args": ["", "--window 8"]}}
    assert len(sweep_sets(tool)) == 4
    expanded = expand_versions(tool, "diann")
    assert len(expanded) == 8
    assert len({v["id"] for v in expanded}) == 8

    # The hash depends only on the set, so it matches across versions
    params = {"fragment_mass_tolerance_ppm": 15, "extra_args": "--window 8"}
    ids = {v["id"] for v in expanded if v["sweep"] == params}
    assert ids == {f"2.5.0-{sweep_id(params)}", f"2.6.0-{sweep_id(params)}"}

    v = next(v for v in expanded if v["id"] == f"2.6.0-{sweep_id(params)}")
    assert v["search_params"] == {"fragment_mass_tolerance_ppm": 15}
    assert v["extra_args"] == ["--a", "1", "--window", "8"]   # version's own args first
    assert v["image"] == "diann:2.6.0"


def test_list_gives_explicit_sets_and_scalars_are_single_values():
    assert sweep_sets({"sweep": [{"a": 1}, {"a": 2, "b": 3}]}) == [{"a": 1}, {"a": 2, "b": 3}]
    assert sweep_sets({"sweep": {"a": 1, "b": [2, 3]}}) == [{"a": 1, "b": 2}, {"a": 1, "b": 3}]


def test_duplicate_sets_run_once():
    # Same set twice, an empty extra_args, and a grid with a repeated value
    assert sweep_sets({"sweep": [{"a": 1}, {"a": 1}, {"a": 1, "extra_args": ""}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert sweep_sets({"sweep": {"a": [10, 10, 15], "extra_args": ["", []]}}) == [{"a": 10}, {"a": 15}]
    ids = [v["id"] for v in expand_versions({"versions": VERSIONS[:1], "sweep": [{"a": 1}, {"a": 1}]}, "diann")]
    assert ids == [f"2.5.0-{sweep_id({'a': 1})}"]


def test_sweep_overrides_reach_the_runner_and_its_command(tmp_path):
    tool = {"versions": VERSIONS[:1], "sweep": [{"fragment_mass_tolerance_ppm": 7, "match_between_runs": False}]}
    version = expand_versions(tool, "diann")[0]
    runner = DIANNRunner(
        tool_cfg=tool, dataset_name="X",
        dataset_cfg={"acquisition": "DIA", "format": "raw", "path": str(tmp_path), "fasta": str(tmp_path / "db.fasta")},
        version_cfg=version, global_cfg={"output_dir": str(tmp_path)},
        search_params={"fragment_mass_tolerance_ppm": 20, "match_between_runs": True},
    )
    cmd = runner.build_command([tmp_path / "a.raw"], tmp_path / "db.fasta", tmp_path)
    assert cmd[cmd.index("--mass-acc") + 1] == "7"
    assert "--reanalyse" not in cmd
    assert runner._output_dir_path().name == f"diann_v{version['id']}"


def test_enumerate_jobs_lists_one_job_per_set():
    cfg = {
        "search_params": {},
        "datasets": {"D": {"acquisition": "DIA", "format": "raw"}},
        "tools": {"diann": {"versions": VERSIONS, "datasets": ["D"], "sweep": {"fdr_psm": [0.01, 0.05]}}},
    }
    jobs = enumerate_jobs(cfg)
    assert len(jobs) == 4
    assert all(j["version"].count("-") == 1 for j in jobs)


def test_standalone_runner_builds_one_job_per_set(tmp_path):
    cfg = {
        "global": {"output_dir": str(tmp_path)},
        "search_params": {"fdr_psm": 0.01},
        "datasets": {"D": {"acquisition": "DIA", "format": "raw", "path": str(tmp_path), "fasta": str(tmp_path / "db.fasta")}},
        "tools": {"diann": {"versions": VERSIONS, "datasets": ["D"], "sweep": {"fdr_psm": [0.01, 0.05]}}},
    }
    jobs = build_jobs(cfg, None, None)
    assert len(jobs) == 4
    assert sorted(j.search_params["fdr_psm"] for j in jobs) == [0.01, 0.01, 0.05, 0.05]


MAXQUANT_CFG = {
    "search_params": {"fdr_psm": 0.01},
    "datasets": {"D": {"acquisition": "DDA", "format": "raw"}},
    "tools": {"maxquant": {
        "versions": [{"id": "2.6.3.0", "image": "mq:2.6.3.0", "maxquant_dll": "/d", "enabled": True}],
        "datasets": ["D"],
        "sweep": {"fdr_psm": [0.01, 0.05]},
    }},
}


def test_maxquant_sweep_is_ignored_with_a_warning(capsys, caplog, tmp_path):
    tool = MAXQUANT_CFG["tools"]["maxquant"]
    assert expand_versions(tool, "maxquant") == tool["versions"]

    assert [j["version"] for j in enumerate_jobs(MAXQUANT_CFG)] == ["2.6.3.0"]
    assert "sweeping maxquant is not supported" in capsys.readouterr().err

    cfg = {**MAXQUANT_CFG, "global": {"output_dir": str(tmp_path)}}
    assert [j.version_id for j in build_jobs(cfg, None, None)] == ["2.6.3.0"]
    assert "sweeping maxquant is not supported" in caplog.text

    # Ignored, so it does not have to be valid either
    errors: list[str] = []
    cv._validate_sweeps({"maxquant": {**tool, "sweep": {"not_a_param": [1]}}}, {}, tmp_path / "config.yaml", errors)
    assert errors == []


@pytest.mark.parametrize("sweep, expected", [
    ({"fragment_mass_tolerance_ppm": [10, -1]}, "must be a positive number"),
    ({"not_a_param": [1, 2]}, "not_a_param not in search_params"),
    ({"extra_args": ["--x 'unbalanced"]}, "cannot be parsed"),
    ("fragment_mass_tolerance_ppm", "should be a mapping"),
])
def test_validator_reports_bad_sweeps(sweep, expected, tmp_path):
    errors: list[str] = []
    tools = {"diann": {"versions": VERSIONS, "sweep": sweep}}
    cv._validate_sweeps(tools, {"fragment_mass_tolerance_ppm": 10}, tmp_path / "config.yaml", errors)
    assert any(expected in e for e in errors), errors


def test_validator_accepts_a_good_sweep(tmp_path):
    errors: list[str] = []
    tools = {"diann": {"versions": VERSIONS, "sweep": {"fragment_mass_tolerance_ppm": [10, 0], "extra_args": ["", "--w 8"]}}}
    cv._validate_sweeps(tools, {"fragment_mass_tolerance_ppm": 10}, tmp_path / "config.yaml", errors)
    assert errors == []
