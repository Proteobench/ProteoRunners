#!/usr/bin/env python3
"""One-time setup: compile Sage from source and download tool binaries.

Usage (interactive — recommended for first-time users):
    python setup.py                         # guided setup, prompts for license

Usage (non-interactive / CI):
    python setup.py --accept-license        # all tasks (Sage + MSFragger + IonQuant + DiaTracer + DIA-NN)
    python setup.py --sage-only             # compile Sage only
    python setup.py --msfragger-only --accept-license
    python setup.py --ionquant-only --accept-license
    python setup.py --diatracer-only        # DiaTracer (free, no license)
    python setup.py --download-diann        # download latest DIA-NN Linux binary

Status check:
    python setup.py --check                 # show which tools are found/missing
"""

from __future__ import annotations

import argparse
import logging
import os
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
DIANN_API_URL = "https://api.github.com/repos/vdemichev/DiaNN/releases/latest"

MSFRAGGER_LICENSE_TEXT = """\
============================================================
  MSFragger Academic License
============================================================
MSFragger is freely available for academic research use.
Commercial use requires a license from the University of Michigan.

By accepting you confirm that you:
  1. Are using MSFragger for non-commercial academic research.
  2. Have read and agree to the MSFragger license at:
     https://msfragger.nesvilab.org/upgrading_msfragger.html
============================================================
"""

NEXT_STEPS_TEXT = """\

============================================================
  Setup complete!  Next steps:
============================================================
  1. Edit config.yaml:
       - Set 'output_dir' under 'global' to your results directory
       - Set dataset paths under 'datasets' to your MS data locations
       - Set 'enabled: true' for the tools you installed

  2. Preview what will run (no files executed):
       python run_proteobench.py --dry-run

  3. Start the pipeline:
       python run_proteobench.py

  Tip: run  python run_proteobench.py --list-tools   to see enabled tools.
============================================================
"""


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict, path: Path) -> None:
    with open(path, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)


def _ask_license() -> bool:
    """Show MSFragger license and ask interactively. Returns True if accepted."""
    print(MSFRAGGER_LICENSE_TEXT)
    try:
        answer = input("Do you accept the MSFragger academic license? [y/N]: ").strip().lower()
        return answer in ("y", "yes")
    except EOFError:
        return False


