"""Bridge between the GUI and the background worker process."""
from __future__ import annotations

import logging
import multiprocessing as mp
import queue

from PySide6.QtCore import QObject, QThread, Signal

from ..worker import worker_main

log = logging.getLogger(__name__)


class _Reader(QThread):
    event = Signal(dict)

    def __init__(self, q, parent=None):
        super().__init__(parent)
        self.q = q
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                ev = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            self.event.emit(ev)

    def stop(self):
        self._stop = True


class WorkerBridge(QObject):
    """Owns one worker process. `cancel()` kills it; the next job starts a fresh one."""
    event = Signal(dict)

    def __init__(self, hf_home: str = "", parent=None):
        super().__init__(parent)
        self.ctx = mp.get_context("spawn")
        self.hf_home = hf_home
        self.proc = None
        self.job_q = None
        self.event_q = None
        self.reader = None
        self.current_job: int | None = None

    def _ensure(self):
        if self.proc is not None and self.proc.is_alive():
            return
        self._teardown()
        self.job_q = self.ctx.Queue()
        self.event_q = self.ctx.Queue()
        self.proc = self.ctx.Process(target=worker_main, args=(self.job_q, self.event_q, self.hf_home),
                                     daemon=True, name="yakusuru-worker")
        self.proc.start()
        self.reader = _Reader(self.event_q, self)
        self.reader.event.connect(self._on_event)
        self.reader.start()

    def _on_event(self, ev: dict):
        if ev.get("type") in ("done", "error", "cancelled") and ev.get("id") == self.current_job:
            self.current_job = None
        self.event.emit(ev)

    def submit(self, job_id: int, path: str, settings: dict) -> None:
        self._ensure()
        self.current_job = job_id
        self.job_q.put({"id": job_id, "path": path, "settings": settings})

    def busy(self) -> bool:
        return self.current_job is not None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def cancel(self) -> None:
        jid = self.current_job
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(3)
            if self.proc.is_alive():
                self.proc.kill()
        self._teardown()
        self.current_job = None
        if jid is not None:
            self.event.emit({"type": "cancelled", "id": jid})

    def check_crashed(self) -> None:
        """Called periodically: report a job as failed if the worker died (e.g. native crash / OOM)."""
        if self.current_job is not None and self.proc is not None and not self.proc.is_alive():
            code = self.proc.exitcode
            jid = self.current_job
            self._teardown()
            self.current_job = None
            self.event.emit({"type": "error", "id": jid,
                             "error": f"The processing engine stopped unexpectedly (exit code {code}). "
                                      "This is usually out-of-memory — try a smaller model.",
                             "trace": ""})

    def _teardown(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(1000)
            self.reader = None
        for q in (self.job_q, self.event_q):
            try:
                if q is not None:
                    q.close()
            except Exception:
                pass
        self.proc = None

    def shutdown(self):
        if self.proc is not None and self.proc.is_alive():
            try:
                self.job_q.put(None)
                self.proc.join(2)
            except Exception:
                pass
            if self.proc.is_alive():
                self.proc.terminate()
        self._teardown()
