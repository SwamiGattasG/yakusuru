"""Hardware & OS detection, and the "profile" that decides which engines get installed.

Profiles
--------
apple_mlx      Apple Silicon Mac        → mlx-whisper (+ PyTorch/MPS for transformers models)
nvidia_cuda    NVIDIA GPU (Win/Linux)   → faster-whisper on CUDA (+ PyTorch CUDA)
amd_rocm       AMD GPU on Linux         → PyTorch ROCm + transformers
vulkan         whisper.cpp, the lightweight native engine (Windows on ARM; optional elsewhere)
cpu            Anything else            → faster-whisper int8 on CPU
"""
from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

PROFILES = {
    "apple_mlx": "Apple Silicon (MLX)",
    "nvidia_cuda": "NVIDIA GPU (CUDA)",
    "amd_rocm": "AMD GPU on Linux (ROCm)",
    "vulkan": "whisper.cpp (lightweight native engine)",
    "cpu": "CPU only",
}

# Engine & default model recommended per profile.
PROFILE_DEFAULTS = {
    "apple_mlx": {"engine": "mlx", "asr_model": "mlx-community/whisper-large-v3-turbo", "device": "auto"},
    "nvidia_cuda": {"engine": "faster_whisper", "asr_model": "large-v3", "device": "cuda"},
    "amd_rocm": {"engine": "transformers", "asr_model": "kotoba-tech/kotoba-whisper-v2.0", "device": "cuda"},
    "vulkan": {"engine": "whispercpp", "asr_model": "ggml-large-v3-turbo.bin", "device": "auto"},
    "cpu": {"engine": "faster_whisper", "asr_model": "kotoba-tech/kotoba-whisper-v2.0-faster", "device": "cpu"},
}


@dataclass
class GPU:
    vendor: str            # nvidia | amd | intel | apple | other
    name: str
    vram_gb: float | None = None
    driver: str = ""


@dataclass
class HardwareInfo:
    os: str                # macos | windows | linux
    os_version: str
    arch: str              # hardware architecture: arm64 | x86_64 (sees through Rosetta)
    cpu: str
    ram_gb: float | None
    python: str
    gpus: list[GPU] = field(default_factory=list)
    rocm: bool = False
    recommended: str = "cpu"
    notes: list[str] = field(default_factory=list)
    python_translated: bool = False   # x86_64 Python emulated on ARM hardware (Rosetta / Prism)
    python_arch: str = ""
    python_ok: bool = True
    python_note: str = ""
    profiles: list[str] = field(default_factory=list)   # profiles valid for this OS + arch


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return out.stdout or ""
    except Exception:
        return ""


def _ram_gb() -> float | None:
    try:
        if os.name == "nt":
            class MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEMSTAT()
            m.dwLength = ctypes.sizeof(MEMSTAT)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))  # type: ignore[attr-defined]
            return round(m.ullTotalPhys / 1024**3, 1)
        if sys.platform == "darwin":
            v = _run(["sysctl", "-n", "hw.memsize"]).strip()
            return round(int(v) / 1024**3, 1) if v else None
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    except Exception:
        return None