def _progress(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb = downloaded / 1_048_576
        total_mb = total_size / 1_048_576
        print(f"\r  {pct:3d}%  {mb:.1f}/{total_mb:.1f} MB", end="", flush=True)


# ── Setup status check ────────────────────────────────────────────────────────

def check_setup(cfg: dict) -> bool:
    """Print a status table for all configured tool components. Returns True if all OK."""
    import re
    print("\nProteoBench pipeline setup status:")
    print(f"  {'Component':<20} {'Status':<8} Details")
    print("  " + "-" * 68)

    all_ok = True

    def _row(component: str, ok: bool, detail: str, hint: str = "") -> None:
        nonlocal all_ok
        status = "[ OK ]" if ok else "[MISS]"
        print(f"  {component:<20} {status}  {detail}")
        if not ok and hint:
            print(f"  {'':<20}         {hint}")
        if not ok:
            all_ok = False

    # Sage
    for ver in cfg.get("tools", {}).get("sage", {}).get("versions", []):
        binary = Path(ver.get("binary", ""))
        _row(
            f"Sage ({ver.get('id', '?')})",
            binary.exists(),
            str(binary) if binary.exists() else str(binary),
            "Run: python setup.py --sage-only" if not binary.exists() else "",
        )

    # MSFragger
    msfragger_jar = None
    for ver in cfg.get("tools", {}).get("fragpipe", {}).get("versions", []):
        jar = ver.get("msfragger_jar", "")
        if jar and Path(jar).exists():
            msfragger_jar = jar
            break
    _row(
        "MSFragger",
        msfragger_jar is not None,
        str(msfragger_jar) if msfragger_jar else "not found",
        "Run: python setup.py --accept-license --msfragger-only" if not msfragger_jar else "",
    )

    # IonQuant and DiaTracer per FragPipe version
    for ver in cfg.get("tools", {}).get("fragpipe", {}).get("versions", []):
        fp_dir = Path(ver.get("dir", ""))
        if not fp_dir.is_dir():
            continue
        tools_dir = fp_dir / "tools"
        ver_id = ver.get("id", "?")

        iq_jars = sorted(tools_dir.glob("IonQuant*.jar"))
        _row(
            f"IonQuant ({ver_id})",
            bool(iq_jars),
            str(iq_jars[0]) if iq_jars else f"not found in {tools_dir}",
            f"Run: python setup.py --accept-license --ionquant-only" if not iq_jars else "",
        )

        dt_jars = sorted(tools_dir.glob("diaTracer*.jar"), key=lambda p: p.name.lower()) + \
                  sorted(tools_dir.glob("diatracer*.jar"), key=lambda p: p.name.lower())
        _row(
            f"DiaTracer ({ver_id})",
            bool(dt_jars),
            str(dt_jars[0]) if dt_jars else f"not found in {tools_dir}",
            "Run: python setup.py --diatracer-only" if not dt_jars else "",
        )

    # DIA-NN versions
    for ver in cfg.get("tools", {}).get("diann", {}).get("versions", []):
        binary = Path(ver.get("binary", ""))
        ver_id = ver.get("id", "?")
        _row(
            f"DIA-NN ({ver_id})",
            binary.exists(),
            str(binary),
            "Run: python setup.py --download-diann" if not binary.exists() else "",
        )

    # MaxQuant
    for ver in cfg.get("tools", {}).get("maxquant", {}).get("versions", []):
        mq_dir = Path(ver.get("dir", ""))
        dll = mq_dir / "bin" / "MaxQuantCmd.dll"
        ver_id = ver.get("id", "?")
        _row(
            f"MaxQuant ({ver_id})",
            dll.exists(),
            str(dll),
            "Download from https://www.maxquant.org/ and extract" if not dll.exists() else "",
        )

    # MetaMorpheus
    for ver in cfg.get("tools", {}).get("metamorpheus", {}).get("versions", []):
        mm_dir = Path(ver.get("dir", ""))
        dll = mm_dir / "CMD.dll"
        ver_id = ver.get("id", "?")
        _row(
            f"MetaMorpheus ({ver_id})",
            dll.exists(),
            str(dll),
            "Download from https://github.com/smith-chem-wisc/MetaMorpheus/releases" if not dll.exists() else "",
        )

    print()
    if all_ok:
        print("  All components found.")
    else:
        print("  Some components are missing. See hints above.")
    print()
    return all_ok


# ── Sage compilation ─────────────────────────────────────────────────────────

def compile_sage(cfg: dict) -> bool:
    sage_cfg = cfg.get("tools", {}).get("sage", {})
    for version in sage_cfg.get("versions", []):
        source_dir = Path(version.get("source_dir", ""))
        binary = Path(version.get("binary", ""))
        git_tag = version.get("git_tag", "")

        if not source_dir.exists():
            logger.error("Sage source_dir not found: %s", source_dir)
            logger.error(
                "Set 'source_dir:' under tools > sage > versions in config.yaml "
                "to the path of the Sage git repository. "
                "Clone with: git clone https://github.com/lazear/sage.git %s", source_dir
            )
            return False

        if binary.exists():
            logger.info("Sage binary already found at %s — skipping compilation.", binary)
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
            logger.error(
                "Make sure Rust/cargo is installed: curl https://sh.rustup.rs -sSf | sh"
            )
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
    import json
    req = urllib.request.Request(MSFRAGGER_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    tag = data["tag_name"]
    for asset in data.get("assets", []):
        if asset["name"].endswith(".jar") and "MSFragger" in asset["name"]:
            return tag, asset["browser_download_url"]
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
        logger.info("MSFragger JAR already present: %s — skipping download.", jar_path)
    else:
        logger.info("Downloading MSFragger %s from %s ...", version, url)
        try:
            urllib.request.urlretrieve(url, str(jar_path), reporthook=_progress)
            print()
        except Exception as exc:
            logger.error("Download failed: %s", exc)
            return False
        logger.info("Downloaded: %s", jar_path)

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


# ── IonQuant download ─────────────────────────────────────────────────────────

def download_ionquant(cfg: dict) -> bool:
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
        download_url = f"https://github.com/Nesvilab/IonQuant/releases/download/{tag}/{jar_name}"

    success = True
    for version_cfg in cfg.get("tools", {}).get("fragpipe", {}).get("versions", []):
        fp_dir = Path(version_cfg.get("dir", ""))
        if not fp_dir.is_dir():
            continue
        tools_dir = fp_dir / "tools"
        dest = tools_dir / jar_name
        if dest.exists():
            logger.info("IonQuant already present: %s — skipping.", dest)
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
    import re
    import subprocess as sp
    launcher = fp_dir / "bin" / "fragpipe"
    if not launcher.exists():
        return None
    try:
        r = sp.run([str(launcher), "--headless"], capture_output=True, timeout=10,
                   env={**os.environ, "JAVA_OPTS": "-Djava.awt.headless=true"})
        output = r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")
        m = re.search(r"diaTracer\s+([\d.]+)\s+is required", output)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def download_diatracer(cfg: dict) -> bool:
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
        for release in releases:
            tag = release["tag_name"]
            ver = tag.lstrip("v")
            if target_version and ver != target_version:
                continue
            for asset in release.get("assets", []):
                name = asset["name"]
                if name.endswith(".jar") and re.match(r"diatracer", name, re.IGNORECASE):
                    return name, asset["browser_download_url"]
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
            logger.info("DiaTracer already present: %s — skipping.", dest)
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


# ── DIA-NN download ───────────────────────────────────────────────────────────

def _get_latest_diann_linux_asset() -> tuple[str, str, str] | None:
    """Return (version, filename, download_url) for the latest DIA-NN Linux binary."""
    import json
    req = urllib.request.Request(DIANN_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    tag = data["tag_name"]
    version = tag.lstrip("v")
    for asset in data.get("assets", []):
        name = asset["name"].lower()
        # Match the Linux binary: typically "diann-linux" or "diann-{version}" without extension
        if "linux" in name and not name.endswith((".zip", ".tar.gz", ".md5", ".sha256")):
            return version, asset["name"], asset["browser_download_url"]
    return None


def download_diann(cfg: dict, config_path: Path) -> bool:
    logger.info("Fetching latest DIA-NN release info from GitHub ...")
    try:
        result = _get_latest_diann_linux_asset()
    except Exception as exc:
        logger.error("Could not fetch DIA-NN release info: %s", exc)
        logger.error("Download manually from https://github.com/vdemichev/DiaNN/releases")
        return False

    if result is None:
        logger.error(
            "No Linux binary found in the latest DIA-NN release. "
            "Check https://github.com/vdemichev/DiaNN/releases manually."
        )
        return False

    version, filename, url = result

    # Install into a per-version subdirectory
    install_dir = Path(MSFRAGGER_INSTALL_DIR).parent / f"diann-{version}"
    install_dir.mkdir(parents=True, exist_ok=True)
    dest = install_dir / "diann-linux"

    if dest.exists():
        logger.info("DIA-NN %s already present at %s — skipping download.", version, dest)
    else:
        logger.info("Downloading DIA-NN %s (%s) ...", version, filename)
        try:
            urllib.request.urlretrieve(url, str(dest), reporthook=_progress)
            print()
            dest.chmod(dest.stat().st_mode | 0o111)  # make executable
        except Exception as exc:
            logger.error("Download failed: %s", exc)
            return False
        logger.info("Downloaded and made executable: %s", dest)

    # Update config.yaml: fill in binary for any DIA-NN version entry where binary is empty/CHANGE_ME
    modified = False
    for version_cfg in cfg.get("tools", {}).get("diann", {}).get("versions", []):
        existing_binary = version_cfg.get("binary", "")
        if not existing_binary or "CHANGE_ME" in existing_binary or not Path(existing_binary).exists():
            if version_cfg.get("id") == version:
                version_cfg["binary"] = str(dest)
                version_cfg["enabled"] = True
                modified = True

    if modified:
        save_config(cfg, config_path)
        logger.info(
            "Updated config.yaml: diann version %s binary set to %s and enabled=true", version, dest
        )
    else:
        logger.info(
            "Binary downloaded to %s. If your config.yaml lists a different version, "
            "manually set binary: %s for the matching version entry.", dest, dest
        )

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time setup for the ProteoBench pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup.py                          # interactive guided setup
  python setup.py --check                  # show tool status without downloading anything
  python setup.py --accept-license         # non-interactive full setup
  python setup.py --download-diann         # download latest DIA-NN Linux binary
  python setup.py --sage-only              # compile Sage from source
  python setup.py --diatracer-only         # download DiaTracer (no license needed)
""",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to config.yaml (default: config.yaml next to this script)")
    parser.add_argument("--check", action="store_true",
                        help="Show setup status for all tools and exit")
    parser.add_argument("--accept-license", action="store_true",
                        help="Accept the MSFragger academic license (required for MSFragger/IonQuant downloads)")
    parser.add_argument("--download-diann",  action="store_true", help="Download latest DIA-NN Linux binary")
    parser.add_argument("--sage-only",       action="store_true", help="Only compile Sage from source")
    parser.add_argument("--msfragger-only",  action="store_true", help="Only download MSFragger")
    parser.add_argument("--ionquant-only",   action="store_true", help="Only download IonQuant")
    parser.add_argument("--diatracer-only",  action="store_true", help="Only download DiaTracer")
    args = parser.parse_args()

    if not args.config.exists():
        logger.error("Config file not found: %s", args.config)
        logger.error(
            "Copy the template first: cp config.template.yaml config.yaml  "
            "then edit it to set your paths."
        )
        sys.exit(1)

    cfg = load_config(args.config)

    # Status-check mode: show table and exit
    if args.check:
        ok = check_setup(cfg)
        sys.exit(0 if ok else 1)

    exclusive = args.sage_only or args.msfragger_only or args.ionquant_only or args.diatracer_only or args.download_diann
    do_sage       = args.sage_only      or not exclusive
    do_msfragger  = args.msfragger_only or not exclusive
    do_ionquant   = args.ionquant_only  or not exclusive
    do_diatracer  = args.diatracer_only or not exclusive
    do_diann      = args.download_diann or not exclusive

    success = True

    if do_sage:
        logger.info("=== Compiling Sage ===")
        if not compile_sage(cfg):
            success = False

    # MSFragger and IonQuant require license acceptance
    needs_license = do_msfragger or do_ionquant
    license_accepted = args.accept_license
    if needs_license and not license_accepted:
        if sys.stdin.isatty():
            license_accepted = _ask_license()
            if not license_accepted:
                logger.warning("License not accepted — skipping MSFragger and IonQuant downloads.")
                do_msfragger = False
                do_ionquant = False
        else:
            # Non-interactive mode: print license text and require explicit flag
            print(MSFRAGGER_LICENSE_TEXT)
            print("Re-run with --accept-license to proceed with downloads.")
            do_msfragger = False
            do_ionquant = False

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

    if do_diann:
        logger.info("=== Downloading DIA-NN ===")
        if not download_diann(cfg, args.config):
            success = False

    if success:
        print(NEXT_STEPS_TEXT)
    else:
        logger.error("Setup finished with errors. See messages above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
