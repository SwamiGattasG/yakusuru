"""ffmpeg discovery and audio extraction (16 kHz mono PCM), shared by every engine."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import wave
from pathlib import Path

MEDIA_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".m2ts", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff",
}

SAMPLE_RATE = 16000


class FFmpegMissing(RuntimeError):
    pass


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # Homebrew paths are not on PATH when launched from Finder.
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(cand):
            return cand
    try:
        import imageio_ffmpeg  # bundled static ffmpeg, installed by the core requirements
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


def probe_duration(path: Path) -> float | None:
    ff = find_ffmpeg()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30, **_no_window())
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None


def has_audio(path: Path) -> bool:
    ff = find_ffmpeg()
    if not ff:
        return True
    r = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30, **_no_window())
    return "Audio:" in r.stderr


def extract_audio(src: Path, dst: Path, audio_track: int | None = None, progress=None,
                  cancelled=None) -> Path:
    """Decode `src` to 16 kHz mono 16-bit WAV at `dst`. Reports 0..1 progress."""
    ff = find_ffmpeg()
    if not ff:
        raise FFmpegMissing("ffmpeg not found. Click Setup on the main window to install it.")
    total = probe_duration(src) or 0
    cmd = [ff, "-hide_banner", "-nostdin", "-y", "-i", str(src)]
    if audio_track is not None:
        cmd += ["-map", f"0:a:{audio_track}"]
    else:
        cmd += ["-map", "0:a:0?"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
            "-progress", "pipe:1", "-loglevel", "error", str(dst)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **_no_window())
    assert proc.stdout
    for line in proc.stdout:
        if cancelled and cancelled():
            proc.kill()
            raise InterruptedError()
        if line.startswith("out_time_ms=") and total and progress:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / total))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size < 1000:
        err = (proc.stderr.read() if proc.stderr else "")[-800:]
        raise RuntimeError(f"ffmpeg failed to extract audio: {err.strip() or 'no audio stream?'}")
    return dst


def load_wav(path: Path):
    """Load the 16 kHz mono WAV as float32 numpy array in [-1, 1]."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
