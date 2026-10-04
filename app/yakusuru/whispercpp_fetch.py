"""Download a prebuilt whisper.cpp (whisper-cli) into the app's tools folder.

The project's newest releases often carry only source code, so this looks through recent releases
for the first one that has a ready-made build for this computer:

  Windows x64, NVIDIA   whisper-cublas-12.4.0-bin-x64.zip   (GPU)
  Windows x64           whisper-blas-bin-x64.zip, else whisper-bin-x64.zip   (CPU)
  Windows ARM           whisper-bin-win-cpu-arm64.zip
  Linux                 whisper-bin-ubuntu-x64.tar.gz / -arm64.tar.gz
  macOS                 none: Homebrew (`brew install whisper-cpp`) is the way there

No Qt here: the setup wizard runs it as `python -m yakusuru.tools.fetch whispercpp [cuda]`."""
from __future__ import annotations

import os
import re
import tarfile
import zipfile
from pathlib import Path

RELEASES = "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=30"


def wanted_assets(osk: str, arch: str, nvidia: bool = False) -> list[str]:
    """Asset names to look for, best first (exact names; versions inside names are matched by pattern)."""
    if osk == "windows":
        if arch == "arm64":
            return [r"whisper-bin-win-cpu-arm64\.zip"]
        out = [r"whisper-cublas-12[\d.]*-bin-x64\.zip"] if nvidia else []
        return out + [r"whisper-blas-bin-x64\.zip", r"whisper-bin-x64\.zip"]
    if osk == "linux":
        return [rf"whisper-bin-ubuntu-{'arm64' if arch == 'arm64' else 'x64'}\.tar\.gz"]
    return []


def pick(releases: list[dict], patterns: list[str]) -> tuple[str, str, str, int] | None:
    """(tag, asset name, url, size) of the best match: pattern order first, then newest release."""
    for pat in patterns:
        rx = re.compile(pat + r"$", re.I)
        for rel in releases:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            for a in rel.get("assets", []):
                if rx.match(a.get("name", "")):
                    return rel.get("tag_name", ""), a["name"], a["browser_download_url"], a.get("size", 0)
    return None


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.namelist():
                if not (dest / m).resolve().is_relative_to(root):
                    raise ValueError(f"unsafe path in archive: {m}")
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if not (dest / m.name).resolve().is_relative_to(root):
                    raise ValueError(f"unsafe path in archive: {m.name}")
            try:
                t.extractall(dest, filter="data")
            except TypeError:          # Python without extraction filters
                t.extractall(dest)


def find_cli(folder: Path) -> Path | None:
    exe = ".exe" if os.name == "nt" else ""
    for name in ("whisper-cli", "main"):
        for p in sorted(folder.rglob(name + exe), key=lambda p: len(p.parts)):
            if p.is_file():
                return p
    return None


def install(osk: str, arch: str, nvidia: bool = False, log=print) -> Path:
    import requests
    from . import paths
    pats = wanted_assets(osk, arch, nvidia)
    if not pats:
        raise RuntimeError("No prebuilt whisper.cpp for this system. On a Mac, use Homebrew: brew install whisper-cpp")
    log("Looking for a prebuilt whisper.cpp on GitHub…")
    r = requests.get(RELEASES, timeout=20, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    hit = pick(r.json(), pats)
    if not hit:
        raise RuntimeError("None of the recent whisper.cpp releases has a build for this computer.")
    tag, name, url, size = hit
    log(f"Downloading {name} from release {tag} ({size / 1e6:.0f} MB)…")
    base = paths.tools_dir() / "whisper.cpp"
    dest = base / re.sub(r"\.(zip|tar\.gz)$", "", name)
    base.mkdir(parents=True, exist_ok=True)
    archive = base / name
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(archive, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                f.write(chunk)
    log("Extracting…")
    extract(archive, dest)
    archive.unlink(missing_ok=True)
    cli = find_cli(dest)
    if not cli:
        raise RuntimeError(f"whisper-cli was not found inside {name}.")
    if os.name != "nt":
        cli.chmod(0o755)
    log(f"✓ whisper.cpp ready: {cli}")
    return cli
