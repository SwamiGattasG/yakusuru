"""Small reusable widgets."""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QLabel, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)

from . import theme
from ..i18n import _


class Card(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(14, 12, 14, 12)
        self.lay.setSpacing(8)
        if title:
            lbl = QLabel(title)
            lbl.setObjectName("CardTitle")
            self.lay.addWidget(lbl)

    def add(self, w):
        self.lay.addWidget(w)
        return w


def label(text: str, kind: str = "", wrap: bool = True) -> QLabel:
    l = QLabel(text)
    if kind:
        l.setObjectName(kind)
    l.setWordWrap(wrap)
    l.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    l.setOpenExternalLinks(True)
    return l


class ModelCombo(QComboBox):
    """Editable combo: items carry an id (userData) and a friendly label; free text allowed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(18)
        self.activated.connect(self._on_activated)

    def set_items(self, items: list[tuple[str, str]], current: str = "", extra: list[str] | None = None):
        self.blockSignals(True)
        self.clear()
        seen = set()
        for mid, lab in items:
            self.addItem(f"{mid}", mid)
            self.setItemData(self.count() - 1, lab, Qt.ItemDataRole.ToolTipRole)
            seen.add(mid)
        for mid in extra or []:
            if mid not in seen:
                self.addItem(mid, mid)
                seen.add(mid)
        self.setCurrentText(current)
        self.blockSignals(False)

    def _on_activated(self, idx: int):
        data = self.itemData(idx)
        if data:
            self.setEditText(data)

    def value(self) -> str:
        return self.currentText().strip()


class ProgressDelegate(QStyledItemDelegate):
    """Draws a slim rounded progress bar with the stage text above it."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        t = theme.current()
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        prog = float(data.get("progress", 0.0))
        state = data.get("state", "queued")
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(t["selection"]))
        elif option.features & QStyleOptionViewItem.ViewItemFeature.Alternate:
            painter.fillRect(option.rect, QColor(t.get("row_alt", t["surface2"])))
        r = option.rect.adjusted(8, 6, -8, -6)
        painter.setPen(QColor(t["warn"] if data.get("stalled") else
                              t["muted"] if state in ("queued", "skipped") else t["text"]))
        f = painter.font()
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        painter.setFont(f)
        tr = QRect(r.left(), r.top(), r.width(), r.height() - 10)
        text = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, tr.width())
        painter.drawText(tr, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        bar = QRect(r.left(), r.bottom() - 5, r.width(), 5)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t["surface2"] if not theme.is_dark() else t["border"]))
        painter.drawRoundedRect(bar, 2.5, 2.5)
        color = {"done": t["ok"], "error": t["err"], "cancelled": t["warn"], "skipped": t["muted"]}.get(
            state, t["accent"])
        if prog > 0:
            w = max(5, int(bar.width() * min(1.0, prog)))
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRect(bar.left(), bar.top(), w, bar.height()), 2.5, 2.5)
        if data.get("busy"):
            # A soft highlight sweeping across the bar: shows the job is alive even when the
            # percentage only moves every few seconds (e.g. per 30-second Whisper window).
            import time
            from PySide6.QtGui import QLinearGradient
            span = bar.width() * 0.25
            x = bar.left() - span + ((time.time() * 0.6) % 1.0) * (bar.width() + span)
            grad = QLinearGradient(x, 0, x + span, 0)
            c0 = QColor(255, 255, 255, 0)
            c1 = QColor(255, 255, 255, 90 if theme.is_dark() else 140)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(0.5, c1)
            grad.setColorAt(1.0, c0)
            painter.setBrush(grad)
            painter.setClipRect(bar)
            painter.drawRoundedRect(bar, 2.5, 2.5)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(160, 44)


class StatusDot(QWidget):
    def __init__(self, color: str = "", parent=None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(10, 10)

    def set_color(self, color: str):
        self._color = color
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(Qt.PenStyle.NoPen))
        p.setBrush(QColor(self._color or theme.current()["muted"]))
        p.drawEllipse(1, 1, 8, 8)


class DropHint(QWidget):
    """Big friendly empty-state shown over the queue."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.highlight = False

    def mouseReleaseEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(24, 24, -24, -24)
        pen = QPen(QColor(t["accent"] if self.highlight else t["border"]), 2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(QColor(t["selection"]) if self.highlight else Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 16, 16)
        p.setPen(QColor(t["accent"]))
        f = QFont(self.font())
        f.setPixelSize(56)
        f.setBold(True)
        p.setFont(f)
        p.drawText(r.adjusted(0, -70, 0, 0), Qt.AlignmentFlag.AlignCenter, "訳")
        f.setPixelSize(17)
        p.setFont(f)
        p.setPen(QColor(t["text"]))
        p.drawText(r.adjusted(0, 30, 0, 0), Qt.AlignmentFlag.AlignCenter, _("Drop videos or audio here"))
        f.setBold(False)
        f.setPixelSize(13)
        p.setFont(f)
        p.setPen(QColor(t["muted"]))
        p.drawText(r.adjusted(0, 80, 0, 0), Qt.AlignmentFlag.AlignCenter,
                   _("or click to browse · folders are scanned for media files"))


class LanguageCombo(QComboBox):
    """Language picker: common languages first, then the rest A–Z. Type to jump (e.g. "sp" → Spanish)."""

    def __init__(self, source: bool, parent=None):
        super().__init__(parent)
        from .. import languages as L
        self.setMaxVisibleItems(18)
        if source:
            self.addItem(_("Auto-detect"), L.AUTO)
            self.insertSeparator(self.count())
        pool = L.source_languages() if source else L.target_languages()
        common = [L.get(c) for c in L.COMMON if any(x.code == c for x in pool)]
        rest = sorted((x for x in pool if x.code not in L.COMMON), key=lambda x: x.name)
        for lang in common:
            self.addItem(lang.label, lang.code)
        self.insertSeparator(self.count())
        for lang in rest:
            self.addItem(lang.label, lang.code)

    def set_code(self, code: str) -> None:
        i = self.findData(code)
        if i < 0:   # unknown code typed into settings by hand: keep it selectable
            self.addItem(code, code)
            i = self.count() - 1
        self.setCurrentIndex(i)

    def code(self) -> str:
        return self.currentData()
