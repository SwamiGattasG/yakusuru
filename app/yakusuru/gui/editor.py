"""Subtitle editor: review and fix lines, timings and translations, with media preview."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout, QWidget)

from .. import glossary
from ..config import Settings
from ..pipeline import PROJECT_SUFFIX, is_project_file, load_project, save_project
from ..languages import AUTO, guess_from_text
from ..languages import get as get_lang
from ..languages import name as lang_name
from ..pipeline import lang_suffixes
from ..subtitles import Cue, fmt_ts, is_rtl, joiner, parse_ts, read_srt, render_srt
from . import theme
from .async_util import run_async
from ..i18n import _

log = logging.getLogger(__name__)

COL_N, COL_START, COL_END, COL_JA, COL_EN = range(5)


class SubtitleEditor(QMainWindow):
    def __init__(self, project: Path, settings: Settings, media: Path | None = None):
        super().__init__()
        self.s = settings
        self.project_path = Path(project)
        self.meta: dict = {}
        self.cues: list[Cue] = []
        self.undo: list[list[Cue]] = []
        self.dirty = False
        self._filling = False
        self._load(self.project_path)
        src = self.meta.get("source")
        self.media = media or (Path(src) if src and Path(src).exists() else None)
        self.setWindowTitle(_("Subtitle Editor") + f" · {self.project_path.name}")
        self.resize(1200, 780)
        self._build()
        self._fill()

    # ---------------------------------------------------------------- data
    def _load(self, p: Path):
        if is_project_file(p):
            self.meta, self.cues = load_project(p)
            self.meta.setdefault("source_lang", "ja")      # files from the Japanese→English-only era
            self.meta.setdefault("target_lang", "en")
            return
        # Plain SRT named "<stem>.<lang>.srt": prefer its project file, else pair it with its sibling.
        src_code, tgt_code = self.s.source_lang, self.s.target_lang
        stem, code = _split_lang_suffix(p)
        for suffix in (PROJECT_SUFFIX, ".langinterp.json"):
            proj = p.with_name(stem + suffix)
            if proj.exists():
                self.project_path = proj
                return self._load(proj)
        from .. import languages as L
        if code and "-" in code and code not in L.BY_CODE and all(x in L.BY_CODE for x in code.split("-", 1)):
            src_code, tgt_code = code.split("-", 1)      # bilingual file: keep its lines as the original
            code = src_code
        elif code and code not in (src_code, tgt_code):
            # e.g. opened "show.ko.srt" while settings say ja→en: treat it as the transcript
            src_code = code
        role = "tgt" if code == tgt_code or (not code and src_code == AUTO) else "src"
        if not code:
            role = "tgt"
        self.cues = []
        for a, b, text in read_srt(p):
            c = Cue(a, b)
            lang = src_code if role == "src" else tgt_code
            setattr(c, role, text.replace("\n", joiner(lang, text)).strip())
            self.cues.append(c)
        other_code = tgt_code if role == "src" else src_code
        other = p.with_name(f"{stem}.{other_code}.srt") if other_code != AUTO else None
        if other and other.exists() and other != p:
            other_role = "tgt" if role == "src" else "src"
            for c, (a, b, text) in zip(self.cues, read_srt(other)):
                setattr(c, other_role, text.replace("\n", joiner(other_code, text)).strip())
        if src_code == AUTO:
            src_code = guess_from_text(" ".join(c.src for c in self.cues)) or "und"
        self.meta = {"source": "", "outputs": [str(p)] + ([str(other)] if other and other.exists() else []),
                     "source_lang": src_code, "target_lang": tgt_code}
        self.project_path = p.with_name(stem + PROJECT_SUFFIX)

    def _snapshot(self):
        self.undo.append(copy.deepcopy(self.cues))
        self.undo = self.undo[-100:]
        self._set_dirty(True)

    def _set_dirty(self, d: bool):
        self.dirty = d
        self.setWindowTitle(("● " if d else "") + _("Subtitle Editor") + f" · {self.project_path.name}")

    # ---------------------------------------------------------------- UI
    def _build(self):
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, sc=None, tip=""):
            a = QAction(text, self)
            if sc:
                a.setShortcut(sc)
            a.setToolTip(tip or text)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        act(_("Save"), self.save, QKeySequence.StandardKey.Save, _("Save project and rewrite SRT files (Ctrl+S)"))
        act(_("Undo"), self.do_undo, QKeySequence.StandardKey.Undo)
        tb.addSeparator()
        act(_("Retranslate"), self.retranslate_selected, "Ctrl+R", _("Translate the selected lines again"))
        act(_("Insert"), self.insert_line, "Ctrl+I", _("Insert a line after the selection"))
        act(_("Split"), self.split_line, "Ctrl+K", _("Split the selected line in two"))
        act(_("Merge"), self.merge_lines, "Ctrl+M", _("Merge selected lines"))
        act(_("Delete"), self.delete_lines, None, _("Delete selected lines"))
        act(_("Shift Time…"), self.shift_time, None, _("Move selected (or all) lines earlier/later"))
        tb.addSeparator()
        self.find = QLineEdit()
        self.find.setPlaceholderText(_("Find…"))
        self.find.setMaximumWidth(200)
        self.find.returnPressed.connect(self.find_next)
        tb.addWidget(self.find)
        act(_("Replace…"), self.replace_all, "Ctrl+H")

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(12, 8, 12, 8)
        split = QSplitter(Qt.Orientation.Vertical)

        # media preview
        self.player = None
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
            if self.media and self.media.exists():
                self.player = QMediaPlayer(self)
                self.audio_out = QAudioOutput(self)
                self.player.setAudioOutput(self.audio_out)
                self.video = QVideoWidget()
                self.video.setMinimumHeight(220)
                self.player.setVideoOutput(self.video)
                self.player.setSource(QUrl.fromLocalFile(str(self.media)))
                self.player.positionChanged.connect(self._on_position)
                tv.addWidget(self.video, 1)
        except Exception as e:  # QtMultimedia backend missing on some Linux systems
            log.info("Media preview unavailable: %s", e)
        self.sub_preview = QLabel("")
        self.sub_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub_preview.setWordWrap(True)
        self.sub_preview.setMinimumHeight(54)
        self.sub_preview.setStyleSheet("font-size: 16px; padding: 6px;")
        tv.addWidget(self.sub_preview)
        ctr = QHBoxLayout()
        self.btn_play = QPushButton(_("▶ Play line"))
        self.btn_play.clicked.connect(self.play_current)
        self.btn_pause = QPushButton(_("Pause"))
        self.btn_pause.clicked.connect(lambda: self.player and self.player.pause())
        self.btn_follow = QPushButton(_("Follow playback"))
        self.btn_follow.setCheckable(True)
        self.btn_follow.setChecked(True)
        for b in (self.btn_play, self.btn_pause, self.btn_follow):
            b.setEnabled(self.player is not None)
            ctr.addWidget(b)
        ctr.addStretch(1)
        self.info = QLabel("")
        self.info.setObjectName("Hint")
        ctr.addWidget(self.info)
        tv.addLayout(ctr)
        split.addWidget(top)

        self.table = QTableWidget(0, 5)
        self.src_lang = self.meta.get("source_lang") or "und"
        self.tgt_lang = self.meta.get("target_lang") or self.s.target_lang
        self.table.setHorizontalHeaderLabels(["#", _("Start"), _("End"), _("Original ({language})").format(language=lang_name(self.src_lang)),
                                              _("Translation ({language})").format(language=lang_name(self.tgt_lang))])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_N, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_START, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_END, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_JA, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_EN, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.EditKeyPressed |
                                   QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.itemChanged.connect(self._item_changed)
        self.table.currentCellChanged.connect(lambda r, *_a: self._show_row(r))
        split.addWidget(self.table)
        split.setSizes([300 if self.player else 90, 480])
        v.addWidget(split)
        self.setCentralWidget(central)

    def _fill(self):
        self._filling = True
        t = theme.current()
        self.table.setRowCount(len(self.cues))
        for r, c in enumerate(self.cues):
            vals = [str(r + 1), fmt_ts(c.start), fmt_ts(c.end), c.src, c.tgt]
            for col, val in enumerate(vals):
                it = self.table.item(r, col)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(r, col, it)
                it.setText(val)
                if col in (COL_JA, COL_EN):        # text columns: right-align right-to-left languages
                    lang = self.src_lang if col == COL_JA else self.tgt_lang
                    it.setTextAlignment(int((Qt.AlignmentFlag.AlignRight if is_rtl(lang) else Qt.AlignmentFlag.AlignLeft)
                                            | Qt.AlignmentFlag.AlignVCenter))
                if col == COL_N:
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    it.setForeground(QColor(t["muted"]))
            # reading-speed warning (full-width scripts are read at roughly half the characters/second)
            en_item = self.table.item(r, COL_EN)
            cps = len(c.tgt) / max(0.1, c.duration)
            if c.tgt.startswith("[?]") or not c.tgt.strip():
                en_item.setBackground(QColor(t["err"]).lighter(170) if not theme.is_dark()
                                      else QColor(t["err"]).darker(300))
                en_item.setToolTip(_("Missing or failed translation"))
            elif cps > (9 if get_lang(self.tgt_lang).wide else 20):
                en_item.setBackground(QColor(t["warn"]).lighter(170) if not theme.is_dark()
                                      else QColor(t["warn"]).darker(300))
                en_item.setToolTip(_("Fast to read: {cps} characters/second").format(cps=f"{cps:.0f}"))
            else:
                en_item.setBackground(QColor(0, 0, 0, 0))
                en_item.setToolTip("")
        self.table.resizeRowsToContents()
        self._filling = False
        flagged = sum(1 for c in self.cues if c.tgt.startswith("[?]") or (c.src and not c.tgt.strip()))
        self.info.setText(_("{n} lines").format(n=len(self.cues))
                          + (" · " + _("{n} need attention").format(n=flagged) if flagged else ""))

    def _item_changed(self, it: QTableWidgetItem):
        if self._filling:
            return
        r, col = it.row(), it.column()
        if r >= len(self.cues):
            return
        self._snapshot()
        c = self.cues[r]
        try:
            if col == COL_START:
                c.start = parse_ts(it.text())
            elif col == COL_END:
                c.end = parse_ts(it.text())
            elif col == COL_JA:
                c.src = it.text().strip()
            elif col == COL_EN:
                c.tgt = it.text().strip()
        except ValueError:
            QMessageBox.warning(self, _("Invalid time"), _("Use the format HH:MM:SS,mmm"))
        if col in (COL_START, COL_END):
            self.cues.sort(key=lambda x: x.start)
        QTimer.singleShot(0, self._fill)

    def _rows(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        return rows

    # ---------------------------------------------------------------- playback
    def _show_row(self, r: int):
        if 0 <= r < len(self.cues):
            c = self.cues[r]
            self.sub_preview.setText(f"{c.src}\n{c.tgt}".strip())

    def play_current(self):
        r = self.table.currentRow()
        if self.player is None or not (0 <= r < len(self.cues)):
            return
        c = self.cues[r]
        self._stop_at = c.end
        self.player.setPosition(int(c.start * 1000))
        self.player.play()

    def _on_position(self, ms: int):
        t = ms / 1000
        stop = getattr(self, "_stop_at", None)
        if stop is not None and t >= stop:
            self.player.pause()
            self._stop_at = None
        if self.btn_follow.isChecked():
            for i, c in enumerate(self.cues):
                if c.start <= t <= c.end:
                    if self.table.currentRow() != i:
                        self.table.blockSignals(True)
                        self.table.selectRow(i)
                        self.table.blockSignals(False)
                        self.table.scrollToItem(self.table.item(i, 0))
                    self._show_row(i)
                    break

    # ---------------------------------------------------------------- editing ops
    def do_undo(self):
        if self.undo:
            self.cues = self.undo.pop()
            self._fill()
            self._set_dirty(True)

    def insert_line(self):
        rows = self._rows()
        r = rows[-1] if rows else len(self.cues) - 1
        self._snapshot()
        if 0 <= r < len(self.cues):
            prev = self.cues[r]
            nxt = self.cues[r + 1].start if r + 1 < len(self.cues) else prev.end + 2.0
            start = prev.end + 0.05
            new = Cue(start, max(start + 0.5, min(start + 2.0, nxt - 0.05)))
        else:
            new = Cue(0.0, 2.0)
        self.cues.insert(r + 1, new)
        self._fill()
        self.table.selectRow(r + 1)

    def delete_lines(self):
        rows = self._rows()
        if not rows:
            return
        self._snapshot()
        for r in reversed(rows):
            self.cues.pop(r)
        self._fill()

    def split_line(self):
        rows = self._rows()
        if len(rows) != 1:
            return
        r = rows[0]
        c = self.cues[r]
        self._snapshot()
        mid = c.start + c.duration / 2

        def half(text: str, sep: str) -> tuple[str, str]:
            if not text:
                return "", ""
            if sep == " ":
                words = text.split()
                k = max(1, len(words) // 2)
                return " ".join(words[:k]), " ".join(words[k:])
            k = len(text) // 2
            for d in range(0, k):  # prefer punctuation near the middle
                for p in (k - d, k + d):
                    if 0 < p < len(text) and text[p - 1] in "、。！？，,.!?":
                        return text[:p], text[p:]
            return text[:k], text[k:]

        s1, s2 = half(c.src, joiner(self.src_lang, c.src))
        t1, t2 = half(c.tgt, joiner(self.tgt_lang, c.tgt))
        self.cues[r:r + 1] = [Cue(c.start, mid, s1, t1), Cue(mid, c.end, s2, t2)]
        self._fill()

    def merge_lines(self):
        rows = self._rows()
        if len(rows) == 1 and rows[0] + 1 < len(self.cues):
            rows = [rows[0], rows[0] + 1]
        if len(rows) < 2:
            return
        self._snapshot()
        group = [self.cues[r] for r in rows]
        sj = joiner(self.src_lang, group[0].src)
        tj = joiner(self.tgt_lang, group[0].tgt)
        merged = Cue(group[0].start, group[-1].end, sj.join(c.src for c in group if c.src),
                     tj.join(c.tgt for c in group if c.tgt))
        for r in reversed(rows):
            self.cues.pop(r)
        self.cues.insert(rows[0], merged)
        self._fill()
        self.table.selectRow(rows[0])

    def shift_time(self):
        rows = self._rows()
        prompt = (_("Shift the {n} selected lines by seconds (negative = earlier):").format(n=len(rows))
                  if len(rows) > 1 else _("Shift all lines by seconds (negative = earlier):"))
        sec, ok = QInputDialog.getDouble(self, _("Shift timing"), prompt, 0.0, -3600, 3600, 3)
        if not ok or sec == 0:
            return
        self._snapshot()
        targets = [self.cues[r] for r in rows] if len(rows) > 1 else self.cues
        for c in targets:
            c.start = max(0.0, c.start + sec)
            c.end = max(c.start + 0.1, c.end + sec)
        self._fill()

    def find_next(self):
        q = self.find.text().strip()
        if not q:
            return
        start = self.table.currentRow() + 1
        n = len(self.cues)
        for k in range(n):
            r = (start + k) % n
            c = self.cues[r]
            if q.lower() in c.tgt.lower() or q in c.src:
                self.table.selectRow(r)
                self.table.scrollToItem(self.table.item(r, 0))
                return
        self.statusBar().showMessage(_("Not found"), 2000)

    def replace_all(self):
        q, ok = QInputDialog.getText(self, _("Replace"), _("Find (in both columns):"), text=self.find.text())
        if not ok or not q:
            return
        rep, ok = QInputDialog.getText(self, _("Replace"), f"Replace “{q}” with:")
        if not ok:
            return
        self._snapshot()
        n = 0
        for c in self.cues:
            for attr in ("ja", "en"):
                v = getattr(c, attr)
                if q in v:
                    n += v.count(q)
                    setattr(c, attr, v.replace(q, rep))
        self._fill()
        self.statusBar().showMessage(f"Replaced {n} occurrence(s)", 3000)

    def retranslate_selected(self):
        rows = self._rows()
        if not rows:
            return
        if self.s.translator == "whisper":
            QMessageBox.information(self, _("Retranslate"), _("Choose an LLM or DeepL translator on the main "
                                                          "window to retranslate individual lines."))
            return
        from ..translators import get_translator
        snap = Settings()
        snap.update(self.s.to_dict())
        snap.context_lines = max(snap.context_lines, 4)
        snap.source_lang, snap.target_lang = self.src_lang, self.tgt_lang
        # include a few neighbouring lines as context by translating a window and keeping the targets
        lo, hi = max(0, rows[0] - 3), min(len(self.cues), rows[-1] + 4)
        window = [c.src for c in self.cues[lo:hi]]
        terms = glossary.load_for(self.media)
        self.statusBar().showMessage(f"Retranslating {len(rows)} line(s) with {snap.translator}…")

        def work():
            return get_translator(snap.translator, snap).translate(window, terms, notes=snap.show_notes)

        def done(res):
            self._snapshot()
            for r in rows:
                self.cues[r].tgt = res[r - lo]
            self._fill()
            self.statusBar().showMessage(_("Retranslated."), 3000)

        def fail(err):
            self.statusBar().clearMessage()
            QMessageBox.warning(self, _("Retranslate failed"), err.split("\n")[0])

        run_async(work, on_done=done, on_error=fail)

    # ---------------------------------------------------------------- save
    def save(self):
        outputs = [Path(o) for o in self.meta.get("outputs", [])]
        kw = dict(src_lang=self.src_lang, tgt_lang=self.tgt_lang, max_chars=self.s.max_line_chars,
                  max_chars_cjk=self.s.max_line_chars_cjk, max_lines=self.s.max_lines)
        suf = lang_suffixes(self.src_lang, self.tgt_lang)
        written = []
        for o in outputs:
            name = _strip_copy_number(o.name)
            mode = ("bi" if name.endswith(suf["bilingual"]) else "src" if name.endswith(suf["original"])
                    else "tgt")
            try:
                o.write_text(render_srt(self.cues, mode, **kw), encoding="utf-8")
                written.append(o.name)
            except OSError as e:
                QMessageBox.warning(self, _("Save failed"), f"{o}: {e}")
        try:
            src = Path(self.meta.get("source") or "")
            if is_project_file(self.project_path):
                data = dict(self.meta)
                data["cues"] = [c.to_dict() for c in self.cues]
                data["outputs"] = [str(o) for o in outputs]
                self.project_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            else:
                snap = Settings()
                snap.update(self.s.to_dict())
                snap.target_lang = self.tgt_lang
                save_project(self.project_path, src, snap, self.cues, [str(o) for o in outputs],
                             src_lang=self.src_lang)
        except OSError as e:
            QMessageBox.warning(self, _("Save failed"), str(e))
            return
        self._set_dirty(False)
        self.statusBar().showMessage(_("Saved {files}").format(files=", ".join(written) if written else self.project_path.name), 4000)

    def closeEvent(self, e):
        if self.dirty:
            r = QMessageBox.question(self, _("Unsaved changes"), _("Save changes before closing?"),
                                     QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                                     QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                e.ignore()
                return
            if r == QMessageBox.StandardButton.Save:
                self.save()
        if self.player is not None:
            self.player.stop()
        e.accept()


def _split_lang_suffix(p: Path) -> tuple[str, str | None]:
    """"show.ja.srt" → ("show", "ja"); "show.srt" → ("show", None)."""
    from .. import languages as L
    stem = p.name[:-4] if p.name.lower().endswith(".srt") else p.stem
    head, _sep, code = stem.rpartition(".")
    if head and (code in L.BY_CODE or (code and "-" in code and code.split("-")[0] in L.BY_CODE)):
        return head, code
    return stem, None


def _strip_copy_number(name: str) -> str:
    """"show (2).en.srt" → "show.en.srt" (copies made by the keep-both setting)."""
    import re
    return re.sub(r" \(\d+\)(?=\.)", "", name)
