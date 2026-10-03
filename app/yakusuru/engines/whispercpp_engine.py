"""whisper.cpp engine — drives the `whisper-cli` executable.

This is the GPU path for AMD/Intel GPUs on Windows (Vulkan build) and also works
with Metal on macOS and plain CPU builds. The binary is located via Settings,
the app's tools folder, or PATH (Homebrew installs it as `whisper-cli`)."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import paths
from ..models import find_asr
from ..subtitles import Cue
from . import Engine

log = logging.getLogger(__name__)

BINARY_NAMES = ["whisper-cli", "whisper-cpp", "main"]


def find_binary(configured: str = "") -> str | None:
    if configured and os.path.isfile(configured):
        return configured
    exe = ".exe" if os.name == "nt" else ""
    tool_root = paths.tools_dir() / "whisper.cpp"
    if tool_root.exists():
        for name in BINARY_NAMES[:2]:
            for p in tool_root.rglob(name + exe):
                if p.is_file():
                    return str(p)
    for name in BINARY_NAMES[:2]:
        found = shutil.which(name)
        if found:
            return found
    for cand in ("/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"):
        if os.path.exists(cand):
            return cand
    return None


def resolve_model(model_id: str) -> Path:
    p = Path(model_id).expanduser()
    if p.is_file():
        return p
    local = paths.models_dir() / model_id
    if local.is_file():
        return local
    # Download on demand from the Hugging Face repo listed in the catalog.
    from huggingface_hub import hf_hub_download
    entry = find_asr("whispercpp", model_id)
    repo = entry.hf_repo if entry and entry.hf_repo else "ggerganov/whisper.cpp"
    return Path(hf_hub_download(repo, model_id))


class WhisperCppEngine(Engine):
    name = "whispercpp"
    requires = ()

    @classmethod
    def available(cls):
        if find_binary(_configured_binary()):
            return True, ""
        return False, "whisper-cli not found (set its path in Settings or install via the setup wizard)"

    def load(self) -> None:
        self.binary = find_binary(self.s.whispercpp_binary)
        if not self.binary:
            raise RuntimeError("whisper.cpp binary (whisper-cli) not found. See Settings → Transcription.")
        self.model_path = resolve_model(self.model_id)
        self.device_used = "whisper.cpp"

    def transcribe(self, audio, wav_path, duration, task, progress, cancelled):
        with tempfile.TemporaryDirectory() as td:
            out_base = os.path.join(td, "out")
            cmd = [self.binary, "-m", str(self.model_path), "-f", str(wav_path), "-l", self.lang_arg() or "auto",
                   "-oj", "-of", out_base, "-pp", "-bs", str(max(1, int(self.s.beam_size))),
                   "-mc", "0"]  # max-context 0 ≈ condition_on_previous_text=False
            if task == "translate":
                cmd.append("-tr")
            if self.s.initial_prompt:
                cmd += ["--prompt", self.s.initial_prompt]
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", **kw)
            assert proc.stderr
            tail = []
            for line in proc.stderr:
                if cancelled():
                    proc.kill()
                    raise InterruptedError()
                tail = (tail + [line])[-40:]
                m = re.search(r"progress\s*=\s*(\d+)%", line)
                if m:
                    progress(int(m.group(1)) / 100)
                if "using" in line.lower() and ("vulkan" in line.lower() or "metal" in line.lower()
                                               or "cuda" in line.lower()):
                    self.device_used = "whisper.cpp " + line.strip()[:60]
            proc.wait()
            jpath = out_base + ".json"
            if proc.returncode != 0 or not os.path.exists(jpath):
                raise RuntimeError("whisper.cpp failed:\n" + "".join(tail[-12:]))
            with open(jpath, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        self.detected_language = ((data.get("result") or {}).get("language")
                                  if self.lang_arg() is None else None)
        cues = []
        for item in data.get("transcription", []):
            off = item.get("offsets", {})
            a, b = off.get("from", 0) / 1000, off.get("to", 0) / 1000
            text = (item.get("text") or "").strip()
            if not text:
                continue
            c = Cue(a, b)
            if task == "translate":
                c.tgt = text
            else:
                c.src = text
            cues.append(c)
        return cues


def _configured_binary() -> str:
    try:
        from ..config import Settings
        return Settings.load().whispercpp_binary
    except Exception:
        return ""
