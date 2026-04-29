#!/usr/bin/env python3
"""One-time setup: compile Sage from source and download MSFragger/IonQuant/DiaTracer for FragPipe.

Usage:
    python setup.py --accept-license             # all tasks (Sage + MSFragger + IonQuant + DiaTracer)
    python setup.py --sage-only                  # compile Sage only
    python setup.py --msfragger-only --accept-license
    python setup.py --ionquant-only --accept-license
    python setup.py --diatracer-only             # download DiaTracer (free, no license)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.yaml"

MSFRAGGER_LICENSE_URL = "https://msfragger.nesvilab.org/upgrading_msfragger.html"
MSFRAGGER_API_URL = "https://api.github.com/repos/Nesvilab/MSFragger/releases/latest"
MSFRAGGER_INSTALL_DIR = Path("/home/robbe/bin/MSFragger")

IONQUANT_API_URL = "https://api.github.com/repos/Nesvilab/IonQuant/releases/latest"
DIATRACER_API_URL = "https://api.github.com/repos/Nesvilab/diaTracer/releases"

MSFRAGGER_LICENSE_TEXT = """\
MSFragger is freely available for academic research use.
Commercial use requires a license from the University of Michigan.

By using --accept-license you confirm that you:
  1. Are using MSFragger for non-commercial academic research.
  2. Have read and agree to the MSFragger license at:
     https://msfragger.nesvilab.org/upgrading_msfragger.html

