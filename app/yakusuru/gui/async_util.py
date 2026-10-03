"""Run blocking calls off the UI thread and deliver the result back on it."""
from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn, args, kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()

    @Slot()
    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            try:
                self.signals.failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            except RuntimeError:      # receiver already gone (window closed / app quitting)
                pass
            return
        try:
            self.signals.done.emit(res)
        except RuntimeError:
            pass


_keep: set = set()


def run_async(fn: Callable[..., Any], *args, on_done: Callable[[Any], None] | None = None,
              on_error: Callable[[str], None] | None = None, **kwargs) -> None:
    task = _Task(fn, args, kwargs)
    _keep.add(task.signals)

    def _cleanup(*_a):
        _keep.discard(task.signals)

    if on_done:
        task.signals.done.connect(on_done)
    if on_error:
        task.signals.failed.connect(on_error)
    task.signals.done.connect(_cleanup)
    task.signals.failed.connect(_cleanup)
    QThreadPool.globalInstance().start(task)
