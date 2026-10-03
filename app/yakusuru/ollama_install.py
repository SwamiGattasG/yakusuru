"""Install and locate Ollama (the local LLM runtime) without admin rights.

macOS    Ollama-darwin.zip → /Applications (or ~/Applications if that isn't writable)
Windows  OllamaSetup.exe, silent per-user install (%LOCALAPPDATA%\\Programs\\Ollama)
Linux    portable archive → the app's tools folder (no sudo, no system service)

All downloads come from ollama.com's official download endpoints."""
from __future__ import annotations

import io
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger(__name__)

BASE = "https://ollama.com/download"
Progress = Callable[[float, str], None]       # (fraction 0..1 or -1 when unknown, message)


# --------------------------------------------------------------------------- locating
def mac_app() -> Path | None:
    for d in (Path("/Applications"), Path.home() / "Applications"):
        app = d / "Ollama.app"
        if app.exists():
            return app
    return None


def find_binary() -> str:
    """Path to the `ollama` executable, or "" (the macOS app can run without one on PATH)."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    home = Path.home()
    cands = [Path("/usr/local/bin/ollama"), Path("/opt/homebrew/bin/ollama"), home / ".homebrew/bin/ollama",
             Path("/usr/bin/ollama"), home / ".local/bin/ollama"]
    app = mac_app()
    if app:
        cands.append(app / "Contents/Resources/ollama")
    if os.name == "nt":
        cands.append(Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")))
    tool = paths.tools_dir() / "ollama"
    if tool.exists():
        cands += [p for p in tool.rglob("ollama") if p.is_file()]
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return ""


def is_installed() -> bool:
    return bool(find_binary() or mac_app())


def start() -> None:
    """Start the Ollama server in the background (no window, no focus steal)."""
    app = mac_app()
    if sys.platform == "darwin" and app:
        subprocess.Popen(["open", "-g", "-j", str(app)])
        return
    exe = find_binary()
    if not exe:
        raise RuntimeError("Ollama is not installed.")
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # detached, no console
    else:
        kw["start_new_session"] = True
    subprocess.Popen([exe, "serve"], **kw)


# --------------------------------------------------------------------------- installing
def _download(url: str, dest: Path, progress: Progress, cancelled: Callable[[], bool], label: str) -> Path:
    import requests
    with requests.get(url, stream=True, timeout=(15, 120)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done, t0 = 0, time.time()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if cancelled():
                    raise InterruptedError()
                f.write(chunk)
                done += len(chunk)
                rate = done / max(0.5, time.time() - t0)
                if total:
                    eta = (total - done) / rate if rate else 0
                    progress(done / total, f"{label} · {done / 1e6:,.0f} / {total / 1e6:,.0f} MB · "
                                           f"{rate / 1e6:,.1f} MB/s · ~{int(eta // 60)}:{int(eta % 60):02d} left")
                else:
                    progress(-1, f"{label} · {done / 1e6:,.0f} MB")
    return dest


def _install_macos(tmp: Path, progress: Progress, cancelled) -> str:
    z = _download(f"{BASE}/Ollama-darwin.zip", tmp / "Ollama-darwin.zip", progress, cancelled,
                  "Downloading Ollama")
    progress(-1, "Unpacking…")
    out = tmp / "unpacked"
    out.mkdir()
    # ditto keeps the app's symlinks, permissions and code signature intact (zipfile would not).
    subprocess.run(["ditto", "-x", "-k", str(z), str(out)], check=True)
    app = out / "Ollama.app"
    if not app.exists():
        raise RuntimeError("The Ollama download didn't contain Ollama.app.")
    for dest_dir in (Path("/Applications"), Path.home() / "Applications"):
        try:
            dest_dir.mkdir(exist_ok=True)
            if not os.access(dest_dir, os.W_OK):
                continue
            dest = dest_dir / "Ollama.app"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(app), str(dest))
            log.info("Installed Ollama to %s", dest)
            return str(dest)
        except OSError as e:
            log.warning("Could not install Ollama into %s: %s", dest_dir, e)
    raise RuntimeError("Couldn't write to /Applications or ~/Applications.")


def _install_windows(tmp: Path, progress: Progress, cancelled) -> str:
    exe = _download(f"{BASE}/OllamaSetup.exe", tmp / "OllamaSetup.exe", progress, cancelled,
                    "Downloading Ollama")
    progress(-1, "Installing Ollama (this takes a minute)…")
    # Inno Setup installer: per-user install, no admin prompt.
    r = subprocess.run([str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
                       creationflags=0x08000000)
    if r.returncode != 0:
        raise RuntimeError(f"The Ollama installer exited with code {r.returncode}.")
    found = find_binary()
    if not found:
        raise RuntimeError("Ollama installed, but ollama.exe wasn't found afterwards.")
    return found


def _extract_tar_zst(data_path: Path, dest: Path) -> None:
    try:
        from compression import zstd   # Python 3.14+
        with zstd.open(data_path) as zf, tarfile.open(fileobj=zf, mode="r|") as tf:
            tf.extractall(dest, filter="data")
        return
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore
        with open(data_path, "rb") as fh:
            reader = zstandard.ZstdDecompressor().stream_reader(fh)
            with tarfile.open(fileobj=reader, mode="r|") as tf:
                tf.extractall(dest, filter="data")
        return
    except ImportError:
        pass
    if shutil.which("zstd"):
        p = subprocess.run(["zstd", "-dc", str(data_path)], capture_output=True, check=True)
        with tarfile.open(fileobj=io.BytesIO(p.stdout), mode="r:") as tf:
            tf.extractall(dest, filter="data")
        return
    raise RuntimeError("Need zstd to unpack Ollama: install the 'zstd' package, or use Python 3.14+.")


def _install_linux(tmp: Path, progress: Progress, cancelled) -> str:
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower())
    if not arch:
        raise RuntimeError(f"Ollama has no Linux build for {platform.machine()}.")
    dest = paths.tools_dir() / "ollama"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    import requests
    for ext in (".tar.zst", ".tgz"):
        url = f"{BASE}/ollama-linux-{arch}{ext}"
        try:
            f = _download(url, tmp / f"ollama{ext}", progress, cancelled, "Downloading Ollama")
        except requests.HTTPError:
            continue
        progress(-1, "Unpacking…")
        if ext == ".tgz":
            with tarfile.open(f, "r:gz") as tf:
                tf.extractall(dest, filter="data")
        else:
            _extract_tar_zst(f, dest)
        break
    else:
        raise RuntimeError("Couldn't download Ollama for Linux.")
    for p in dest.rglob("ollama"):
        if p.is_file():
            p.chmod(0o755)
            return str(p)
    raise RuntimeError("The Ollama archive didn't contain the ollama program.")


def install(progress: Progress = lambda f, m: None, cancelled: Callable[[], bool] = lambda: False) -> str:
    """Download and install Ollama for this OS, then start it. Returns where it was installed."""
    with tempfile.TemporaryDirectory(prefix="yakusuru-ollama-") as td:
        tmp = Path(td)
        if sys.platform == "darwin":
            where = _install_macos(tmp, progress, cancelled)
        elif os.name == "nt":
            where = _install_windows(tmp, progress, cancelled)
        else:
            where = _install_linux(tmp, progress, cancelled)
    progress(-1, "Starting Ollama…")
    start()
    return where