"""


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict, path: Path) -> None:
    with open(path, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)


# ── Sage compilation ─────────────────────────────────────────────────────────

def compile_sage(cfg: dict) -> bool:
    sage_cfg = cfg.get("tools", {}).get("sage", {})
    for version in sage_cfg.get("versions", []):
        source_dir = Path(version.get("source_dir", ""))
        binary = Path(version.get("binary", ""))
        git_tag = version.get("git_tag", "")

        if not source_dir.exists():
            logger.error("Sage source_dir not found: %s", source_dir)
            return False

        if binary.exists():
            logger.info("Sage binary already exists at %s; skipping compilation.", binary)
            return True

        if git_tag:
            logger.info("Checking out Sage git tag %s ...", git_tag)
            r = subprocess.run(["git", "-C", str(source_dir), "checkout", git_tag])
            if r.returncode != 0:
                logger.error("git checkout %s failed.", git_tag)
                return False

        logger.info("Compiling Sage (this may take a few minutes) ...")
        r = subprocess.run(
            ["cargo", "build", "--release", "--manifest-path", str(source_dir / "Cargo.toml")]
        )
        if r.returncode != 0:
            logger.error("Sage compilation failed.")
            return False

        if not binary.exists():
            logger.error("Compilation finished but binary not found at %s", binary)
            return False

        logger.info("Sage compiled successfully: %s", binary)
        return True

    logger.warning("No Sage version entries found in config.")
    return False


# ── MSFragger download ────────────────────────────────────────────────────────

def _get_latest_msfragger_release() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest MSFragger release."""
    import json

    req = urllib.request.Request(MSFRAGGER_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    tag = data["tag_name"]
    for asset in data.get("assets", []):
        if asset["name"].endswith(".jar") and "MSFragger" in asset["name"]:
            return tag, asset["browser_download_url"]

    # Fall back to constructed URL pattern
    version = tag.lstrip("v")
    url = f"https://github.com/Nesvilab/MSFragger/releases/download/{tag}/MSFragger-{version}.jar"
    return tag, url


def download_msfragger(cfg: dict, config_path: Path) -> bool:
    logger.info("Fetching latest MSFragger release info from GitHub ...")
    try:
        tag, url = _get_latest_msfragger_release()
    except Exception as exc:
        logger.error("Could not fetch MSFragger release info: %s", exc)
        logger.error("Download manually from https://github.com/Nesvilab/MSFragger/releases")
        return False

    version = tag.lstrip("v")
    MSFRAGGER_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    jar_path = MSFRAGGER_INSTALL_DIR / f"MSFragger-{version}.jar"

    if jar_path.exists():
        logger.info("MSFragger JAR already present: %s", jar_path)
    else:
        logger.info("Downloading MSFragger %s from %s ...", version, url)
        try:
            urllib.request.urlretrieve(url, str(jar_path), reporthook=_progress)
            print()  # newline after progress
        except Exception as exc:
            logger.error("Download failed: %s", exc)
            return False
        logger.info("Downloaded: %s", jar_path)

    # Update config.yaml with the jar path and enable FragPipe versions
    modified = False
    for version_cfg in cfg.get("tools", {}).get("fragpipe", {}).get("versions", []):
        if not version_cfg.get("msfragger_jar"):
            version_cfg["msfragger_jar"] = str(jar_path)
            version_cfg["enabled"] = True
            modified = True

    if modified:
        save_config(cfg, config_path)
        logger.info("Updated config.yaml: fragpipe.versions[*].msfragger_jar and enabled=true")
    else:
        logger.info("FragPipe versions already configured; config.yaml not modified.")

    return True


def _progress(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb = downloaded / 1_048_576
        total_mb = total_size / 1_048_576
        print(f"\r  {pct:3d}%  {mb:.1f}/{total_mb:.1f} MB", end="", flush=True)


# ── IonQuant download ─────────────────────────────────────────────────────────

def download_ionquant(cfg: dict) -> bool:
    """Download the latest IonQuant JAR and place it in each FragPipe tools/ directory."""
    import json

    logger.info("Fetching latest IonQuant release info from GitHub ...")
    try:
        req = urllib.request.Request(IONQUANT_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.error("Could not fetch IonQuant release info: %s", exc)
        logger.error("Download manually from https://github.com/Nesvilab/IonQuant/releases")
        return False

    tag = data["tag_name"]
    version = tag.lstrip("v")
    jar_name = f"IonQuant-{version}.jar"
    download_url = None
    for asset in data.get("assets", []):
        if asset["name"].endswith(".jar") and "IonQuant" in asset["name"]:
            download_url = asset["browser_download_url"]
            jar_name = asset["name"]
            break
    if download_url is None:
        download_url = (
            f"https://github.com/Nesvilab/IonQuant/releases/download/{tag}/{jar_name}"
        )

    success = True
    for version_cfg in cfg.get("tools", {}).get("fragpipe", {}).get("versions", []):
        fp_dir = Path(version_cfg.get("dir", ""))
        if not fp_dir.is_dir():
            continue
        tools_dir = fp_dir / "tools"
        dest = tools_dir / jar_name
        if dest.exists():
            logger.info("IonQuant already present: %s", dest)
            continue
        logger.info("Downloading IonQuant %s → %s ...", version, dest)
        try:
            urllib.request.urlretrieve(download_url, str(dest), reporthook=_progress)
            print()
        except Exception as exc:
            logger.error("Download failed: %s", exc)
            success = False
            continue
        logger.info("Downloaded: %s", dest)

    return success


# ── DiaTracer download ────────────────────────────────────────────────────────

def _get_required_diatracer_version(fp_dir: Path) -> str | None:
    """Run FragPipe briefly to extract the required diaTracer version from its error output."""
    import re
    import subprocess as sp
    launcher = fp_dir / "bin" / "fragpipe"
    if not launcher.exists():
        return None
    try:
        r = sp.run([str(launcher), "--headless"], capture_output=True, timeout=10,
                   env={**__import__("os").environ, "JAVA_OPTS": "-Djava.awt.headless=true"})
        output = r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")
        m = re.search(r"diaTracer\s+([\d.]+)\s+is required", output)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def download_diatracer(cfg: dict) -> bool:
    """Download the DiaTracer JAR version required by each FragPipe installation."""
    import json
    import re

    logger.info("Fetching DiaTracer releases from GitHub ...")
    try:
        req = urllib.request.Request(DIATRACER_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.loads(resp.read())
    except Exception as exc:
        logger.error("Could not fetch DiaTracer release info: %s", exc)
        logger.error("Download manually from https://github.com/Nesvilab/diaTracer/releases")
        return False

    def _find_asset(target_version: str | None) -> tuple[str, str] | None:
        """Return (jar_name, url) for target_version, or latest if target_version is None."""
        for release in releases:
            tag = release["tag_name"]
            ver = tag.lstrip("v")
            if target_version and ver != target_version:
                continue
            for asset in release.get("assets", []):
                name = asset["name"]
                if name.endswith(".jar") and re.match(r"diatracer", name, re.IGNORECASE):
                    return name, asset["browser_download_url"]
            # Construct fallback URL
            jar_name = f"diaTracer-{ver}.jar"
            url = f"https://github.com/Nesvilab/diaTracer/releases/download/{tag}/{jar_name}"
            return jar_name, url
        return None

    success = True
    for version_cfg in cfg.get("tools", {}).get("fragpipe", {}).get("versions", []):
        fp_dir = Path(version_cfg.get("dir", ""))
        if not fp_dir.is_dir():
            continue
        tools_dir = fp_dir / "tools"

        required = _get_required_diatracer_version(fp_dir)
        if required:
            logger.info("FragPipe at %s requires diaTracer %s", fp_dir, required)
        else:
            logger.info("Could not detect required diaTracer version; downloading latest.")

        result = _find_asset(required)
        if result is None:
            logger.error("No diaTracer release found for version %s", required)
            success = False
            continue
        jar_name, download_url = result

        dest = tools_dir / jar_name
        if dest.exists():
            logger.info("DiaTracer already present: %s", dest)
            continue
        logger.info("Downloading %s → %s ...", jar_name, dest)
        try:
            urllib.request.urlretrieve(download_url, str(dest), reporthook=_progress)
            print()
        except Exception as exc:
            logger.error("Download failed: %s", exc)
            success = False
            continue
        logger.info("Downloaded: %s", dest)

    return success


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="One-time setup for the ProteoBench pipeline.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--accept-license", action="store_true",
                        help="Accept the MSFragger academic license (required for downloading MSFragger/IonQuant)")
    parser.add_argument("--sage-only",      action="store_true", help="Only compile Sage")
    parser.add_argument("--msfragger-only", action="store_true", help="Only download MSFragger")
    parser.add_argument("--ionquant-only",  action="store_true", help="Only download IonQuant")
    parser.add_argument("--diatracer-only", action="store_true", help="Only download DiaTracer")
    args = parser.parse_args()

    if not args.config.exists():
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    cfg = load_config(args.config)

    exclusive = args.sage_only or args.msfragger_only or args.ionquant_only or args.diatracer_only
    do_sage       = args.sage_only       or not exclusive
    do_msfragger  = args.msfragger_only  or not exclusive
    do_ionquant   = args.ionquant_only   or not exclusive
    do_diatracer  = args.diatracer_only  or not exclusive

    success = True

    if do_sage:
        logger.info("=== Compiling Sage ===")
        if not compile_sage(cfg):
            success = False

    if do_msfragger or do_ionquant:
        if not args.accept_license:
            print(MSFRAGGER_LICENSE_TEXT)
            print("Re-run with --accept-license to proceed with downloads.")
            sys.exit(0)

    if do_msfragger:
        logger.info("=== Downloading MSFragger ===")
        if not download_msfragger(cfg, args.config):
            success = False

    if do_ionquant:
        logger.info("=== Downloading IonQuant ===")
        if not download_ionquant(cfg):
            success = False

    if do_diatracer:
        logger.info("=== Downloading DiaTracer ===")
        if not download_diatracer(cfg):
            success = False

    if success:
        logger.info("Setup complete.")
    else:
        logger.error("Setup finished with errors. See messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
