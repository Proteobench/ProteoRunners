#!/usr/bin/env python3
"""One-time setup: compile Sage from source and download tool binaries.

License summary
---------------
Tool            License                    Prompt required
-----------     --------------------------  ---------------
Sage            MIT (open source)           no
AlphaDIA        Apache 2.0 (open source)    no
MetaMorpheus    MIT (open source)           no
DIA-NN          Proprietary (Academia free) notice shown
MSFragger       Nesvilab Academic           --accept-license
IonQuant        Nesvilab Academic           --accept-license
DiaTracer       Nesvilab Academic           --accept-license
MaxQuant        Proprietary (Max Planck)    manual download only

Usage (interactive — recommended for first-time users):
    python setup.py                         # guided setup, prompts for licenses

Usage (non-interactive / CI):
    python setup.py --accept-license        # all tasks (Sage + MSFragger + IonQuant + DiaTracer + DIA-NN)
    python setup.py --sage-only             # compile Sage only
    python setup.py --alphadia-only         # pip-install AlphaDIA into configured venv
    python setup.py --metamorpheus-only     # download latest MetaMorpheus release
    python setup.py --msfragger-only --accept-license   # downloads FragPipe bundle (includes MSFragger)
    python setup.py --ionquant-only --accept-license    # download IonQuant via Nesvilab token (interactive)
    python setup.py --diatracer-only --accept-license   # download DiaTracer via Nesvilab token (interactive)
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
FRAGPIPE_API_URL = "https://api.github.com/repos/Nesvilab/FragPipe/releases/latest"
# IonQuant and DiaTracer are downloaded via the Nesvilab academic upgrader (token sent by email).
IONQUANT_UPGRADER_URL = "https://msfragger-upgrader.nesvilab.org/ionquant/"
DIATRACER_UPGRADER_URL = "https://msfragger-upgrader.nesvilab.org/diatracer/"
DIANN_API_URL = "https://api.github.com/repos/vdemichev/DiaNN/releases?per_page=100"
METAMORPHEUS_API_URL = "https://api.github.com/repos/smith-chem-wisc/MetaMorpheus/releases/latest"

# MSFragger, IonQuant, and DiaTracer all share the Nesvilab Academic License.
# Commercial use of any of these tools requires a separate license from Fragmatics
# (https://fragmatics.com). Accept once for all three.
NESVILAB_LICENSE_TEXT = """\
============================================================
  Nesvilab Academic License (MSFragger, IonQuant, DiaTracer)
