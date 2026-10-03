"""Job queue data model."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from . import theme

_ids = itertools.count(1)

STATE_LABEL = {
    "queued": "Queued", "running": "Working", "done": "Done", "error": "Failed",
    "cancelled": "Stopped", "skipped": "Skipped",
}


@dataclass
class Job:
    path: Path
    id: int = field(default_factory=lambda: next(_ids))
    duration: float | None = None
    state: str = "queued"
    stage: str = ""
    message: str = ""
    progress: float = 0.0
    outputs: list[str] = field(default_factory=list)
    project: str = ""
    error: str = ""
    trace: str = ""
    started: float = 0.0      # wall-clock start of processing (for elapsed / ETA)
    quiet: float = 0.0        # seconds since the worker last reported progress
    activity: str = ""        # what the worker is doing right now (from its monitor)
    stalled: bool = False     # monitor thinks it's stuck
    seconds: float = 0.0      # total processing time once finished
    breakdown: str = ""       # per-stage times ("download 0:51 · translate 2:04")


def running_text(j: "Job") -> str:
    import time
    el = time.time() - j.started if j.started else 0.0
    parts = [j.message or "Working"]
    if j.stage != "downloading":          # download messages carry their own MB / ETA
        parts.append(f"{int(j.progress * 100)}%")
    parts.append(f"{fmt_duration(el) if el >= 1 else '0:00'} elapsed")
    # Overall ETA once there is enough signal (and not during the one-off model download).
    if j.stage in ("transcribing", "translating", "writing") and j.progress > 0.15 and el > 15 \
            and j.quiet < 30:
        parts.append(f"~{fmt_duration(el * (1 - j.progress) / j.progress)} left")
    text = " · ".join(parts)
    note = quiet_note(j)
    # Put it first: the cell is often narrow, and this is the part that matters while it waits.
    return f"{note} · {text}" if note else text


def quiet_note(j: "Job") -> str:
    """When nothing has moved for a while, say what is actually happening (from the monitor)."""
    if j.state != "running" or j.quiet < 15 or not j.activity:
        return ""
    head = "⚠ Looks stuck" if j.stalled else "No progress"
    return f"{head} for {fmt_duration(j.quiet)}: {j.activity}"


def time_text(j: "Job") -> str:
    """Time column: live while running, the total once finished."""
    import time
    if j.state == "running" and j.started:
        return fmt_duration(max(1, time.time() - j.started))
    if j.state in ("done", "error", "cancelled") and j.seconds:
        return fmt_duration(max(1, j.seconds))
    return ""


def fmt_duration(sec: float | None) -> str:
    if not sec:
        return "—"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class QueueModel(QAbstractTableModel):
    COLS = ["File", "Length", "Progress", "Time", "Output"]
    OUT_COL = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: list[Job] = []

    # Qt API --------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.jobs)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLS[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            h = Qt.AlignmentFlag.AlignLeft if section == 0 else Qt.AlignmentFlag.AlignHCenter
            return int(h | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        j = self.jobs[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return j.path.name
            if col == 1:
                return fmt_duration(j.duration)
            if col == 2:
                if j.state == "running":
                    return running_text(j)
                if j.state == "error":
                    return "Failed — " + j.error.split("\n")[0][:90]
                return j.message if j.state in ("done", "skipped") and j.message else STATE_LABEL[j.state]
            if col == 3:
                return time_text(j)
            if col == 4:
                if j.outputs:
                    return "  ".join(Path(o).name.replace(j.path.stem, "…") for o in j.outputs)
                return ""
        if role == Qt.ItemDataRole.UserRole and col == 2:
            return {"progress": j.progress if j.state != "queued" else 0.0, "state": j.state,
                    "busy": j.state == "running", "stalled": j.state == "running" and j.stalled}
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 0:
                return str(j.path)
            if col == 2 and j.error:
                return j.error
            if col == 2 and j.state == "running":
                return running_text(j)
            if col == 3 and j.breakdown:
                return j.breakdown
            if col == 4 and j.outputs:
                return "\n".join(j.outputs)
        if role == Qt.ItemDataRole.ForegroundRole and (col == 4 or (col == 3 and j.state == "running")):
            return QColor(theme.current()["muted"])
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    # helpers -------------------------------------------------------------
    def add(self, paths: list[Path]) -> list[Job]:
        existing = {j.path.resolve() for j in self.jobs if j.state in ("queued", "running")}
        new = []
        for p in paths:
            rp = p.resolve()
            if rp not in existing:
                existing.add(rp)
                new.append(Job(p))
        if not new:
            return []
        self.beginInsertRows(QModelIndex(), len(self.jobs), len(self.jobs) + len(new) - 1)
        self.jobs.extend(new)
        self.endInsertRows()
        return new

    def remove_rows(self, rows: list[int]) -> None:
        for r in sorted(set(rows), reverse=True):
            if self.jobs[r].state == "running":
                continue
            self.beginRemoveRows(QModelIndex(), r, r)
            self.jobs.pop(r)
            self.endRemoveRows()

    def clear_finished(self) -> None:
        rows = [i for i, j in enumerate(self.jobs) if j.state in ("done", "skipped")]
        self.remove_rows(rows)

    def row_of(self, job_id: int) -> int:
        for i, j in enumerate(self.jobs):
            if j.id == job_id:
                return i
        return -1

    def job(self, job_id: int) -> Job | None:
        r = self.row_of(job_id)
        return self.jobs[r] if r >= 0 else None

    def changed(self, job_id: int) -> None:
        r = self.row_of(job_id)
        if r >= 0:
            self.dataChanged.emit(self.index(r, 0), self.index(r, self.columnCount() - 1))

    def next_queued(self) -> Job | None:
        return next((j for j in self.jobs if j.state == "queued"), None)

    def move(self, rows: list[int], delta: int) -> list[int]:
        rows = sorted(set(rows), reverse=delta > 0)
        new_rows = []
        for r in rows:
            t = r + delta
            if 0 <= t < len(self.jobs):
                self.beginResetModel()
                self.jobs[r], self.jobs[t] = self.jobs[t], self.jobs[r]
                self.endResetModel()
                new_rows.append(t)
            else:
                new_rows.append(r)
        return new_rows
