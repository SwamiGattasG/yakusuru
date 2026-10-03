#!/usr/bin/env python3
"""Yakusuru bootstrapper — standard library only.

1. Checks the Python version.
2. Creates a private virtual environment in the per-user app-data folder
   (NOT next to this file, so iCloud/OneDrive never syncs gigabytes of packages):
     macOS   ~/Library/Application Support/Yakusuru/venv
     Windows %LOCALAPPDATA%\\Yakusuru\\venv
     Linux   ~/.local/share/Yakusuru/venv
3. Installs the core requirements (once, or when requirements/core.txt changes).
4. Starts the app. GPU engines are installed afterwards by the in-app Setup Wizard.

Usage:  python bootstrap.py [--reset] [--setup] [--doctor] [--no-launch] [files…]
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQ = HERE / "requirements" / "core.txt"

sys.dont_write_bytecode = True   # keep the (possibly cloud-synced) project folder clean
sys.path.insert(0, str(HERE))
from yakusuru.paths import data_dir  # noqa: E402  (stdlib-only module)

VENV = data_dir() / "venv"
PYCACHE = data_dir() / "pycache"
MARKER = VENV / ".core-installed"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def venv_python(gui: bool = False) -> Path:
    if os.name == "nt":
        exe = "pythonw.exe" if gui else "python.exe"
        return VENV / "Scripts" / exe
    return VENV / "bin" / "python"


def req_hash() -> str:
    return hashlib.sha256(REQ.read_bytes()).hexdigest()[:16]


def py_tag() -> str:
    import platform
    return f"py{sys.version_info[0]}.{sys.version_info[1]}-{platform.machine()}"


def read_marker() -> tuple[str, str]:
    try:
        req, _, tag = MARKER.read_text(encoding="utf-8").strip().partition("|")
        return req, tag
    except OSError:
        return "", ""


def check_python() -> None:
    """Refuse Pythons that can't install native packages on this OS + CPU architecture."""
    from yakusuru import platform_matrix as pm
    ok, why = pm.check_python()
    hw, pa = pm.hardware_arch(), pm.python_arch()
    say(f"• Machine: {pm.os_key()} {hw} · Python {sys.version.split()[0]} ({pa}) at {sys.executable}")
    if not ok:
        say(f"✗ {why}")
        sys.exit(2)


def create_venv() -> None:
    say(f"• Creating private environment in {VENV} …")
    try:
        venv.EnvBuilder(with_pip=True, clear=True, upgrade_deps=False).create(VENV)
    except Exception as e:
        say(f"✗ Could not create the virtual environment: {e}")
        if sys.platform.startswith("linux"):
            say("  On Debian/Ubuntu install the venv module first:  sudo apt install python3-venv")
        sys.exit(3)


def pip(*args: str) -> int:
    cmd = [str(venv_python()), "-m", "pip", *args]
    return subprocess.call(cmd)


def install_core() -> None:
    say("• Installing the app's core packages (one time, ~150 MB)…")
    if pip("install", "--upgrade", "pip", "--disable-pip-version-check", "-q") != 0:
        say("! Could not upgrade pip — continuing with the bundled version.")
    if pip("install", "--disable-pip-version-check", "-r", str(REQ)) != 0:
        say("✗ Installing core packages failed. Check your internet connection and run the launcher again.")
        say("  If it keeps failing, run with --reset to start from a clean environment.")
        sys.exit(4)
    MARKER.write_text(f"{req_hash()}|{py_tag()}", encoding="utf-8")
    say("✓ Core packages installed.")


def ensure_env(reset: bool = False) -> None:
    if reset and VENV.exists():
        say("• Removing the old environment…")
        shutil.rmtree(VENV, ignore_errors=True)
    req, tag = read_marker()
    if venv_python().exists() and tag and tag != py_tag():
        say(f"• Python changed ({tag} → {py_tag()}) — rebuilding the environment.")
        shutil.rmtree(VENV, ignore_errors=True)
        req = ""
    if not venv_python().exists():
        create_venv()
        req = ""
    if req != req_hash():
        install_core()


def launch(args: list[str]) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONPYCACHEPREFIX"] = str(PYCACHE)   # compiled files go to app-data, not next to the code
    if os.name == "nt":
        gui_py = venv_python(gui=True)
        exe = gui_py if gui_py.exists() and "--doctor" not in args else venv_python()
        flags = (0x00000008 | 0x00000200) if exe == gui_py else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        p = subprocess.Popen([str(exe), "-m", "yakusuru", *args], cwd=str(HERE), env=env, creationflags=flags)
        if exe != gui_py:
            return p.wait()
        say("✓ Yakusuru started. You can close this window.")
        return 0
    os.chdir(HERE)
    py = str(venv_python())
    os.execve(py, [py, "-m", "yakusuru", *args], env)
    return 0  # not reached


def main() -> int:
    args = sys.argv[1:]
    reset = "--reset" in args
    no_launch = "--no-launch" in args
    args = [a for a in args if a not in ("--reset", "--no-launch")]
    say("Yakusuru — starting up")
    check_python()
    ensure_env(reset)
    if no_launch:
        say("✓ Environment ready.")
        return 0
    return launch(args)


if __name__ == "__main__":
    sys.exit(main())
