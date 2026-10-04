"""Optional components: what each profile needs, how to check it and how to install it.

Checks run in a *fresh* Python subprocess so results are accurate right after a pip
install (and so importing torch never bloats the GUI process)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

from .hardware import HardwareInfo

TORCH_INDEX = {
    "cuda": "https://download.pytorch.org/whl/cu128",
    "rocm": "https://download.pytorch.org/whl/rocm7.1",
    "cpu": "https://download.pytorch.org/whl/cpu",
}


@dataclass
class Component:
    key: str
    title: str
    description: str
    size: str
    commands: list[list[str]] = field(default_factory=list)  # pip argument lists (after "pip install")
    manual: bool = False        # not pip-installable (whisper.cpp binary, Ollama app)
    fetch: list[str] = field(default_factory=list)   # args for `python -m yakusuru.tools.fetch` instead of pip
    recommended: bool = False
    available: bool = True      # can this component run on this machine at all?
    unavailable_reason: str = ""


# Heavy native packages: never let pip fall back to compiling these from source — a missing
# wheel should fail fast with "no matching distribution" instead of a 20-minute broken build.
BINARY_ONLY = "torch,mlx,mlx-metal,ctranslate2,onnxruntime,av,tokenizers,safetensors,numba,llvmlite"


def _pip(*pkgs: str, index: str | None = None) -> list[str]:
    args = list(pkgs) + ["--prefer-binary", "--only-binary", BINARY_ONLY]
    if index:
        args += ["--index-url", index]
    return args


def torch_spec(profile: str, osk: str, arch: str) -> tuple[list[str], str, str]:
    """(pip args, description, size) for the PyTorch build matching OS + CPU arch + GPU profile."""
    if osk == "macos":
        return (_pip("torch"), "PyTorch for Apple Silicon with Metal (MPS) GPU support — used by "
                "Transformers models such as anime-whisper." if arch == "arm64" else
                "PyTorch (CPU) for Intel Macs.", "~250 MB")
    if profile == "nvidia_cuda":
        if osk == "linux" and arch == "arm64":   # Grace / Jetson-class: CUDA wheels are on PyPI
            return _pip("torch"), "PyTorch with CUDA for ARM64 Linux.", "~2.5 GB"
        return (_pip("torch", index=TORCH_INDEX["cuda"]),
                "PyTorch with CUDA 12.8 (NVIDIA GPU) — used by Transformers models.", "~2.5 GB")
    if profile == "amd_rocm":
        return (_pip("torch", index=TORCH_INDEX["rocm"]),
                "PyTorch built for AMD ROCm. Requires ROCm drivers installed on the system.", "~3 GB")
    if osk == "linux" and arch == "arm64":
        return _pip("torch"), "PyTorch (CPU, ARM64).", "~250 MB"
    return _pip("torch", index=TORCH_INDEX["cpu"]), "PyTorch (CPU build). Only needed for Transformers models.", "~200 MB"


def components_for(profile: str, hw: HardwareInfo) -> list[Component]:
    """Components for this OS + hardware architecture + Python version + GPU profile."""
    from . import platform_matrix as pm
    osk, arch = hw.os, hw.arch
    py = sys.version_info[:2]
    blocked = "" if hw.python_ok else hw.python_note

    def support(key: str) -> tuple[bool, str]:
        if blocked:
            return False, "fix Python first — see the first page"
        return pm.component_support(key, osk, arch, py)

    comps: list[Component] = []
    comps.append(Component(
        "ffmpeg", "ffmpeg (audio decoding)",
        "Reads audio from any video/audio format. Uses your system ffmpeg if present, otherwise a "
        "bundled static build for this platform.", "~30 MB",
        [_pip("imageio-ffmpeg")], recommended=True))

    ok, why = support("mlx_whisper")
    comps.append(Component(
        "mlx_whisper", "MLX Whisper (Apple Silicon)",
        "Fastest engine on M-series Macs; runs Whisper on the Apple GPU.", "~150 MB",
        [_pip("mlx-whisper")], recommended=profile == "apple_mlx", available=ok, unavailable_reason=why))

    fw_cmds = [_pip("faster-whisper")]
    if profile == "nvidia_cuda" and osk in ("windows", "linux") and arch == "x86_64":
        fw_cmds.append(_pip("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"))
    ok, why = support("faster_whisper")
    fw_desc = ("Fast Whisper engine for NVIDIA GPUs (CUDA) and CPUs, with voice-activity detection."
               if osk != "macos" else "Whisper on the CPU (int8) — a fallback; MLX is much faster on Apple Silicon.")
    comps.append(Component(
        "faster_whisper", "faster-whisper (CTranslate2)", fw_desc,
        "~150 MB" if profile != "nvidia_cuda" else "~1 GB with CUDA libraries",
        fw_cmds, recommended=profile in ("nvidia_cuda", "cpu") and ok, available=ok, unavailable_reason=why))

    t_args, t_desc, t_size = torch_spec(profile, osk, arch)
    ok, why = support("torch")
    comps.append(Component("torch", "PyTorch", t_desc, t_size, [t_args],
                           recommended=profile in ("nvidia_cuda", "amd_rocm", "apple_mlx") and ok,
                           available=ok, unavailable_reason=why))
    ok, why = support("transformers")
    comps.append(Component(
        "transformers", "Transformers engine",
        "Runs Japanese-specialised checkpoints (anime-whisper, kotoba-whisper) on "
        + ("the Apple GPU (MPS)." if osk == "macos" and arch == "arm64" else "CUDA, ROCm or CPU."),
        "~100 MB", [_pip("transformers", "accelerate")],
        recommended=profile in ("nvidia_cuda", "amd_rocm", "apple_mlx") and ok,
        available=ok, unavailable_reason=why))
    wc_auto = osk in ("windows", "linux")
    wc_nvidia = any(g.vendor == "nvidia" for g in hw.gpus)
    comps.append(Component(
        "whispercpp", "whisper.cpp",
        ("A small native Whisper program, used mainly on Windows on ARM. Installed with Homebrew on a Mac."
         if not wc_auto else
         "A small native Whisper program, used mainly on Windows on ARM. Downloads a ready-made build "
         "from GitHub (NVIDIA builds use the GPU, others the CPU)."),
        "~20 MB", manual=not wc_auto, fetch=(["whispercpp"] + (["cuda"] if wc_nvidia else [])) if wc_auto else [],
        recommended=profile == "vulkan"))
    comps.append(Component(
        "furigana", "Furigana (Japanese readings)",
        "SudachiPy and its dictionary: adds kana readings over kanji in Japanese subtitles (optional).",
        "~80 MB", [_pip("sudachipy", "sudachidict_core")],
        available=not blocked, unavailable_reason="fix Python first — see the first page" if blocked else ""))
    comps.append(Component(
        "ollama", "Ollama (local LLM translator)",
        "Free, private translation on your own machine. Install the Ollama app, then pull a model.",
        "app ~500 MB + model 5–17 GB", manual=True, recommended=True))
    return comps


# --------------------------------------------------------------------------- checks
_CHECK_SCRIPT = r"""
import json, importlib.util, sys
r = {}
def ver(mod):
    try:
        m = __import__(mod)
        return getattr(m, "__version__", "installed")
    except Exception as e:
        return None
