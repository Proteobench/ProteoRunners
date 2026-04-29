"""Base runner class and shared utilities for all search engine runners."""

from __future__ import annotations

import gc
import logging
import os
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Acquisition types recognised throughout the pipeline
DDA = "DDA"
DIA = "DIA"


def infer_acquisition(dataset_name: str, dataset_cfg: dict) -> str:
    """Return 'DDA' if dataset name contains 'DDA', else 'DIA'.
    An explicit 'acquisition' key in dataset_cfg always takes precedence.
    """
    explicit = dataset_cfg.get("acquisition", "").upper()
    if explicit in (DDA, DIA):
        return explicit
    return DDA if "DDA" in dataset_name.upper() else DIA


# Maps human-readable mod names (MaxQuant convention) to tool-specific representations.
# Keys must match what users write in config.yaml search_params.fixed_mods / variable_mods.
MOD_REGISTRY: dict[str, dict[str, Any]] = {
    "Carbamidomethyl (C)": {
        "maxquant": "Carbamidomethyl (C)",
        "metamorpheus_fixed": "Carbamidomethyl on C",
        "alphadia": "Carbamidomethyl@C",
        "diann": "Carbamidomethyl",          # DIA-NN recognises common mod names
        "sage_residues": ["C"],
        "sage_mass": 57.021464,
        "unimod_id": 4,
    },
    "Oxidation (M)": {
        "maxquant": "Oxidation (M)",
        "metamorpheus_variable": "Oxidation on M",
        "alphadia": "Oxidation@M",
        "diann": "Oxidation",
        "sage_residues": ["M"],
        "sage_mass": 15.994915,
        "unimod_id": 35,
    },
    "Phospho (STY)": {
        "maxquant": "Phospho (STY)",
        "metamorpheus_variable": "Phosphorylation on S",    # MetaMorpheus uses per-residue entries
        "alphadia": "Phospho@S;Phospho@T;Phospho@Y",
        "diann": "Phospho",
        "sage_residues": ["S", "T", "Y"],
        "sage_mass": 79.966331,
        "unimod_id": 21,
    },
    "Acetyl (Protein N-term)": {
        "maxquant": "Acetyl (Protein N-term)",
        "metamorpheus_variable": "Acetylation on X",
        "alphadia": "Acetyl@^",
        "diann": "Acetylation",
        "sage_residues": ["["],           # Sage uses "[" for N-term
        "sage_mass": 42.010565,
        "unimod_id": 1,
    },
    "Deamidation (NQ)": {
        "maxquant": "Deamidation (NQ)",
        "metamorpheus_variable": "Deamidation on N",
        "alphadia": "Deamidation@N;Deamidation@Q",
        "diann": "Deamidation",
        "sage_residues": ["N", "Q"],
        "sage_mass": 0.984016,
        "unimod_id": 7,
    },
}

ENZYME_MAP = {
    "trypsin":      {"diann": "Trypsin", "alphadia": "trypsin", "sage_cleave_at": "KR", "sage_restrict": "P",
                     "maxquant": "Trypsin/P", "metamorpheus": "trypsin"},
    "lysc":         {"diann": "LysC",    "alphadia": "lysc",    "sage_cleave_at": "K",  "sage_restrict": None,
                     "maxquant": "Lys-C", "metamorpheus": "Lys-C"},
    "gluc":         {"diann": "GluC",    "alphadia": "gluc",    "sage_cleave_at": "DE", "sage_restrict": None,
                     "maxquant": "Glu-C", "metamorpheus": "Glu-C"},
    "chymotrypsin": {"diann": "Chymotrypsin", "alphadia": "chymotrypsin", "sage_cleave_at": "FWYL", "sage_restrict": None,
                     "maxquant": "Chymotrypsin (FWYL)", "metamorpheus": "chymotrypsin"},
}


@dataclass
class RunResult:
    tool: str
    version: str
    dataset: str
    success: bool
    runtime_s: float
    output_dir: Path
    error_msg: str = ""
    skipped: bool = False
    stdout_log: Path | None = None
    stderr_log: Path | None = None