============================================================
MSFragger, IonQuant, and DiaTracer are freely available for
non-commercial academic and non-profit research.
Commercial use requires a license from Fragmatics (https://fragmatics.com).

By accepting you confirm that you:
  1. Are using these tools for non-commercial academic research.
  2. Have read and agree to the terms at:
     https://msfragger.nesvilab.org/upgrading_msfragger.html
============================================================
"""

DIANN_NOTICE_TEXT = """\
============================================================
  DIA-NN License Notice
============================================================
DIA-NN is available free of charge for non-commercial academic
research (Academia tier). A commercial Enterprise license is
required for commercial applications.

Please review the current license terms before use:
  https://github.com/vdemichev/DiaNN
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
    """Show Nesvilab license and ask interactively. Returns True if accepted."""
    print(NESVILAB_LICENSE_TEXT)
    try:
        answer = input("Do you accept the Nesvilab academic license? [y/N]: ").strip().lower()
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
            "Run: python setup.py --accept-license --diatracer-only" if not dt_jars else "",
        )

    # AlphaDIA
    for ver in cfg.get("tools", {}).get("alphadia", {}).get("versions", []):
        cmd = ver.get("command", "")
        ver_id = ver.get("id", "?")
        found = bool(cmd) and Path(cmd).exists()
        _row(
            f"AlphaDIA ({ver_id})",
            found,
            cmd if found else (cmd or "not configured"),
            "Run: python setup.py --alphadia-only" if not found else "",
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
            logger.info("Sage source_dir not found; cloning https://github.com/lazear/sage.git → %s", source_dir)
            r = subprocess.run(["git", "clone", "https://github.com/lazear/sage.git", str(source_dir)])
            if r.returncode != 0:
                logger.error("git clone failed. Check network access or set 'source_dir:' manually.")
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


# ── FragPipe bundle download (MSFragger + IonQuant ship inside FragPipe) ──────

def _get_fragpipe_linux_bundle() -> tuple[str, str]:
    """Return (tag, download_url) for the latest FragPipe Linux zip bundle."""
    import json
    req = urllib.request.Request(FRAGPIPE_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    tag = data["tag_name"]
    for asset in data.get("assets", []):
        if asset["name"].lower().endswith("-linux.zip"):
            return tag, asset["browser_download_url"]
    raise RuntimeError(f"No Linux zip found in FragPipe release {tag}")


def _extract_zip_to(zip_path: Path, dest_dir: Path) -> None:
    """Extract zip to dest_dir, stripping a single top-level directory if present."""
    import shutil
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        prefix = ""
        first_name = members[0].filename if members else ""
        if "/" in first_name:
            candidate = first_name.split("/")[0] + "/"
            if all(m.filename.startswith(candidate) for m in members):
                prefix = candidate
        for info in members:
            rel = info.filename[len(prefix):] if prefix else info.filename
            if not rel:
                continue
            target = dest_dir / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)



def _ensure_fragpipe_bundle(version_cfgs: list[dict]) -> bool:
    """Download and extract the FragPipe Linux bundle for all given versions.

    The caller is responsible for filtering to versions that actually need the
    bundle; this function always downloads and extracts for each unique dir.
    Deduplication by dir avoids redundant downloads when multiple config entries
    share the same FragPipe installation path.
    """
    import tempfile

    if not version_cfgs:
        return True

    logger.info("Fetching latest FragPipe release info from GitHub ...")
    try:
        tag, url = _get_fragpipe_linux_bundle()
    except Exception as exc:
        logger.error("Could not fetch FragPipe release info: %s", exc)
        logger.error("Download the FragPipe bundle from https://github.com/Nesvilab/FragPipe/releases")
        return False

    seen: set[Path] = set()
    success = True
    for version_cfg in version_cfgs:
        fp_dir = Path(version_cfg.get("dir", ""))
        if fp_dir in seen:
            continue
        seen.add(fp_dir)

        if not fp_dir.parent.exists():
            logger.error("Parent directory does not exist: %s", fp_dir.parent)
            success = False
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / f"FragPipe-{tag}-linux.zip"
            logger.info("Downloading FragPipe %s bundle → %s ...", tag, fp_dir)
            try:
                urllib.request.urlretrieve(url, str(zip_path), reporthook=_progress)
                print()
            except Exception as exc:
                logger.error("Download failed: %s", exc)
                success = False
                continue
            fp_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Extracting FragPipe bundle ...")
            _extract_zip_to(zip_path, fp_dir)

    return success


def download_msfragger(cfg: dict, config_path: Path) -> bool:
    """MSFragger ships inside the FragPipe Linux bundle; download and extract it."""
    versions = cfg.get("tools", {}).get("fragpipe", {}).get("versions", [])
    missing = [v for v in versions if not Path(v.get("msfragger_jar", "")).exists()]
    if not missing:
        logger.info("MSFragger already present in all configured FragPipe versions — skipping.")
        return True

    if not _ensure_fragpipe_bundle(missing):
        return False

    modified = False
    success = True
    for version_cfg in missing:
        fp_dir = Path(version_cfg.get("dir", ""))
        jars = sorted(fp_dir.rglob("MSFragger*.jar"))
        if not jars:
            logger.error("MSFragger jar not found in %s after extraction", fp_dir)
            success = False
            continue
        jar_path = jars[-1]
        version_cfg["msfragger_jar"] = str(jar_path)
        version_cfg["enabled"] = True
        modified = True
        logger.info("MSFragger jar: %s", jar_path)

    if modified:
        save_config(cfg, config_path)
        logger.info("Updated config.yaml with MSFragger jar paths.")

    return success


# ── Nesvilab academic upgrader helpers (IonQuant + DiaTracer) ─────────────────

def _nesvilab_latest_version(base_url: str) -> str:
    """Fetch the latest version string from a Nesvilab upgrader endpoint."""
    req = urllib.request.Request(
        base_url + "latest_version.php",
        headers={"User-Agent": "proteobench-setup/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode().strip()


def _nesvilab_download(base_url: str, version: str, token: str, dest: Path) -> bool:
    """Download a JAR using the token received by email from the Nesvilab upgrader."""
    import urllib.parse
    encoded = urllib.parse.quote(f"{version}$jar")
    url = f"{base_url}download.php?token={token}&download={encoded}"
    logger.info("Downloading %s ...", url)
    try:
        urllib.request.urlretrieve(url, str(dest), reporthook=_progress)
        print()
    except Exception as exc:
        logger.error("Download failed: %s", exc)
        return False
    if dest.stat().st_size < 1_000_000:
        logger.error(
            "Downloaded file is too small (%d bytes) — token may be invalid or expired.",
            dest.stat().st_size,
        )
        dest.unlink(missing_ok=True)
        return False
    return True


def _nesvilab_get_token(base_url: str, version: str, tool_name: str) -> str | None:
    """Guide the user through obtaining a Nesvilab download token (sent by email)."""
    print(f"\n  To download {tool_name} you need a one-time validation code.")
    print(f"  1. Open: {base_url}")
    print(f"  2. Select version {version}, fill in your academic details, and click Download.")
    print(f"  3. A token will be sent to your email address.")
    print(f"  4. Paste the token below.")
    try:
        token = input(f"  {tool_name} token: ").strip()
    except EOFError:
        return None
    return token if token else None


# ── IonQuant ──────────────────────────────────────────────────────────────────

def download_ionquant(cfg: dict) -> bool:
    """Download IonQuant via the Nesvilab academic upgrader (token sent by email)."""
    versions = cfg.get("tools", {}).get("fragpipe", {}).get("versions", [])

    def _has_ionquant(v: dict) -> bool:
        tools_dir = Path(v.get("dir", "")) / "tools"
        return tools_dir.is_dir() and bool(sorted(tools_dir.glob("IonQuant*.jar")))

    missing = [v for v in versions if not _has_ionquant(v)]
    if not missing:
        for v in versions:
            tools_dir = Path(v.get("dir", "")) / "tools"
            jars = sorted(tools_dir.glob("IonQuant*.jar"))
            logger.info("IonQuant (%s): %s", v.get("id", "?"), jars[-1])
        return True

    try:
        latest = _nesvilab_latest_version(IONQUANT_UPGRADER_URL)
    except Exception as exc:
        logger.error("Could not fetch latest IonQuant version: %s", exc)
        return False
    logger.info("IonQuant latest version from server: %s", latest)

    if not sys.stdin.isatty():
        logger.error(
            "IonQuant download requires an interactive token. "
            "Run setup.py interactively or place IonQuant-%s.jar in {fragpipe_dir}/tools/ manually "
            "(download from %s).",
            latest, IONQUANT_UPGRADER_URL,
        )
        return False

    token = _nesvilab_get_token(IONQUANT_UPGRADER_URL, latest, "IonQuant")
    if not token:
        logger.warning("No token entered — IonQuant download skipped.")
        return False

    seen: set[Path] = set()
    success = True
    for version_cfg in missing:
        tools_dir = Path(version_cfg.get("dir", "")) / "tools"
        if tools_dir in seen:
            continue
        seen.add(tools_dir)
        tools_dir.mkdir(parents=True, exist_ok=True)
        jar_path = tools_dir / f"IonQuant-{latest}.jar"
        logger.info("Downloading IonQuant %s → %s ...", latest, jar_path)
        if not _nesvilab_download(IONQUANT_UPGRADER_URL, latest, token, jar_path):
            success = False
        else:
            logger.info("IonQuant installed: %s", jar_path)
    return success


# ── DiaTracer ─────────────────────────────────────────────────────────────────

def download_diatracer(cfg: dict) -> bool:
    """Download DiaTracer via the Nesvilab academic upgrader (token sent by email)."""
    versions = cfg.get("tools", {}).get("fragpipe", {}).get("versions", [])

    def _has_diatracer(v: dict) -> bool:
        tools_dir = Path(v.get("dir", "")) / "tools"
        return tools_dir.is_dir() and bool(
            sorted(tools_dir.glob("diaTracer*.jar")) + sorted(tools_dir.glob("diatracer*.jar"))
        )

    missing = [v for v in versions if not _has_diatracer(v)]
    if not missing:
        for v in versions:
            tools_dir = Path(v.get("dir", "")) / "tools"
            jars = (
                sorted(tools_dir.glob("diaTracer*.jar")) +
                sorted(tools_dir.glob("diatracer*.jar"))
            )
            logger.info("DiaTracer (%s): %s", v.get("id", "?"), jars[-1])
        return True

    try:
        latest = _nesvilab_latest_version(DIATRACER_UPGRADER_URL)
    except Exception as exc:
        logger.error("Could not fetch latest DiaTracer version: %s", exc)
        return False
    logger.info("DiaTracer latest version from server: %s", latest)

    if not sys.stdin.isatty():
        logger.error(
            "DiaTracer download requires an interactive token. "
            "Run setup.py interactively or place diaTracer-%s.jar in {fragpipe_dir}/tools/ manually "
            "(download from %s).",
            latest, DIATRACER_UPGRADER_URL,
        )
        return False

    token = _nesvilab_get_token(DIATRACER_UPGRADER_URL, latest, "DiaTracer")
    if not token:
        logger.warning("No token entered — DiaTracer download skipped.")
        return False

    seen: set[Path] = set()
    success = True
    for version_cfg in missing:
        tools_dir = Path(version_cfg.get("dir", "")) / "tools"
        if tools_dir in seen:
            continue
        seen.add(tools_dir)
        tools_dir.mkdir(parents=True, exist_ok=True)
        jar_path = tools_dir / f"diaTracer-{latest}.jar"
        logger.info("Downloading DiaTracer %s → %s ...", latest, jar_path)
        if not _nesvilab_download(DIATRACER_UPGRADER_URL, latest, token, jar_path):
            success = False
        else:
            logger.info("DiaTracer installed: %s", jar_path)
    return success


# ── DIA-NN download ───────────────────────────────────────────────────────────

def _get_diann_linux_assets() -> dict[str, tuple[str, str]]:
    """Return {version: (filename, download_url)} for all DIA-NN Linux ZIPs across all releases.

    Handles two naming conventions:
      2.x:   DIA-NN-2.5.0-Academia-Linux.zip  (all 2.x assets live in the single "2.0" release tag)
      1.9.x: diann-1.9.2.Linux.zip            (each version has its own release tag)
    For the same version, stable > preview and update > plain.
    """
    import json
    import re
    req = urllib.request.Request(DIANN_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.loads(resp.read())

    # 2.x: DIA-NN-2.5.0-Academia-Linux[-Preview].zip → version from filename
    new_pat = re.compile(
        r"DIA-NN-(\d+\.\d+(?:\.\d+)?)-Academia-Linux(?:-Preview)?\.zip", re.IGNORECASE
    )
    # 1.9.x: diann-1.9.2.Linux[_update_YYYY-MM-DD].zip → version from filename
    old_pat = re.compile(
        r"diann-(\d+\.\d+(?:\.\d+)?)\.Linux(?:_[^.]+)?\.zip", re.IGNORECASE
    )

    assets: dict[str, tuple[str, str]] = {}
    for release in releases:
        for asset in release.get("assets", []):
            name = asset["name"]
            url = asset["browser_download_url"]
            m = new_pat.match(name) or old_pat.match(name)
            if not m:
                continue
            version = m.group(1)
            is_preview = "preview" in name.lower()
            is_update = "_update_" in name.lower()
            existing = assets.get(version)
            if existing is None:
                assets[version] = (name, url)
            elif is_preview and "preview" not in existing[0].lower():
                pass  # never overwrite stable with preview
            elif is_update and "_update_" not in existing[0].lower():
                assets[version] = (name, url)  # prefer update patch over plain release
    return assets


def download_diann(cfg: dict, config_path: Path) -> bool:
    import tempfile
    import zipfile

    print(DIANN_NOTICE_TEXT)
    logger.info("Fetching DIA-NN release assets from GitHub ...")
    try:
        assets = _get_diann_linux_assets()
    except Exception as exc:
        logger.error("Could not fetch DIA-NN release info: %s", exc)
        logger.error("Download manually from https://github.com/vdemichev/DiaNN/releases")
        return False

    if not assets:
        logger.error(
            "No Linux ZIPs found in the DIA-NN release. "
            "Check https://github.com/vdemichev/DiaNN/releases manually."
        )
        return False

    success = True
    modified = False

    for version_cfg in cfg.get("tools", {}).get("diann", {}).get("versions", []):
        ver_id = version_cfg.get("id", "")
        dest = Path(version_cfg.get("binary", ""))

        if not version_cfg.get("binary"):
            logger.warning("DIA-NN version %s has no 'binary:' path in config.yaml — skipping.", ver_id)
            continue

        if dest.exists():
            logger.info("DIA-NN %s already present at %s — skipping.", ver_id, dest)
            continue

        if ver_id not in assets:
            logger.warning(
                "DIA-NN %s: no matching release asset found. Available versions: %s.",
                ver_id, ", ".join(sorted(assets.keys())),
            )
            continue

        filename, url = assets[ver_id]
        dest.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / filename
            logger.info("Downloading DIA-NN %s (%s) → %s ...", ver_id, filename, dest)
            try:
                urllib.request.urlretrieve(url, str(zip_path), reporthook=_progress)
                print()
            except Exception as exc:
                logger.error("Download failed: %s", exc)
                success = False
                continue

            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(dest.parent)

        # The zip may extract into a subdirectory; search recursively for the binary.
        binary_path = None
        for name_candidate in ("diann-linux", "diann"):
            hits = [f for f in dest.parent.rglob(name_candidate) if f.is_file()]
            if hits:
                binary_path = hits[0]
                break

        if binary_path is None:
            logger.error("Could not locate diann binary in %s", dest.parent)
            success = False
            continue

        if binary_path != dest:
            binary_path.rename(dest)

        dest.chmod(dest.stat().st_mode | 0o111)
        logger.info("Installed DIA-NN %s at %s", ver_id, dest)
        version_cfg["enabled"] = True
        modified = True

    if modified:
        save_config(cfg, config_path)
        logger.info("Updated config.yaml: set enabled=true for downloaded DIA-NN versions.")

    return success


# ── AlphaDIA install ──────────────────────────────────────────────────────────

def install_alphadia(cfg: dict) -> bool:
    """pip-install AlphaDIA using the Python interpreter set in global.python."""
    alphadia_cfg = cfg.get("tools", {}).get("alphadia", {})
    versions = alphadia_cfg.get("versions", [])
    if not versions:
        logger.warning("No alphadia version entries found in config.")
        return False

    python = cfg.get("global", {}).get("python", "python3")
    success = True
    for version in versions:
        ver_id = version.get("id", "?")
        logger.info("Installing alphadia==%s using %s ...", ver_id, python)
        r = subprocess.run(
            [python, "-m", "pip", "install", f"alphadia=={ver_id}"],
            capture_output=False,
        )
        if r.returncode != 0:
            logger.error("pip install alphadia==%s failed.", ver_id)
            success = False
        else:
            logger.info("alphadia==%s installed successfully.", ver_id)

    return success


# ── MetaMorpheus download ─────────────────────────────────────────────────────

def download_metamorpheus(cfg: dict) -> bool:
    """Download the latest MetaMorpheus release and extract to configured dir."""
    import json
    import zipfile

    mm_cfg = cfg.get("tools", {}).get("metamorpheus", {})
    versions = mm_cfg.get("versions", [])
    if not versions:
        logger.warning("No metamorpheus version entries found in config.")
        return False

    logger.info("Fetching latest MetaMorpheus release info from GitHub ...")
    try:
        req = urllib.request.Request(METAMORPHEUS_API_URL, headers={"User-Agent": "proteobench-setup/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.error("Could not fetch MetaMorpheus release info: %s", exc)
        logger.error("Download manually from https://github.com/smith-chem-wisc/MetaMorpheus/releases")
        return False

    tag = data["tag_name"]
    version = tag.lstrip("v")
    # Pick the command-line zip; skip the Windows .msi installer.
    zip_url = None
    zip_name = None
    for asset in data.get("assets", []):
        name = asset["name"]
        if name.endswith(".zip") and not name.endswith(".msi"):
            zip_url = asset["browser_download_url"]
            zip_name = name
            break
    if zip_url is None:
        logger.error("No ZIP found in MetaMorpheus release %s.", tag)
        logger.error("Download manually from https://github.com/smith-chem-wisc/MetaMorpheus/releases")
        return False

    success = True
    for version_cfg in versions:
        mm_dir = Path(version_cfg.get("dir", ""))
        if not mm_dir.parent.exists():
            logger.error("Parent directory does not exist for metamorpheus dir: %s", mm_dir)
            success = False
            continue

        dll = mm_dir / "CMD.dll"
        if dll.exists():
            logger.info("MetaMorpheus CMD.dll already present at %s — skipping.", dll)
            continue

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / zip_name
            logger.info("Downloading MetaMorpheus %s (%s) ...", version, zip_name)
            try:
                urllib.request.urlretrieve(zip_url, str(zip_path), reporthook=_progress)
                print()
            except Exception as exc:
                logger.error("Download failed: %s", exc)
                success = False
                continue

            mm_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Extracting to %s ...", mm_dir)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(mm_dir)

        if not dll.exists():
            logger.error("Extraction complete but CMD.dll not found at %s", dll)
            success = False
        else:
            logger.info("MetaMorpheus %s installed at %s", version, mm_dir)

    return success


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time setup for the ProteoBench pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup.py                                    # interactive guided setup
  python setup.py --check                            # show tool status without downloading anything
  python setup.py --accept-license                   # non-interactive full setup
  python setup.py --sage-only                        # compile Sage from source
  python setup.py --alphadia-only                    # pip-install AlphaDIA into configured venv
  python setup.py --metamorpheus-only                # download latest MetaMorpheus release
  python setup.py --msfragger-only --accept-license  # download MSFragger only
  python setup.py --ionquant-only --accept-license   # download IonQuant only
  python setup.py --diatracer-only --accept-license  # download DiaTracer only
  python setup.py --download-diann                   # download latest DIA-NN Linux binary
""",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to config.yaml (default: config.yaml next to this script)")
    parser.add_argument("--check", action="store_true",
                        help="Show setup status for all tools and exit")
    parser.add_argument("--accept-license", action="store_true",
                        help="Accept the Nesvilab Academic License (required for MSFragger, IonQuant, and DiaTracer downloads)")
    parser.add_argument("--download-diann",      action="store_true", help="Download latest DIA-NN Linux binary")
    parser.add_argument("--sage-only",           action="store_true", help="Only compile Sage from source")
    parser.add_argument("--alphadia-only",       action="store_true", help="Only pip-install AlphaDIA into configured venv")
    parser.add_argument("--metamorpheus-only",   action="store_true", help="Only download latest MetaMorpheus release")
    parser.add_argument("--msfragger-only",      action="store_true", help="Only download MSFragger (requires --accept-license)")
    parser.add_argument("--ionquant-only",       action="store_true", help="Only download IonQuant (requires --accept-license)")
    parser.add_argument("--diatracer-only",      action="store_true", help="Only download DiaTracer (requires --accept-license)")
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

    exclusive = (
        args.sage_only or args.alphadia_only or args.metamorpheus_only
        or args.msfragger_only or args.ionquant_only or args.diatracer_only
        or args.download_diann
    )
    do_sage           = args.sage_only          or not exclusive
    do_alphadia       = args.alphadia_only       or not exclusive
    do_metamorpheus   = args.metamorpheus_only   or not exclusive
    do_msfragger      = args.msfragger_only      or not exclusive
    do_ionquant       = args.ionquant_only       or not exclusive
    do_diatracer      = args.diatracer_only      or not exclusive
    do_diann          = args.download_diann      or not exclusive

    success = True

    if do_sage:
        logger.info("=== Compiling Sage ===")
        if not compile_sage(cfg):
            success = False

    if do_alphadia:
        logger.info("=== Installing AlphaDIA ===")
        if not install_alphadia(cfg):
            success = False

    if do_metamorpheus:
        logger.info("=== Downloading MetaMorpheus ===")
        if not download_metamorpheus(cfg):
            success = False

    # MSFragger, IonQuant, and DiaTracer all require Nesvilab license acceptance
    needs_license = do_msfragger or do_ionquant or do_diatracer
    license_accepted = args.accept_license
    if needs_license and not license_accepted:
        if sys.stdin.isatty():
            license_accepted = _ask_license()
            if not license_accepted:
                logger.warning("License not accepted — skipping MSFragger, IonQuant, and DiaTracer downloads.")
                do_msfragger = False
                do_ionquant = False
                do_diatracer = False
        else:
            # Non-interactive mode: print license text and require explicit flag
            print(NESVILAB_LICENSE_TEXT)
            print("Re-run with --accept-license to proceed with downloads.")
            do_msfragger = False
            do_ionquant = False
            do_diatracer = False

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
