"""Platform / architecture compatibility rules (standard library only).

Verified against PyPI in Oct 2026 by resolving every component for each target
(`uv pip compile --python-platform … --python-version …  --only-binary :all:`):

  OS / CPU              Python      PyTorch   faster-whisper   MLX     GUI (PySide6)
  macOS arm64 (M1–M4)   3.10–3.14   ✓ (MPS)   ✓ (CPU, int8)    ✓ *     ✓
  macOS x86_64 (Intel)  3.10–3.12   ✓ ≤3.12   ✓ ≤3.13          ✗       ✓
  Windows x64           3.10–3.14   ✓         ✓                ✗       ✓
  Windows arm64         3.10–3.13   ✗         ✗                ✗       ✓ ≤3.13
  Linux x86_64          3.10–3.14   ✓         ✓                ✗       ✓
  Linux aarch64         3.10–3.14   ✓         ✓                ✗       ✓ (glibc ≥ 2.39)
  * MLX wheels need macOS 14 (Sonoma) or newer.

Rules are *native only*: an Intel (x86_64) Python on an Apple Silicon Mac — i.e. running
under Rosetta — is rejected, because nothing it installs could use the Apple GPU.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys

# Highest Python minor version each platform supports for the core app, and the order
# launchers try versions in (newest first where everything is available).
PY_MIN = (3, 10)
PY_MAX = {
    ("macos", "arm64"): (3, 14),
    ("macos", "x86_64"): (3, 12),
    ("windows", "x86_64"): (3, 14),
    ("windows", "arm64"): (3, 13),
    ("linux", "x86_64"): (3, 14),
    ("linux", "arm64"): (3, 14),
}


def os_key() -> str:
    s = platform.system()
    return {"Darwin": "macos", "Windows": "windows"}.get(s, "linux")


def _norm(machine: str) -> str:
    m = machine.lower()
    if m in ("arm64", "aarch64", "armv8", "armv8l"):
        return "arm64"
    if m in ("x86_64", "amd64", "x64", "i686", "x86"):
        return "x86_64"
    return m


def python_arch() -> str:
    """Architecture the *current Python* runs as."""
    return _norm(platform.machine())


def hardware_arch() -> str:
    """Architecture of the machine itself (sees through Rosetta / Windows x64 emulation)."""
    osk = os_key()
    if osk == "macos":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
            if out == "1":
                return "arm64"
        except Exception:
            pass
        return python_arch()
    if osk == "windows":
        native = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")
        try:  # Windows 11 on ARM reports AMD64 to emulated processes; ask the kernel directly.
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            proc, mach = ctypes.c_ushort(0), ctypes.c_ushort(0)
            if hasattr(k32, "IsWow64Process2") and k32.IsWow64Process2(k32.GetCurrentProcess(),
                                                                       ctypes.byref(proc), ctypes.byref(mach)):
                return {0xAA64: "arm64", 0x8664: "x86_64"}.get(mach.value, _norm(native or python_arch()))
        except Exception:
            pass
        return _norm(native) if native else python_arch()
    return python_arch()


def emulated() -> bool:
    """True when this Python is an x86_64 build running on ARM hardware (Rosetta / Prism)."""
    return hardware_arch() == "arm64" and python_arch() == "x86_64"


def python_max(osk: str | None = None, arch: str | None = None) -> tuple[int, int]:
    return PY_MAX.get((osk or os_key(), arch or hardware_arch()), (3, 14))


def check_python(version: tuple[int, int] | None = None) -> tuple[bool, str]:
    """Is the running Python suitable on this machine? Returns (ok, explanation)."""
    v = version or sys.version_info[:2]
    osk, hw, pa = os_key(), hardware_arch(), python_arch()
    hi = python_max(osk, hw)
    label = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}[osk]
    if emulated():
        where = "Rosetta" if osk == "macos" else "x64 emulation"
        return False, (f"This Python is an Intel/x86_64 build running under {where} on an ARM ({hw}) "
                       f"{label} machine. Install a native arm64 Python {hi[0]}.{hi[1]}"
                       + (" from python.org (universal2 installer) or an arm64 Homebrew in /opt/homebrew."
                          if osk == "macos" else "."))
    if v < PY_MIN:
        return False, f"Python {v[0]}.{v[1]} is too old — use Python {PY_MIN[0]}.{PY_MIN[1]}–{hi[0]}.{hi[1]}."
    if v > hi:
        return False, (f"Python {v[0]}.{v[1]} is newer than the AI libraries support on {label} {hw} — "
                       f"use Python {hi[0]}.{hi[1]} or older.")
    return True, f"Python {v[0]}.{v[1]} · native {pa}"


def macos_major() -> int:
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except Exception:
        return 0


def component_support(key: str, osk: str, hw: str, py: tuple[int, int]) -> tuple[bool, str]:
    """Can component `key` be installed natively on (os, hardware arch, python version)?"""
    if key == "mlx_whisper":
        if not (osk == "macos" and hw == "arm64"):
            return False, "Apple Silicon Macs only"
        if 0 < macos_major() < 14:
            return False, "needs macOS 14 Sonoma or newer"
        return True, ""
    if key in ("torch", "transformers"):
        if osk == "windows" and hw == "arm64":
            return False, "no native PyTorch for Windows on ARM yet — use whisper.cpp"
        if osk == "macos" and hw == "x86_64" and py > (3, 12):
            return False, "PyTorch for Intel Macs stops at Python 3.12"
        return True, ""
    if key == "faster_whisper":
        if osk == "windows" and hw == "arm64":
            return False, "no native build for Windows on ARM — use whisper.cpp"
        if osk == "macos" and hw == "x86_64" and py > (3, 13):
            return False, "needs Python 3.13 or older on Intel Macs"
        return True, ""
    return True, ""


def profiles_for(osk: str, hw: str) -> list[str]:
    """Acceleration profiles that make sense on this OS + architecture."""
    if osk == "macos":
        return ["apple_mlx", "cpu"] if hw == "arm64" else ["cpu"]
    if osk == "windows":
        return ["nvidia_cuda", "vulkan", "cpu"] if hw == "x86_64" else ["vulkan", "cpu"]
    # linux
    return ["nvidia_cuda", "amd_rocm", "vulkan", "cpu"] if hw == "x86_64" else ["nvidia_cuda", "vulkan", "cpu"]