r["python"] = sys.version.split()[0]
for k, mod in [("faster_whisper","faster_whisper"),("mlx_whisper","mlx_whisper"),
               ("transformers","transformers"),("imageio_ffmpeg","imageio_ffmpeg"),
               ("huggingface_hub","huggingface_hub"),("ctranslate2","ctranslate2"),
               ("sudachipy","sudachipy"),("sudachidict_core","sudachidict_core")]:
    r[k] = ver(mod) if importlib.util.find_spec(mod) else None
r["torch"] = None
if importlib.util.find_spec("torch"):
    try:
        import torch
        acc = "cpu"
        if torch.cuda.is_available():
            acc = ("rocm" if getattr(torch.version, "hip", None) else "cuda") + ":" + torch.cuda.get_device_name(0)
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            acc = "mps"
        r["torch"] = torch.__version__
        r["torch_accel"] = acc
    except Exception as e:
        r["torch"] = None
        r["torch_error"] = str(e)[:300]
if r.get("ctranslate2"):
    try:
        import ctranslate2
        r["ct2_cuda"] = ctranslate2.get_cuda_device_count()
    except Exception:
        r["ct2_cuda"] = 0
print("JSON:" + json.dumps(r))
"""


def check_python_packages(python: str | None = None, timeout: float = 120) -> dict:
    python = python or sys.executable
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        out = subprocess.run([python, "-c", _CHECK_SCRIPT], capture_output=True, text=True, timeout=timeout,
                             **kw)
        for line in out.stdout.splitlines():
            if line.startswith("JSON:"):
                return json.loads(line[5:])
        return {"error": (out.stderr or "")[-500:]}
    except Exception as e:
        return {"error": str(e)}


def check_ollama(url: str = "http://localhost:11434") -> dict:
    from . import ollama_install
    app = ollama_install.mac_app()
    res = {"binary": ollama_install.find_binary(), "app": str(app) if app else "", "running": False,
           "version": "", "models": []}
    try:
        import requests
        r = requests.get(url.rstrip("/") + "/api/version", timeout=2)
        if r.ok:
            res["running"] = True
            res["version"] = r.json().get("version", "")
            t = requests.get(url.rstrip("/") + "/api/tags", timeout=3)
            res["models"] = sorted(m["name"] for m in t.json().get("models", []))
    except Exception:
        pass
    return res


def status_of(key: str, pk: dict, ollama: dict | None = None, whispercpp: str | None = None) -> tuple[bool, str]:
    """(installed, detail) for one component, from check results."""
    if key == "ffmpeg":
        from .audio import find_ffmpeg
        p = find_ffmpeg()
        return bool(p), (p or "not found")
    if key == "torch":
        if pk.get("torch"):
            return True, f"{pk['torch']} · {pk.get('torch_accel', 'cpu')}"
        return False, pk.get("torch_error", "not installed")
    if key in ("faster_whisper", "mlx_whisper", "transformers"):
        v = pk.get(key)
        detail = f"v{v}" if v else "not installed"
        if key == "faster_whisper" and v:
            detail += f" · CUDA devices: {pk.get('ct2_cuda', 0)}"
        if key == "transformers" and v and not pk.get("torch"):
            return False, "needs PyTorch"
        return bool(v), detail
    if key == "furigana":
        ok = bool(pk.get("sudachipy") and pk.get("sudachidict_core"))
        return ok, f"SudachiPy v{pk['sudachipy']}" if ok else "not installed"
    if key == "whispercpp":
        return bool(whispercpp), whispercpp or "whisper-cli not found"
    if key == "ollama":
        o = ollama or {}
        if o.get("running"):
            n = len(o.get("models", []))
            return True, f"running v{o.get('version')} · {n} model(s)"
        if o.get("binary"):
            return False, "installed but not running"
        return False, "not installed"
    return False, ""


def component_report() -> str:
    from .config import Settings
    from .engines.whispercpp_engine import find_binary
    s = Settings.load()
    pk = check_python_packages()
    ol = check_ollama(s.ollama_url)
    wc = find_binary(s.whispercpp_binary)
    lines = ["Components:"]
    for key in ("ffmpeg", "torch", "faster_whisper", "mlx_whisper", "transformers", "whispercpp", "ollama"):
        ok, detail = status_of(key, pk, ol, wc)
        lines.append(f"  {'✓' if ok else '✗'} {key:<15} {detail}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(component_report())
