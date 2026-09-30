"""Relative paths in config.yaml are resolved against the folder of the config,
never against the Nextflow task work directory the job runs in."""

from pathlib import Path

from run_single_job import resolve_paths


def test_relative_paths_are_relative_to_the_config_folder(tmp_path):
    dataset = {"path": "data/A", "fasta": "data/A/a.fasta", "acquisition": "DDA"}
    glob = {"output_dir": "results"}
    resolve_paths(dataset, glob, tmp_path)
    assert dataset["path"] == str(tmp_path / "data/A")
    assert dataset["fasta"] == str(tmp_path / "data/A/a.fasta")
    assert glob["output_dir"] == str(tmp_path / "results")
    assert dataset["acquisition"] == "DDA"


def test_absolute_and_home_paths(tmp_path):
    dataset = {"path": "/abs/A", "fasta": "~/a.fasta"}
    glob = {"output_dir": "~/results"}
    resolve_paths(dataset, glob, tmp_path)
    assert dataset["path"] == "/abs/A"
    assert dataset["fasta"] == str(Path.home() / "a.fasta")
    assert glob["output_dir"] == str(Path.home() / "results")


def test_missing_keys_stay_missing(tmp_path):
    dataset, glob = {"path": "a"}, {}
    resolve_paths(dataset, glob, tmp_path)
    assert "fasta_decoy" not in dataset and "output_dir" not in glob
