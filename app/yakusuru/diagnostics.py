"""Live diagnostics for the worker: what is the process doing right now, and is it stuck?

The worker's heavy calls (loading a model, a Whisper window, an API request) can run for minutes
without reporting progress. The monitor samples, every few seconds:
  * where the job's thread is in the code (its innermost frames, library + function),
  * CPU use and memory of the worker process,
  * bytes landing in the model cache (catches downloads a library starts on its own),
and turns that into a plain-language activity line plus a stuck/not-stuck verdict.
No Qt here: this runs inside the worker process."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

# Code locations → what that means to a person.
_NET = ("socket", "ssl", "http/client", "http\\client", "urllib3", "requests", "httpx", "httpcore",
        "hf_xet", "file_download", "_http", "anthropic", "openai", "google")
_LOCK = ("filelock",)
_DISK_READ = ("safetensors", "numpy/lib/npyio", "numpy\\lib\\npyio", "mlx/utils", "load_weights", "mx.load")


@dataclass
class Sample:
    t: float
    cpu_s: float          # process CPU seconds (all threads)
    rss: int              # bytes, 0 if unknown
    cache: int            # bytes in the HF cache


def _cpu_seconds() -> float:
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime
    except Exception:
        t = os.times()
        return t.user + t.system


def _rss() -> int:
    """Current resident memory of this process (bytes)."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                                 capture_output=True, text=True, timeout=2).stdout
            return int(out.strip() or 0) * 1024
    except Exception:
        pass
    return 0


def _cache_bytes(root: Path) -> int:
    total = 0
    try:
        for repo in root.iterdir():
            blobs = repo / "blobs"
            if blobs.is_dir():
                for p in blobs.iterdir():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass
    return total


def _short(path: str) -> str:
    p = path.replace("\\", "/")
    for marker in ("/site-packages/", "/dist-packages/", "/lib/python"):
        if marker in p:
            p = p.split(marker, 1)[1]
            break
    else:
        if "/yakusuru/" in p:
            p = "yakusuru/" + p.split("/yakusuru/", 1)[1]
        else:
            p = Path(p).name
    head = p.split("/", 1)[0]
    if "/" in p and (head.startswith("python3") or head[:1].isdigit()):   # stdlib: 3.13/socket.py
        p = p.split("/", 1)[1]
    return p


def where(thread_id: int, depth: int = 3) -> tuple[str, list[str]]:
    """(one-line location, raw frame list) for the given thread."""
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "", []
    stack = traceback.extract_stack(frame)
    frames = [f"{_short(fs.filename)}:{fs.name}" for fs in stack]
    # Skip our own thin wrappers at the top of the stack, keep the innermost few distinct files.
    picked, seen = [], set()
    for fs in reversed(frames):
        mod = fs.split(":")[0]
        if mod in seen:
            continue
        seen.add(mod)
        picked.append(fs)
        if len(picked) >= depth:
            break
    return " ← ".join(picked), frames


def net_target(thread_id: int) -> str:
    """'host:port' the thread is connecting to / talking with, if it's in a socket call."""
    f = sys._current_frames().get(thread_id)
    while f is not None:
        loc = f.f_locals
        try:
            if f.f_code.co_name == "create_connection" and isinstance(loc.get("address"), tuple):
                h, p = loc["address"][:2]
                return f"{h}:{p}"
            if f.f_code.co_name in ("connect_tcp", "_connect") and isinstance(loc.get("host"), (str, bytes)):
                h = loc["host"].decode() if isinstance(loc["host"], bytes) else loc["host"]
                return f"{h}:{loc.get('port', '')}".rstrip(":")
        except Exception:
            pass
        f = f.f_back
    return ""


def classify(frames: list[str], cpu_pct: float, cache_rate: float) -> str:
    """Plain-language reading of what the process is doing."""
    inner = " ".join(frames[-8:]).lower()
    if cache_rate > 50_000:
        return f"downloading model files ({cache_rate / 1e6:,.1f} MB/s)"
    if any(k in inner for k in _LOCK):
        return "waiting for a file lock on the model cache (another app or Yakusuru window may be using it)"
    if any(k in inner for k in _NET):
        return "waiting on the network" + (" (no data arriving)" if cpu_pct < 5 else "")
    if any(k in inner for k in _DISK_READ):
        return "reading model weights from disk"
    if "mlx" in inner or "metal" in inner:
        return "running on the Apple GPU" if cpu_pct < 60 else "working (MLX)"
    if "ctranslate2" in inner or "faster_whisper" in inner or "torch" in inner:
        return "computing"
    if "subprocess" in inner or "ffmpeg" in inner:
        return "waiting for an external tool (ffmpeg / whisper.cpp)"
    if cpu_pct >= 20:
        return "computing"
    return "idle (no CPU, disk or network activity)"


class Monitor:
    """Watches one job. `heartbeat(callback)` is called every `interval` seconds while it runs."""

    def __init__(self, thread_id: int, cache_root: Path, interval: float = 5.0):
        self.thread_id = thread_id
        self.cache_root = cache_root
        self.interval = interval
        self._stop = threading.Event()
        self._prev = self._sample()
        self.last_progress = time.time()

    def _sample(self) -> Sample:
        return Sample(time.time(), _cpu_seconds(), _rss(), _cache_bytes(self.cache_root))

    def touch(self) -> None:
        self.last_progress = time.time()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        cur = self._sample()
        dt = max(0.001, cur.t - self._prev.t)
        cpu_pct = 100.0 * (cur.cpu_s - self._prev.cpu_s) / dt
        cache_rate = max(0.0, (cur.cache - self._prev.cache) / dt)
        self._prev = cur
        loc, frames = where(self.thread_id)
        activity = classify(frames, cpu_pct, cache_rate)
        if activity.startswith("waiting on the network"):
            target = net_target(self.thread_id)
            if target:
                connecting = any("create_connection" in f or "connect_tcp" in f for f in frames[-6:])
                activity = activity.replace("waiting on the network",
                                            f"{'connecting to' if connecting else 'waiting on'} {target}", 1)
        return {
            "quiet": time.time() - self.last_progress,
            "cpu": cpu_pct,
            "rss": cur.rss,
            "cache_rate": cache_rate,
            "where": loc,
            "activity": activity,
        }

    def run(self, callback) -> threading.Thread:
        def loop():
            while not self._stop.wait(self.interval):
                try:
                    callback(self.snapshot())
                except Exception:
                    pass
        t = threading.Thread(target=loop, daemon=True, name="yakusuru-monitor")
        t.start()
        return t


def fmt_mmss(sec: float) -> str:
    sec = int(max(0, sec))
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}"