class BaseRunner(ABC):
    """Abstract base for all search engine runners."""

    # Override in subclasses to restrict to one acquisition type
    SUPPORTED_ACQUISITIONS: tuple[str, ...] = (DDA, DIA)

    def __init__(
        self,
        tool_cfg: dict,
        dataset_name: str,
        dataset_cfg: dict,
        version_cfg: dict,
        global_cfg: dict,
        search_params: dict,
    ) -> None:
        self.tool_cfg = tool_cfg
        self.dataset_name = dataset_name
        self.dataset_cfg = dataset_cfg
        self.version_cfg = version_cfg
        self.global_cfg = global_cfg
        self.search_params = search_params
        self.extra = tool_cfg.get("extra", {}) or {}

    @property
    @abstractmethod
    def tool_name(self) -> str:
        pass

    @property
    def version_id(self) -> str:
        return self.version_cfg["id"]

    @property
    def acquisition(self) -> str:
        return infer_acquisition(self.dataset_name, self.dataset_cfg)

    def is_compatible(self) -> bool:
        """Return False if this tool/version cannot handle the dataset at all.
        Incompatible jobs are silently skipped at build time, never queued.
        Override in subclasses for version-specific checks.
        """
        return self.acquisition in self.SUPPORTED_ACQUISITIONS

    def preflight_check(self) -> list[str]:
        """Return list of error strings; empty list means all checks passed.
        Called only on jobs that passed is_compatible().
        """
        errors: list[str] = []

        dataset_path = Path(self.dataset_cfg["path"])
        if not dataset_path.exists():
            errors.append(f"Dataset path not found: {dataset_path}")
        fasta = Path(self.dataset_cfg["fasta"])
        if not fasta.exists():
            errors.append(f"FASTA not found: {fasta}")
        if not self.get_input_files():
            errors.append(f"No input MS files found in {dataset_path}")
        return errors

    def get_input_files(self) -> list[Path]:
        """Return list of MS data file paths based on dataset format."""
        dataset_path = Path(self.dataset_cfg["path"])
        fmt = self.dataset_cfg["format"]
        if fmt == "raw":
            return sorted(dataset_path.glob("*.raw"))
        if fmt == "mzml":
            return sorted(dataset_path.glob("*.mzML")) or sorted(dataset_path.glob("*.mzml"))
        if fmt == "d":
            # Bruker .d directories
            return sorted(p for p in dataset_path.iterdir() if p.is_dir() and p.suffix == ".d")
        if fmt == "wiff":
            return sorted(dataset_path.glob("*.wiff"))
        if fmt == "mgf":
            return sorted(dataset_path.glob("*.mgf")) + sorted(dataset_path.glob("*.mgf.gz"))
        return []

    @abstractmethod
    def map_params(self) -> dict:
        """Translate self.search_params to tool-specific parameter dict."""

    @abstractmethod
    def build_command(self, input_files: list[Path], fasta: Path, output_dir: Path) -> list[str]:
        pass

    def subprocess_stdin(self) -> bytes | None:
        """Override to supply bytes written to the subprocess stdin."""
        return None

    def extra_env(self) -> dict[str, str]:
        """Override to inject extra environment variables into the subprocess."""
        return {}

    def pre_run_hook(self, input_files: list[Path]) -> None:
        """Called once just before the subprocess is launched. Override for pre-run cleanup."""

    def post_run_hook(
        self, input_files: list[Path], output_dir: Path, success: bool, error_msg: str
    ) -> tuple[bool, str]:
        """Called after the subprocess exits. Return (success, error_msg), possibly overriding them."""
        return success, error_msg

    def _output_dir_path(self) -> Path:
        return (
            Path(self.global_cfg["output_dir"])
            / self.dataset_name
            / f"{self.tool_name}_v{self.version_id}"
        )

    def make_output_dir(self) -> Path:
        out = self._output_dir_path()
        out.mkdir(parents=True, exist_ok=True)
        return out

    def run(self) -> RunResult:
        output_dir = self._output_dir_path()
        done_marker = output_dir / ".done"
        overwrite = self.global_cfg.get("overwrite", False)

        if done_marker.exists() and not overwrite:
            logger.info("[SKIP] %s v%s / %s — previous successful run found; set overwrite: true to rerun",
                        self.tool_name, self.version_id, self.dataset_name)
            return RunResult(
                tool=self.tool_name, version=self.version_id, dataset=self.dataset_name,
                success=True, runtime_s=0.0, output_dir=output_dir,
                skipped=True,
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = output_dir / "stdout.log"
        stderr_log = output_dir / "stderr.log"
        input_files = self.get_input_files()
        fasta = Path(self.dataset_cfg["fasta"])

        self.pre_run_hook(input_files)

        try:
            cmd = self.build_command(input_files, fasta, output_dir)
        except Exception as exc:
            return RunResult(
                tool=self.tool_name, version=self.version_id, dataset=self.dataset_name,
                success=False, runtime_s=0.0, output_dir=output_dir,
                error_msg=f"build_command failed: {exc}",
            )

        logger.info("[%s v%s / %s] starting: %s", self.tool_name, self.version_id, self.dataset_name,
                    shlex.join(str(c) for c in cmd))
        t0 = time.monotonic()
        try:
            env = {**os.environ, **self.extra_env()}
            with open(stdout_log, "w") as fout, open(stderr_log, "w") as ferr:
                proc = subprocess.run(
                    [str(c) for c in cmd],
                    stdout=fout,
                    stderr=ferr,
                    input=self.subprocess_stdin(),
                    env=env,
                    check=False,
                )
            runtime = time.monotonic() - t0
            success = proc.returncode == 0
            error_msg = "" if success else f"exit code {proc.returncode}"
        except Exception as exc:
            runtime = time.monotonic() - t0
            success = False
            error_msg = str(exc)
        finally:
            gc.collect()

        success, error_msg = self.post_run_hook(input_files, output_dir, success, error_msg)

        if success:
            done_marker.write_text(datetime.now().isoformat() + "\n")

        level = logging.INFO if success else logging.ERROR
        logger.log(level, "[%s v%s / %s] finished in %.1fs success=%s%s",
                   self.tool_name, self.version_id, self.dataset_name,
                   runtime, success, f" ({error_msg})" if error_msg else "")
        return RunResult(
            tool=self.tool_name, version=self.version_id, dataset=self.dataset_name,
            success=success, runtime_s=runtime, output_dir=output_dir,
            error_msg=error_msg, stdout_log=stdout_log, stderr_log=stderr_log,
        )