def _reg_value(path: str, name: str):
    """A value under HKEY_LOCAL_MACHINE, or None (Windows only)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def _vendor(name: str) -> str:
    low = name.lower()
    return ("nvidia" if "nvidia" in low or "geforce" in low or "quadro" in low
            else "amd" if ("amd" in low or "radeon" in low or "ati " in low)
            else "intel" if ("intel" in low or " arc" in low or "iris" in low)
            else "other")


_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
_SKIP_ADAPTERS = ("basic display", "basic render", "remote display", "virtual", "parsec", "meta")


def _windows_registry_video() -> list[GPU]:
    """Graphics adapters from the registry: fast, needs no PowerShell, and has the real VRAM size
    (WMI's AdapterRAM stops at 4 GB)."""
    gpus: list[GPU] = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as cls:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        def val(n):
                            try:
                                return winreg.QueryValueEx(k, n)[0]
                            except OSError:
                                return None
                        name = str(val("DriverDesc") or "").strip()
                        if not name or any(w in name.lower() for w in _SKIP_ADAPTERS):
                            continue
                        mem = val("HardwareInformation.qwMemorySize") or val("HardwareInformation.MemorySize")
                        if isinstance(mem, bytes):
                            mem = int.from_bytes(mem[:8], "little")
                        vram = round(int(mem) / 1024**3, 1) if mem else None
                        if any(g.name == name for g in gpus):
                            continue
                        gpus.append(GPU(_vendor(name), name, vram or None, str(val("DriverVersion") or "")))
                except OSError:
                    continue
    except Exception:
        return []
    return gpus


def _cpu_name() -> str:
    if sys.platform == "darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or platform.processor()
    if os.name == "nt":
        name = _reg_value(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
        return (str(name).strip() if name else "") or platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor()


def _nvidia() -> list[GPU]:
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        exe = cand if os.path.exists(cand) else None
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                vram = round(float(parts[1]) / 1024, 1)
            except ValueError:
                vram = None
            gpus.append(GPU("nvidia", parts[0], vram, parts[2]))
    return gpus


def _windows_video() -> list[GPU]:
    gpus = _windows_registry_video()
    return gpus or _windows_wmi_video()


def _windows_wmi_video() -> list[GPU]:
    out = _run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.AdapterRAM }"], 15)
    gpus = []
    for line in out.strip().splitlines():
        name, _, ram = line.partition("|")
        name = name.strip()
        if not name:
            continue
        if any(w in name.lower() for w in _SKIP_ADAPTERS):
            continue
        vendor = _vendor(name)
        try:
            vram = round(int(ram) / 1024**3, 1) if ram.strip() else None  # caps at 4 GB (WMI limitation)
        except ValueError:
            vram = None
        gpus.append(GPU(vendor, name, vram))
    return gpus


def _linux_video() -> list[GPU]:
    gpus = []
    out = _run(["lspci"])
    for line in out.splitlines():
        if re.search(r"VGA|3D controller|Display controller", line):
            desc = line.split(":", 2)[-1].strip()
            low = desc.lower()
            vendor = ("nvidia" if "nvidia" in low else "amd" if ("amd" in low or "ati " in low or "radeon" in low)
                      else "intel" if "intel" in low else "other")
            gpus.append(GPU(vendor, desc))
    return gpus


def detect() -> HardwareInfo:
    from . import platform_matrix as pm
    os_key = pm.os_key()
    hw_arch, py_arch = pm.hardware_arch(), pm.python_arch()
    if os_key == "macos":
        os_ver = platform.mac_ver()[0]
    elif os_key == "windows":
        os_ver = platform.version()
    else:
        os_ver = platform.release()
    info = HardwareInfo(os=os_key, os_version=os_ver, arch=hw_arch, cpu=_cpu_name(), ram_gb=_ram_gb(),
                        python=platform.python_version(), python_arch=py_arch)
    info.python_translated = pm.emulated()
    info.python_ok, info.python_note = pm.check_python()
    info.profiles = pm.profiles_for(os_key, hw_arch)
    if not info.python_ok:
        info.notes.append(info.python_note + " Then run the launcher again — it rebuilds automatically.")

    if os_key == "macos":
        if hw_arch == "arm64":
            info.gpus.append(GPU("apple", info.cpu or "Apple Silicon", info.ram_gb))
            info.recommended = "apple_mlx"
            if 0 < pm.macos_major() < 14:
                info.notes.append("MLX needs macOS 14 Sonoma or newer — update macOS for the fastest engine.")
        else:
            info.recommended = "cpu"
            info.notes.append("Intel Mac: GPU acceleration is not available; CPU mode will be used.")
        return info

    nv = _nvidia()
    others = _windows_video() if os_key == "windows" else _linux_video()
    info.gpus = nv + [g for g in others if not (g.vendor == "nvidia" and nv)]

    if os_key == "linux":
        info.rocm = bool(shutil.which("rocminfo") or os.path.isdir("/opt/rocm"))

    vendors = {g.vendor for g in info.gpus}
    if nv:
        info.recommended = "nvidia_cuda"
        vram = max((g.vram_gb or 0) for g in nv)
        if vram and vram < 6:
            info.notes.append(f"GPU has {vram} GB VRAM, so prefer large-v3-turbo or kotoba (int8) models.")
    elif "amd" in vendors and os_key == "linux" and info.rocm:
        info.recommended = "amd_rocm"
    else:
        # AMD / Intel graphics on Windows (and on Linux without ROCm): faster-whisper on the CPU is
        # the dependable choice. It needs no GPU drivers and handles a long file in reasonable time.
        info.recommended = "cpu"
        if "amd" in vendors and os_key == "linux":
            info.notes.append("AMD GPU found but ROCm is not installed. Install ROCm to use the GPU, "
                              "otherwise Yakusuru uses the fast CPU engine.")
        elif vendors & {"amd", "intel"}:
            info.notes.append("AMD and Intel graphics have no ready-made GPU build for Whisper on this "
                              "system, so Yakusuru uses faster-whisper on the CPU, which works well.")
    if info.recommended not in info.profiles:
        info.recommended = "cpu"
    if os_key == "windows" and hw_arch == "arm64":
        info.recommended = "vulkan"
        info.notes.append("Windows on ARM: PyTorch and faster-whisper have no native builds yet, so "
                          "transcription uses whisper.cpp.")
    return info


def format_report(info: HardwareInfo) -> str:
    lines = [
        f"OS:        {info.os} {info.os_version} ({info.arch})",
        f"CPU:       {info.cpu}",
        f"RAM:       {info.ram_gb} GB" if info.ram_gb else "RAM:       unknown",
        f"Python:    {info.python} · {info.python_arch or info.arch}"
        + ("  ✗ emulated (Rosetta)" if info.python_translated else "  ✓ native" if info.python_ok else "  ✗ unsupported"),
    ]
    if info.gpus:
        for g in info.gpus:
            extra = f", {g.vram_gb} GB" if g.vram_gb else ""
            drv = f", driver {g.driver}" if g.driver else ""
            lines.append(f"GPU:       {g.name} [{g.vendor}{extra}{drv}]")
    else:
        lines.append("GPU:       none detected")
    if info.os == "linux":
        lines.append(f"ROCm:      {'yes' if info.rocm else 'no'}")
    lines.append(f"Profile:   {PROFILES[info.recommended]}")
    for n in info.notes:
        lines.append(f"Note:      {n}")
    return "\n".join(lines)
