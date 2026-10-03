"""Background worker process.

The GUI sends jobs through a multiprocessing queue; this process runs the pipeline
and streams events back. Running the heavy ML code in a separate process keeps the
UI responsive, lets "Stop" kill a job instantly, and means a crash in a GPU library
can't take the whole app down. This module must not import Qt."""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback

# No progress for this long while the process is idle (no CPU, no download) = stuck.
STALL_SECS = float(os.environ.get("YAKUSURU_STALL_SECS", "180"))


class _QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put({"type": "log", "level": record.levelname, "msg": self.format(record)})
        except Exception:
            pass


def worker_main(job_q, event_q, hf_home: str = "") -> None:
    if hf_home:
        os.environ["HF_HOME"] = hf_home
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        os.nice(5)   # background work yields to the UI and other apps (no effect on GPU priority)
    except (AttributeError, OSError):
        pass
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    root = logging.getLogger()
    root.handlers[:] = [_QueueLogHandler(event_q)]
    root.handlers[0].setFormatter(logging.Formatter("%(message)s"))
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "filelock", "faster_whisper", "hf_xet"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # huggingface_hub warns about anonymous downloads on every file; that's expected here.
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

    from .config import Settings
    from .pipeline import EngineCache, process

    cache = EngineCache()
    event_q.put({"type": "ready", "pid": os.getpid()})
    while True:
        job = job_q.get()
        if job is None:
            break
        jid = job["id"]
        s = Settings()
        s.update(job["settings"])
        from .i18n import set_language
        set_language(s.ui_language)          # progress messages in the interface language

        last = {"p": -1.0, "t": time.time(), "stage": "", "m": ""}
        from pathlib import Path as _P
        from .diagnostics import Monitor, fmt_mmss
        try:
            from huggingface_hub import constants as _hc
            cache_root = _P(_hc.HF_HUB_CACHE)
        except Exception:
            cache_root = _P(os.environ.get("HF_HOME", _P.home() / ".cache" / "huggingface")) / "hub"
        mon = Monitor(threading.get_ident(), cache_root)
        wlog = logging.getLogger("yakusuru.monitor")

        def emit(stage, prog, msg, jid=jid):
            moved = abs(prog - last["p"]) >= 0.0005 or stage != last["stage"]
            if moved:
                last["t"] = time.time()
                mon.touch()
            if stage != last["stage"] and stage not in ("done", "skipped"):
                wlog.info("· %s", msg or stage)          # every stage change shows up in the log
            last["stage"] = stage
            # throttle: only send meaningful changes
            if stage in ("done", "skipped") or abs(prog - last["p"]) >= 0.002 or msg != last.get("m"):
                last["p"], last["m"] = prog, msg
                event_q.put({"type": "progress", "id": jid, "stage": stage, "progress": prog, "message": msg})

        beat = {"next_log": 30.0, "warned": False}

        def heartbeat(snap, jid=jid):
            quiet = snap["quiet"]
            stage = last["stage"]
            waiting = snap["activity"].startswith(("waiting", "idle"))
            # Stuck = no progress for 3 min while nothing is happening (no CPU, no download).
            # GPU work can't be measured from here, so MLX compute never counts as stuck.
            stalled = quiet >= STALL_SECS and waiting and snap["cpu"] < 3 and snap["cache_rate"] < 10_000
            event_q.put({"type": "status", "id": jid, "quiet": quiet, "activity": snap["activity"],
                         "cpu": snap["cpu"], "rss": snap["rss"], "where": snap["where"],
                         "stalled": stalled})
            if quiet < 30:
                beat["next_log"], beat["warned"] = 30.0, False
                return
            if quiet >= beat["next_log"]:
                beat["next_log"] = quiet + (30 if quiet < 300 else 60)
                mem = f" · memory {snap['rss'] / 1e9:.1f} GB" if snap["rss"] else ""
                wlog.info("⏳ %s with no progress (%s) · %s · CPU %d%%%s · in %s",
                          fmt_mmss(quiet), last.get("m") or stage, snap["activity"], round(snap["cpu"]),
                          mem, snap["where"] or "?")
            if stalled and not beat["warned"]:
                beat["warned"] = True
                wlog.warning("Looks stuck: nothing has happened for %s (%s). Press Stop to cancel; "
                             "Help → System Report has details to share.", fmt_mmss(quiet), snap["activity"])

        mon.run(heartbeat)
        try:
            result = process(job["path"], s, cache, emit, lambda: False)
            event_q.put({"type": "done", "id": jid, **result})
        except InterruptedError:
            event_q.put({"type": "cancelled", "id": jid})
        except Exception as e:
            event_q.put({"type": "error", "id": jid, "error": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc()})
        finally:
            mon.stop()
    event_q.put({"type": "exit"})
