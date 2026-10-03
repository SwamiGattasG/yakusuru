"""Main window: queue, quick settings, log."""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QFrame, QSplitter, QStackedWidget, QStatusBar, QTableView,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, AUTHOR, HOMEPAGE, __version__, audio, paths
from ..config import Settings
from ..engines import engine_status
from ..models import ASR_MODELS, CONTENT_TYPES, ENGINES, TRANSLATOR_MODELS, TRANSLATORS
from . import theme
from .async_util import run_async
from .queue_model import QueueModel, fmt_duration, quiet_note
from .. import languages as L
from .widgets import Card, DropHint, LanguageCombo, ModelCombo, ProgressDelegate, StatusDot, label
from .worker_bridge import WorkerBridge
from ..i18n import _

log = logging.getLogger(__name__)


class QueueTable(QTableView):
    def __init__(self, on_drop, parent=None):
        super().__init__(parent)
        self.on_drop = on_drop
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def paintEvent(self, e):
        super().paintEvent(e)
        # Column separators run the full height of the list, not just through the filled rows.
        from PySide6.QtGui import QColor, QPainter
        from . import theme
        hh = self.horizontalHeader()
        p = QPainter(self.viewport())
        color = QColor(theme.current()["border"])
        h = self.viewport().height()
        for col in range(hh.count()):
            if hh.isSectionHidden(col) or col == hh.logicalIndex(hh.count() - 1):
                continue
            x = hh.sectionViewportPosition(col) + hh.sectionSize(col) - 1
            # fillRect (not drawLine) so the line covers exactly the same pixels as the header's
            # border — a 1px pen is centred between pixels and lands half a pixel off on Retina.
            p.fillRect(x, 0, 1, h, color)
        p.end()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.on_drop([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])
        e.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.s = settings
        self.running = False
        self.editors = []
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1060, 680)
        self.setAcceptDrops(True)
        self.model = QueueModel(self)
        self.bridge = WorkerBridge(settings.hf_cache_dir, self)
        self.bridge.event.connect(self._on_worker_event)
        self._build_menu()
        self._build_ui()
        self._load_quick_settings()
        self._refresh_engine_status()
        self.crash_timer = QTimer(self)
        self.crash_timer.timeout.connect(self.bridge.check_crashed)
        self.crash_timer.start(1500)
        # Repaint the running row ~8×/s: elapsed counter + activity shimmer.
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(120)
        if theme.watcher is not None:
            theme.watcher.changed.connect(self._theme_changed)
        if settings.window_geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode()))
            except Exception:
                pass
        else:
            self.resize(1320, 880)

    # ------------------------------------------------------------------ UI build
    def _build_menu(self):
        mb = self.menuBar()
        m_file = mb.addMenu(_("&File"))
        self.act_add = QAction(_("Add Files…"), self, shortcut=QKeySequence.StandardKey.Open)
        self.act_add.triggered.connect(self.add_files_dialog)
        m_file.addAction(self.act_add)
        a = QAction(_("Add Folder…"), self)
        a.setMenuRole(QAction.MenuRole.NoRole)
        a.triggered.connect(self.add_folder_dialog)
        m_file.addAction(a)
        m_file.addSeparator()
        a = QAction(_("Open Subtitle Project…"), self, shortcut="Ctrl+E")
        a.setMenuRole(QAction.MenuRole.NoRole)
        a.triggered.connect(self.open_project_dialog)
        m_file.addAction(a)
        m_file.addSeparator()
        self.act_stop = QAction(_("Stop"), self, shortcut=QKeySequence("Ctrl+."))
        self.act_stop.setMenuRole(QAction.MenuRole.NoRole)
        self.act_stop.setEnabled(False)
        self.act_stop.triggered.connect(self.stop)
        m_file.addAction(self.act_stop)
        m_file.addSeparator()
        a = QAction(_("Quit"), self, shortcut=QKeySequence.StandardKey.Quit)
        a.setMenuRole(QAction.MenuRole.QuitRole)
        a.triggered.connect(self.close)
        m_file.addAction(a)

        m_tools = mb.addMenu(_("&Tools"))
        for text, slot, sc in [
            (_("Setup Wizard…"), self.open_wizard, None),
            (_("Settings…"), self.open_settings, QKeySequence.StandardKey.Preferences),
            (_("Glossary…"), self.open_glossary, "Ctrl+G"),
            (None, None, None),
            (_("Test Translator"), self.test_translator, None),
            (_("System Report"), self.show_doctor, None),
            (_("Open Logs Folder"), lambda: self._reveal(paths.logs_dir()), None),
        ]:
            if text is None:
                m_tools.addSeparator()
                continue
            act = QAction(text, self)
            if sc:
                act.setShortcut(sc)
            # macOS moves items named "Setup…/Settings…" into the app menu by heuristics;
            # pin them: only Settings becomes the standard Preferences item (⌘,).
            act.setMenuRole(QAction.MenuRole.PreferencesRole if slot == self.open_settings
                            else QAction.MenuRole.NoRole)
            act.triggered.connect(slot)
            m_tools.addAction(act)
        m_help = mb.addMenu(_("&Help"))
        a = QAction(f"About {APP_NAME}", self)
        a.setMenuRole(QAction.MenuRole.AboutRole)
        a.triggered.connect(self.about)
        m_help.addAction(a)

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 10)
        outer.setSpacing(12)

        # Header ------------------------------------------------------------
        header = QHBoxLayout()
        mark = QLabel(_("訳"))
        mark.setObjectName("BrandMark")
        brand = QLabel(APP_NAME)
        brand.setObjectName("Brand")
        sub = QLabel(_("訳する · AI subtitles in any language"))
        sub.setObjectName("Muted")
        header.addWidget(mark)
        header.addWidget(brand)
        header.addSpacing(8)
        header.addWidget(sub)
        header.addStretch(1)
        for text, slot in [(_("Add Files"), self.add_files_dialog), (_("Add Folder"), self.add_folder_dialog),
                           (_("Glossary"), self.open_glossary), (_("Editor"), self.open_project_dialog),
                           (_("Setup"), self.open_wizard), (_("Settings"), self.open_settings)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            header.addWidget(b)
        outer.addLayout(header)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)

        # Queue card ------------------------------------------------------------
        qcard = Card(_("Queue"))
        self.stack = QStackedWidget()
        self.drop_hint = DropHint()
        self.drop_hint.clicked.connect(self.add_files_dialog)
        self.table = QueueTable(self.add_paths)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(2, ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setObjectName("Queue")
        hh = self.table.horizontalHeader()
        # Every column can be resized by dragging its edge; the last one fills the remaining width.
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(60)
        hh.setSectionsMovable(True)          # columns can also be reordered by dragging the header
        for col, width in enumerate((300, 70, 280, 70, 170)):
            hh.resizeSection(col, width)
        # Saved layouts from before the Time column existed have 4 columns: ignore those.
        if self.s.queue_columns.startswith("v5:"):
            try:
                hh.restoreState(QByteArray.fromBase64(self.s.queue_columns[3:].encode()))
            except Exception:
                pass
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._queue_menu)
        self._ctx_menu = QMenu(self.table)
        self.table.doubleClicked.connect(lambda idx: self.open_job_in_editor(idx.row()))
        self.stack.addWidget(self.drop_hint)
        self.stack.addWidget(self.table)
        qcard.add(self.stack)

        qbar = QHBoxLayout()
        for text, slot, tip in [(_("Remove"), self.remove_selected, _("Remove selected files (Del)")),
                                ("↑", lambda: self._move(-1), _("Move up")),
                                ("↓", lambda: self._move(1), _("Move down")),
                                (_("Clear finished"), self.model.clear_finished, "")]:
            b = QPushButton(text)
            b.setObjectName("Ghost")
            b.setToolTip(tip)
            b.clicked.connect(slot)
            qbar.addWidget(b)
        qbar.addStretch(1)
        self.queue_summary = QLabel("")
        self.queue_summary.setObjectName("Hint")
        qbar.addWidget(self.queue_summary)
        qcard.lay.addLayout(qbar)
        del_act = QAction(self.table)
        del_act.setShortcuts([QKeySequence.StandardKey.Delete, QKeySequence("Backspace")])
        del_act.triggered.connect(self.remove_selected)
        self.table.addAction(del_act)
        for sig in (self.model.rowsInserted, self.model.rowsRemoved, self.model.dataChanged,
                    self.model.modelReset):
            sig.connect(self._update_queue_view)
        split.addWidget(qcard)

        # Settings column -------------------------------------------------------
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(12)

        asr = Card(_("1 · Transcribe"))
        self.asr_card = asr
        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.cb_src = LanguageCombo(source=True)
        self.cb_src.setToolTip(_("Language spoken in the media. Auto-detect works, but choosing it is "
                               "faster and more reliable."))
        f.addRow(_("Spoken"), self.cb_src)
        self.cb_engine = QComboBox()
        self.cb_asr_model = ModelCombo()
        self.asr_hint = label("", "Hint")
        f.addRow(_("Engine"), self.cb_engine)
        f.addRow(_("Model"), self.cb_asr_model)
        asr.lay.addLayout(f)
        asr.add(self.asr_hint)
        sl.addWidget(asr)

        tr = Card(_("2 · Translate"))
        self.tr_card = tr
        f = QFormLayout()
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.ck_translate = QCheckBox(_("Translate the transcript"))
        self.ck_translate.setToolTip(_("Turn off to only transcribe. You can translate later: "
                                     "right-click a finished file → Translate Now (the transcript is reused)."))
        f.addRow(self.ck_translate)
        self.cb_tgt = LanguageCombo(source=False)
        f.addRow(_("Into"), self.cb_tgt)
        self.cb_translator = QComboBox()
        for k, v in TRANSLATORS.items():
            self.cb_translator.addItem(_(v), k)
        self.cb_tr_model = ModelCombo()
        self.cb_content = QComboBox()
        for k, v in CONTENT_TYPES.items():
            self.cb_content.addItem(_(v), k)
        self.cb_honor = QComboBox()
        self.cb_honor.addItem(_("Keep (Tanaka-san)"), "keep")
        self.cb_honor.addItem(_("Localize (Mr. Tanaka)"), "localize")
        f.addRow(_("Translator"), self.cb_translator)
        f.addRow(_("Model"), self.cb_tr_model)
        f.addRow(_("Content"), self.cb_content)
        f.addRow(_("Honorifics"), self.cb_honor)
        self.honor_label = f.labelForField(self.cb_honor)
        tr.lay.addLayout(f)
        self.tr_hint = label("", "Hint")
        tr.add(self.tr_hint)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText(_("Optional notes for the translator — show title, characters, "
                                      "who speaks how… (e.g. \"Yuki is a girl; Ren speaks rudely\")"))
        self.notes.setFixedHeight(58)
        tr.add(self.notes)
        sl.addWidget(tr)

        out = Card(_("3 · Output"))
        row = QHBoxLayout()
        self.ck_en = QCheckBox(_("Translation"))
        self.ck_ja = QCheckBox(_("Original"))
        self.ck_bi = QCheckBox(_("Bilingual"))
        self.ck_en.setToolTip(_("Subtitles in the target language"))
        self.ck_ja.setToolTip(_("The transcript in the spoken language"))
        self.ck_bi.setToolTip(_("Original line above its translation, in one file"))
        for w in (self.ck_en, self.ck_ja, self.ck_bi):
            row.addWidget(w)
        row.addStretch(1)
        out.lay.addLayout(row)
        frow = QHBoxLayout()
        self.ck_furi = QCheckBox(_("Furigana"))
        self.ck_furi.setToolTip(_("Also write Japanese subtitles with kana readings over the kanji"))
        self.cb_furi = QComboBox()
        self.cb_furi.addItem(_("Inline — 漢字（かんじ） in .furigana.srt"), "inline")
        self.cb_furi.addItem(_("Ruby — real furigana in .vtt"), "ruby")
        self.cb_furi.addItem(_("Both files"), "both")
        self.cb_furi.setToolTip(_("Inline works in every player. Ruby shows small kana above the kanji in "
                                "browsers and WebVTT players that support it; others show the plain text."))
        self.btn_furi_install = QPushButton(_("Install (80 MB)"))
        self.btn_furi_install.setToolTip(_("Installs SudachiPy and its Japanese dictionary, used to read the kanji"))
        self.btn_furi_install.clicked.connect(self._install_furigana)
        frow.addWidget(self.ck_furi)
        frow.addWidget(self.cb_furi, 1)
        frow.addWidget(self.btn_furi_install)
        out.lay.addLayout(frow)
        self.out_hint = label("", "Hint")
        out.add(self.out_hint)
        sl.addWidget(out)
        sl.addStretch(1)

        run_row = QHBoxLayout()
        self.btn_start = QPushButton(_("Start"))
        self.btn_start.setObjectName("Primary")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton(_("■  Stop"))
        self.btn_stop.setToolTip(_("Cancel the file being processed (⌘.). Its transcript so far is kept "
                                 "if transcription had finished."))
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setMinimumHeight(40)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        run_row.addWidget(self.btn_start, 3)
        run_row.addWidget(self.btn_stop, 1)

        side_scroll = QScrollArea()
        side_scroll.setObjectName("SidePanel")
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_scroll.setWidget(side)
        side_col = QWidget()
        sc = QVBoxLayout(side_col)
        sc.setContentsMargins(0, 0, 0, 0)
        sc.setSpacing(10)
        sc.addWidget(side_scroll, 1)
        sc.addLayout(run_row)
        side_col.setMinimumWidth(380)
        side_col.setMaximumWidth(480)
        for cb in (self.cb_engine, self.cb_asr_model, self.cb_translator, self.cb_tr_model, self.cb_content,
                   self.cb_honor):
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(12)
        split.addWidget(side_col)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)

        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.setChildrenCollapsible(True)
        vsplit.addWidget(split)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText(_("Log"))
        f = self.log_view.font()
        f.setFamily("Menlo" if sys.platform == "darwin" else "Consolas" if os.name == "nt" else "Monospace")
        f.setStyleHint(f.StyleHint.Monospace)
        self.log_view.setFont(f)
        vsplit.addWidget(self.log_view)
        vsplit.setSizes([700, 80])
        self.vsplit = vsplit
        outer.addWidget(vsplit, 1)
        self.setCentralWidget(root)

        # status bar
        sb = QStatusBar()
        self.dot = StatusDot()
        self.status_label = QLabel("")
        w = QWidget()
        hl = QHBoxLayout(w)
        hl.setContentsMargins(8, 0, 0, 0)
        hl.addWidget(self.dot)
        hl.addWidget(self.status_label)
        self.btn_fix = QPushButton(_("Open Setup Wizard"))
        self.btn_fix.setObjectName("Primary")
        self.btn_fix.clicked.connect(self.open_wizard)
        self.btn_fix.hide()
        hl.addWidget(self.btn_fix)
        hl.addStretch(1)
        sb.addWidget(w, 1)
        self.overall = QLabel("")
        sb.addPermanentWidget(self.overall)
        self.setStatusBar(sb)

        # signals
        self.cb_engine.currentIndexChanged.connect(self._engine_changed)
        self.cb_src.currentIndexChanged.connect(self._save_quick_settings)
        self.cb_tgt.currentIndexChanged.connect(self._save_quick_settings)
        self.ck_translate.toggled.connect(self._save_quick_settings)
        self.cb_translator.currentIndexChanged.connect(self._translator_changed)
        for w in (self.cb_asr_model, self.cb_tr_model):
            w.editTextChanged.connect(self._save_quick_settings)
        for w in (self.cb_content, self.cb_honor):
            w.currentIndexChanged.connect(self._save_quick_settings)
        for w in (self.ck_en, self.ck_ja, self.ck_bi, self.ck_furi):
            w.toggled.connect(self._save_quick_settings)
        self.cb_furi.currentIndexChanged.connect(self._save_quick_settings)
        self.notes.textChanged.connect(self._save_quick_settings)
        self._update_queue_view()

    # ------------------------------------------------------------------ quick settings
    def _load_quick_settings(self):
        self._loading = True
        self.cb_engine.blockSignals(True)
        self.cb_engine.clear()
        for k, v in ENGINES.items():
            self.cb_engine.addItem(_(v), k)
        i = self.cb_engine.findData(self.s.engine)
        self.cb_engine.setCurrentIndex(max(0, i))
        self.cb_engine.blockSignals(False)
        self._fill_asr_models()
        i = self.cb_translator.findData(self.s.translator)
        self.cb_translator.blockSignals(True)
        self.cb_translator.setCurrentIndex(max(0, i))
        self.cb_translator.blockSignals(False)
        self._fill_tr_models()
        for cb, code in ((self.cb_src, self.s.source_lang), (self.cb_tgt, self.s.target_lang)):
            cb.blockSignals(True)
            cb.set_code(code)
            cb.blockSignals(False)
        self.cb_content.setCurrentIndex(max(0, self.cb_content.findData(self.s.content_type)))
        self.cb_honor.setCurrentIndex(max(0, self.cb_honor.findData(self.s.honorifics)))
        self.ck_translate.setChecked(self.s.translate)
        self.ck_en.setChecked(self.s.out_translation)
        self.ck_ja.setChecked(self.s.out_original)
        self.ck_bi.setChecked(self.s.out_bilingual)
        self.ck_furi.setChecked(self.s.out_furigana)
        self.cb_furi.setCurrentIndex(max(0, self.cb_furi.findData(self.s.furigana_style)))
        self.notes.setPlainText(self.s.show_notes)
        self._loading = False
        self._update_hints()

    def _fill_asr_models(self):
        eng = self.cb_engine.currentData()
        items = [(m.id, f"{m.label} · {m.size_gb:.1f} GB" + (f"\n{m.notes}" if m.notes else ""))
                 for m in ASR_MODELS.get(eng, [])]
        cur = self.s.asr_model if eng == self.s.engine else (items[0][0] if items else "")
        self.cb_asr_model.set_items(items, cur)

    def _fill_tr_models(self):
        tkey = self.cb_translator.currentData()
        items = TRANSLATOR_MODELS.get(tkey, [])
        self.cb_tr_model.set_items(items, self.s.translator_models.get(tkey, items[0][0] if items else ""))
        self.cb_tr_model.setEnabled(tkey != "whisper")
        if tkey in ("ollama", "anthropic", "openai", "gemini", "openai_compatible"):
            from ..translators import get_translator
            snapshot = Settings()
            snapshot.update(self.s.to_dict())

            def fetch():
                return tkey, get_translator(tkey, snapshot).list_models()

            run_async(fetch, on_done=self._got_live_models)

    def _got_live_models(self, res):
        tkey, models = res
        if tkey != self.cb_translator.currentData() or not models:
            return
        cur = self.cb_tr_model.value()
        items = TRANSLATOR_MODELS.get(tkey, [])
        self.cb_tr_model.set_items(items, cur, extra=models)
        if tkey == "ollama":
            installed = set(models)
            if cur and cur not in installed and f"{cur}:latest" not in installed:
                self.tr_hint.setText(_("⚠ '{model}' is not installed in Ollama yet. Installed: {list}").format(
                    model=cur, list=", ".join(models[:6]) + ("…" if len(models) > 6 else "")))

    def _engine_changed(self):
        self._fill_asr_models()
        self._save_quick_settings()
        self._refresh_engine_status()

    def _translator_changed(self):
        self._fill_tr_models()
        self._save_quick_settings()

    def _save_quick_settings(self):
        if getattr(self, "_loading", False):
            return
        s = self.s
        s.translate = self.ck_translate.isChecked()
        s.source_lang = self.cb_src.code()
        s.target_lang = self.cb_tgt.code()
        s.engine = self.cb_engine.currentData()
        s.asr_model = self.cb_asr_model.value()
        s.translator = self.cb_translator.currentData()
        if s.translator != "whisper":
            s.translator_models[s.translator] = self.cb_tr_model.value()
        s.content_type = self.cb_content.currentData()
        s.honorifics = self.cb_honor.currentData()
        s.out_translation = self.ck_en.isChecked()
        s.out_original = self.ck_ja.isChecked()
        s.out_bilingual = self.ck_bi.isChecked()
        s.out_furigana = self.ck_furi.isChecked()
        s.furigana_style = self.cb_furi.currentData() or "inline"
        s.show_notes = self.notes.toPlainText()
        s.save()
        self._update_hints()

    def _update_hints(self):
        from ..models import find_asr
        s = self.s
        src, tgt = s.source_lang, s.target_lang
        src_name = L.name(src) if src != L.AUTO else _("Detected language")
        tgt_name = L.name(tgt)
        self.asr_card.findChild(QLabel, "CardTitle").setText(
            _("1 · Transcribe {language}").format(language=L.name(src)) if src != L.AUTO
            else _("1 · Transcribe (auto-detect language)"))
        self.tr_card.findChild(QLabel, "CardTitle").setText(_("2 · Translate into {language}").format(language=tgt_name))
        m = find_asr(s.engine, s.asr_model)
        hint = _(m.notes) if m and m.notes else ""
        if m and m.languages and src not in m.languages:
            only = ", ".join(L.name(x) for x in m.languages)
            hint = (_("⚠ This model is trained for {languages} only. Use large-v3 or turbo for {language}.")
                    .format(languages=only, language=L.name(src)) if src != L.AUTO else
                    _("⚠ This model is trained for {languages} only. Use large-v3 or turbo for auto-detect.")
                    .format(languages=only))
        self.asr_hint.setText(hint)
        self.asr_hint.setVisible(bool(hint))
        t = s.translator
        hint = ""
        if t == "whisper":
            from ..models import can_whisper_translate, find_asr as _fa, whisper_translate_model
            hint = _("Fast and offline, but literal. For natural English, an LLM translator (Claude, Grok…) is better.")
            if tgt not in L.WHISPER_TRANSLATE_TARGETS:
                hint = _("⚠ Whisper can only translate into English, not {language}. Pick another translator.").format(
                    language=tgt_name)
            elif not can_whisper_translate(s.engine, s.asr_model):
                alt = whisper_translate_model(s.engine)
                am = _fa(s.engine, alt) if alt else None
                short = s.asr_model.split("/")[-1]
                hint = (_("{model} can't translate, so the English pass runs with large-v3 instead "
                          "({size} GB, downloaded once; slower than turbo).").format(model=short, size=f"{am.size_gb:.1f}")
                        if am else _("⚠ {model} can't do Whisper's translation. Pick an LLM translator.").format(
                            model=short)) + " " + _("For natural English, an LLM translator is better.")
        elif t == "deepl" and (not L.get(tgt).deepl_tgt or (src != L.AUTO and not L.get(src).deepl_src)):
            bad = tgt_name if not L.get(tgt).deepl_tgt else L.name(src)
            hint = _("⚠ DeepL doesn't support {language}. An LLM translator handles any language.").format(language=bad)
        elif t in ("anthropic", "openai", "gemini", "xai", "deepl"):
            from ..keystore import has_key
            if not has_key(t):
                hint = _("⚠ No API key yet — add it in Settings → API keys.")
            else:
                hint = _("Sends the subtitle text (never audio) to the provider.")
        elif t == "ollama":
            from ..hardware import _ram_gb
            from ..models import OLLAMA_MODEL_GB, recommended_ollama_model
            ram = _ram_gb() or 0
            need = OLLAMA_MODEL_GB.get(s.model_for("ollama"))
            hint = _("Runs locally via Ollama. Lines are translated in context batches.")
            if ram and need and need > ram * 0.5:
                hint = _("⚠ {model} needs ~{need} GB of this computer's {ram} GB, which makes everything "
                         "sluggish. Try {lighter}, or a cloud translator (Claude, Grok…) which uses no local "
                         "memory.").format(model=s.model_for("ollama"), need=f"{need:.0f}", ram=f"{ram:.0f}",
                                           lighter=recommended_ollama_model(ram))
        if src != L.AUTO and src.split("-")[0] == tgt.split("-")[0] and "-" not in tgt:
            hint = _("Spoken and target language are both {language}: you'll get a transcript, no translation.").format(
                language=tgt_name)
        on = s.translate
        if not on:
            hint = (_("Transcript only. To translate later, right-click a finished file → Translate Now "
                    "(the transcript is reused, nothing is transcribed again)."))
            self.tr_card.findChild(QLabel, "CardTitle").setText(_("2 · Translate (off)"))
        self.tr_hint.setText(hint)
        self.tr_hint.setVisible(bool(hint))
        for w in (self.cb_tgt, self.cb_translator, self.cb_tr_model, self.cb_content, self.cb_honor, self.notes,
                  self.ck_en, self.ck_bi):
            w.setEnabled(on)
        if not on:
            return self._update_output_hint(src, tgt, src_name, tgt_name, translate=False)
        self.cb_content.setEnabled(t not in ("whisper",))
        self.cb_tr_model.setEnabled(t not in ("whisper",))      # Whisper translates with the speech model
        if t == "whisper" and self.cb_tr_model.lineEdit():
            self.cb_tr_model.lineEdit().setPlaceholderText(_("Uses the speech model"))
        honor = src.split("-")[0] in ("ja", "ko", L.AUTO) and t not in ("whisper", "deepl")
        self.cb_honor.setVisible(honor)
        if self.honor_label:
            self.honor_label.setVisible(honor)
        self.notes.setEnabled(t not in ("whisper", "deepl"))
        self._update_output_hint(src, tgt, src_name, tgt_name, translate=True)

    def _update_output_hint(self, src, tgt, src_name, tgt_name, translate: bool):
        s = self.s
        # Output labels follow the chosen languages.
        sc = src if src != L.AUTO else "xx"
        self.ck_en.setText(f"{tgt_name}")
        self.ck_ja.setText(src_name if src != L.AUTO else _("Original"))
        self.ck_bi.setText(_("Bilingual"))
        if s.output_mode == "folder" and s.output_folder:
            where = _("Saved to {folder}").format(folder=s.output_folder)
        else:
            where = _("Saved next to each source file")
        names = (f"name.{tgt}.srt / name.{sc}.srt / name.{sc}-{tgt}.srt" if translate else f"name.{sc}.srt")
        if src == L.AUTO:
            names += _(" (xx = detected language)")
        # Furigana: only when one of the outputs is Japanese.
        from .. import furigana
        ja_out = (src.split("-")[0] == "ja" or src == L.AUTO) or (translate and tgt.split("-")[0] == "ja")
        have = furigana.available()
        self.ck_furi.setEnabled(ja_out)
        self.cb_furi.setEnabled(ja_out and self.ck_furi.isChecked())
        self.btn_furi_install.setVisible(ja_out and self.ck_furi.isChecked() and not have)
        self.ck_furi.setToolTip(_("Also write Japanese subtitles with kana readings over the kanji") if ja_out else
                                _("Furigana is for Japanese subtitles — none of this job's outputs is Japanese"))
        if ja_out and self.ck_furi.isChecked():
            style = self.cb_furi.currentData()
            extra = {"inline": f"name.ja.furigana.srt", "ruby": "name.ja.vtt",
                     "both": "name.ja.furigana.srt + name.ja.vtt"}.get(style, "")
            names += f" + {extra}" + ("" if have else _(" (needs the Install button)"))
        self.out_hint.setText(f"{where} as {names}")

    def _install_furigana(self):
        from PySide6.QtWidgets import QProgressDialog
        from .. import furigana
        dlg = QProgressDialog(_("Installing the Japanese reading dictionary (SudachiPy, ~80 MB)…"), None, 0, 0, self)
        dlg.setWindowTitle(_("Furigana"))
        dlg.setMinimumDuration(0)
        dlg.setCancelButton(None)
        dlg.show()
        self.btn_furi_install.setEnabled(False)
        self._log("INFO", "Installing SudachiPy + dictionary for furigana…")

        def work():
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--progress-bar", "off",
                                "--prefer-binary", *furigana.PIP_ARGS], capture_output=True, text=True, **kw)
            return r.returncode, (r.stdout + r.stderr)[-1500:]

        def done(res):
            dlg.close()
            self.btn_furi_install.setEnabled(True)
            code, out = res
            import importlib
            importlib.invalidate_caches()
            if code == 0 and furigana.available():
                self._log("INFO", "Furigana ready.")
            else:
                self._log("ERROR", "Couldn't install the furigana dictionary:\n" + out)
                QMessageBox.warning(self, _("Furigana"), _("The install failed — see the log for details."))
            self._update_hints()

        def failed(err):
            dlg.close()
            self.btn_furi_install.setEnabled(True)
            self._log("ERROR", f"Furigana install failed: {err}")

        run_async(work, on_done=done, on_error=failed)

    def _refresh_engine_status(self):
        eng = self.s.engine
        ok, why = engine_status(eng)
        t = theme.current()
        ff = audio.find_ffmpeg()
        if not ff:
            self.dot.set_color(t["err"])
            self.status_label.setText(_("ffmpeg is missing."))
            self.btn_fix.show()
        elif ok:
            self.dot.set_color(t["ok"])
            self.btn_fix.hide()
            self.status_label.setText(_("{engine} ready").format(engine=_(ENGINES.get(eng, eng)))
                                      + f" · {platform.system()} {platform.machine()}")
        else:
            self.dot.set_color(t["warn"])
            self.status_label.setText(f"{_(ENGINES.get(eng, eng))}: {_(why)}.")
            self.btn_fix.show()
        for i in range(self.cb_engine.count()):
            k = self.cb_engine.itemData(i)
            good, _unused = engine_status(k)
            self.cb_engine.setItemText(i, _(ENGINES[k]) + ("" if good else _("  (not installed)")))

    # ------------------------------------------------------------------ queue ops
    def add_paths(self, paths_in: list[Path]):
        files: list[Path] = []
        for p in paths_in:
            if p.is_dir():
                files.extend(sorted(q for q in p.rglob("*") if q.suffix.lower() in audio.MEDIA_EXTS
                                    and not q.name.startswith(".")))
            elif p.suffix.lower() in audio.MEDIA_EXTS:
                files.append(p)
        if not files:
            self.statusBar().showMessage(_("No media files found in what you dropped."), 4000)
            return
        new = self.model.add(files)
        for job in new:
            run_async(audio.probe_duration, job.path,
                      on_done=lambda d, jid=job.id: self._set_duration(jid, d))
        if new:
            self.statusBar().showMessage(_("Added {n} file(s)").format(n=len(new)), 3000)

    def _set_duration(self, jid, d):
        j = self.model.job(jid)
        if j:
            j.duration = d
            self.model.changed(jid)
            self._update_queue_view()

    def add_files_dialog(self):
        exts = " ".join(f"*{e}" for e in sorted(audio.MEDIA_EXTS))
        files, _unused = QFileDialog.getOpenFileNames(self, _("Add media files"), "", _("Media") + f" ({exts});;" + _("All files") + " (*)")
        self.add_paths([Path(f) for f in files])

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, _("Add folder"))
        if d:
            self.add_paths([Path(d)])

    def remove_selected(self):
        rows = [i.row() for i in self.table.selectionModel().selectedRows()]
        self.model.remove_rows(rows)

    def _move(self, delta):
        rows = [i.row() for i in self.table.selectionModel().selectedRows()]
        if not rows:
            return
        new_rows = self.model.move(rows, delta)
        sel = self.table.selectionModel()
        sel.clearSelection()
        for r in new_rows:
            self.table.selectRow(r)

    def _update_queue_view(self, *_a):
        self.stack.setCurrentIndex(1 if self.model.jobs else 0)
        jobs = self.model.jobs
        q = sum(1 for j in jobs if j.state == "queued")
        d = sum(1 for j in jobs if j.state == "done")
        e = sum(1 for j in jobs if j.state == "error")
        total = sum(j.duration or 0 for j in jobs if j.state in ("queued", "running"))
        parts = [_("{n} file(s)").format(n=len(jobs))]
        if q:
            parts.append(_("{n} queued ({time})").format(n=q, time=fmt_duration(total)))
        if d:
            parts.append(_("{n} done").format(n=d))
        if e:
            parts.append(_("{n} failed").format(n=e))
        self.queue_summary.setText(" · ".join(parts) if jobs else "")

    def _queue_menu(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        job = self.model.jobs[idx.row()]
        row = idx.row()
        # One long-lived menu, shown with popup(): creating a fresh QMenu and calling exec() from a
        # context-menu event can crash Qt on macOS (QWindow::geometry on a destroyed window).
        m = self._ctx_menu
        m.clear()
        a = m.addAction(_("Open in Subtitle Editor"))
        a.setEnabled(bool(job.project) and Path(job.project).exists())
        a.triggered.connect(lambda: self.open_job_in_editor(row))
        m.addAction(_("Show in Folder")).triggered.connect(lambda: self._reveal(
            Path(job.outputs[0]) if job.outputs else job.path))
        m.addAction(_("Per-file Glossary…")).triggered.connect(lambda: self.open_glossary(job.path))
        m.addSeparator()
        if job.state == "running":
            m.addAction(_("Stop")).triggered.connect(self.stop)
        if job.state in ("error", "cancelled", "done", "skipped"):
            label = _("Run Again")
            if self.s.translate and job.state == "done" and not any(
                    o.endswith(f".{self.s.target_lang}.srt") for o in job.outputs):
                label = _("Translate Now (reuses the transcript)")
            m.addAction(label).triggered.connect(lambda: self._requeue(job.id))
        if job.error:
            m.addAction(_("Copy Error Details")).triggered.connect(
                lambda: QApplication.clipboard().setText(job.error + "\n\n" + job.trace))
        m.addAction(_("Remove")).triggered.connect(self.remove_selected)
        m.popup(self.table.viewport().mapToGlobal(pos))

    def _requeue(self, jid):
        j = self.model.job(jid)
        if j:
            j.state, j.progress, j.message, j.error, j.trace = "queued", 0.0, "", "", ""
            self.model.changed(jid)
            if self.running and not self.bridge.busy():
                self._next()

    # ------------------------------------------------------------------ run control
    def _preflight(self) -> str | None:
        s = self.s
        if s.translate and not (s.out_translation or s.out_original or s.out_bilingual):
            return _("Choose at least one output (translation, original or bilingual).")
        if not s.translate:
            return self._preflight_engine()
        if (s.translator == "whisper" and s.target_lang not in L.WHISPER_TRANSLATE_TARGETS
                and (s.out_translation or s.out_bilingual)):
            return _("Whisper's built-in translation only produces English, not {language}.\n\n"
                     "Choose Ollama, a cloud translator or DeepL for other languages.").format(
                language=L.name(s.target_lang))
        if s.translator == "deepl" and (s.out_translation or s.out_bilingual):
            if not L.get(s.target_lang).deepl_tgt or (s.source_lang != L.AUTO and not L.get(s.source_lang).deepl_src):
                return (_("DeepL doesn't support this language pair.\n\n"
                        "Choose Ollama or a cloud LLM translator — they handle any language."))
        problem = self._preflight_engine()
        if problem:
            return problem
        if s.translator in ("anthropic", "openai", "gemini", "xai", "deepl") and (s.out_translation or s.out_bilingual):
            from ..keystore import has_key
            if not has_key(s.translator):
                return _("No API key for {service}. Add one in Settings → API keys.").format(
                    service=_(TRANSLATORS[s.translator]))
        return None

    def _preflight_engine(self) -> str | None:
        if not audio.find_ffmpeg():
            return _("ffmpeg is missing. Click Setup in the toolbar to install it.")
        ok, why = engine_status(self.s.engine)
        if not ok:
            return _("The {engine} engine isn't installed ({reason}).\nClick Setup in the toolbar to install it.").format(
                engine=_(ENGINES.get(self.s.engine, self.s.engine)), reason=why)
        return None

    def start(self):
        if not self.model.next_queued():
            if not self.model.jobs:
                self.add_files_dialog()
            return
        problem = self._preflight()
        if problem:
            QMessageBox.warning(self, _("Can't start yet"), problem)
            return
        if not self._ensure_ollama():
            return
        self.running = True
        self._queue_started = time.time()
        self._set_running_ui(True)
        self._next()

    def _ensure_ollama(self) -> bool:
        """If Ollama is the translator but isn't installed, offer to install it right here."""
        s = self.s
        if not s.translate or s.translator != "ollama" or not (s.out_translation or s.out_bilingual):
            return True
        if not s.ollama_url.startswith(("http://localhost", "http://127.0.0.1")):
            return True       # remote Ollama server: nothing to install locally
        from ..deps import check_ollama
        from ..ollama_install import is_installed
        if is_installed() or check_ollama(s.ollama_url)["running"]:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(_("Install Ollama?"))
        box.setText(_("Ollama, the free local translator, isn't installed yet."))
        box.setInformativeText(_("Yakusuru can install it for you now (a few hundred MB, no admin password). "
                                 "Afterwards it starts automatically and downloads the translation model "
                                 "({model}) on first use.").format(model=s.model_for("ollama")))
        install = box.addButton(_("Install Ollama"), QMessageBox.ButtonRole.AcceptRole)
        install.setObjectName("Primary")
        other = box.addButton(_("Use Another Translator"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(install)
        box.exec()
        if box.clickedButton() is other:
            self.cb_translator.setFocus()
            self.cb_translator.showPopup()
            return False
        if box.clickedButton() is not install:
            return False
        from .ollama_dialog import InstallOllamaDialog
        dlg = InstallOllamaDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        self._log("INFO", f"Ollama installed at {dlg.where}")
        self._fill_tr_models()
        return True

    def _set_running_ui(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.act_stop.setEnabled(running)
        # Filled red while there is something to stop, so it reads as the way out.
        self.btn_stop.setProperty("armed", running)
        self.btn_stop.style().unpolish(self.btn_stop)
        self.btn_stop.style().polish(self.btn_stop)

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.bridge.cancel()          # kills the worker process: works even mid-download or mid-load
        for j in self.model.jobs:             # in case the worker had already gone
            if j.state == "running":
                j.state, j.message = "cancelled", _("Stopped")
                self.model.changed(j.id)
        self._set_running_ui(False)
        self.overall.setText("")
        self._update_queue_view()

    def _next(self):
        if not self.running:
            return
        job = self.model.next_queued()
        if job is None:
            self.running = False
            self._set_running_ui(False)
            done = sum(1 for j in self.model.jobs if j.state == "done")
            failed = sum(1 for j in self.model.jobs if j.state == "error")
            self.overall.setText("")
            took = time.time() - self._queue_started if getattr(self, "_queue_started", 0) else 0
            msg = (_("Queue finished: {n} done").format(n=done)
                   + (_(", {n} failed").format(n=failed) if failed else "")
                   + (_(" · total time {time}").format(time=fmt_duration(max(1, took))) if took else ""))
            self.statusBar().showMessage(msg, 10000)
            self._log("INFO", msg)
            QApplication.alert(self)
            return
        job.state, job.progress, job.message, job.stage = "running", 0.0, _("Starting"), "starting"
        job.started = time.time()
        self.model.changed(job.id)
        self._log("INFO", f"▶ {job.path.name}")
        self.bridge.submit(job.id, str(job.path), self.s.to_dict())

    def _on_worker_event(self, ev: dict):
        typ = ev.get("type")
        if typ == "log":
            self._log(ev.get("level", "INFO"), ev.get("msg", ""))
            return
        if typ in ("ready", "exit"):
            return
        job = self.model.job(ev.get("id", -1))
        if job is None:
            return
        if typ == "status":
            if job.state == "running":
                job.quiet, job.activity = float(ev.get("quiet", 0)), ev.get("activity", "")
                job.stalled = bool(ev.get("stalled"))
                self.model.changed(job.id)
            return
        if typ == "progress":
            if float(ev.get("progress", 0)) != job.progress or ev.get("stage", "") != job.stage:
                job.quiet, job.stalled = 0.0, False
            job.progress = float(ev.get("progress", 0))
            job.message = ev.get("message", "")
            job.stage = ev.get("stage", "")
            if ev.get("stage") == "skipped":
                job.state = "skipped"
            self.overall.setText(f"{job.path.name}: {int(job.progress * 100)}%")
        elif typ == "done":
            if ev.get("skipped"):
                job.state = "skipped"
                job.message = _("Skipped — outputs exist")
            else:
                job.state = "done"
                job.outputs = ev.get("outputs", [])
                job.project = ev.get("project", "")
                job.seconds = float(ev.get("seconds", 0))
                job.breakdown = ev.get("breakdown", "")
                job.message = f"Done · {ev.get('lines', 0)} lines"
                if self.s.source_lang == L.AUTO and ev.get("source_lang"):
                    job.message += f" · detected {L.name(ev['source_lang'])}"
            job.progress = 1.0
            self._after_job()
        elif typ == "error":
            job.seconds = time.time() - job.started if job.started else 0.0
            job.state = "error"
            job.error = ev.get("error", "Unknown error")
            job.trace = ev.get("trace", "")
            self._log("ERROR", f"{job.path.name}: {job.error}")
            if job.trace:
                log.error(job.trace)
            self._after_job()
        elif typ == "cancelled":
            if job.state == "running":
                job.seconds = time.time() - job.started if job.started else 0.0
                self._log("WARNING", f"■ Stopped {job.path.name} after {fmt_duration(max(1, job.seconds))} "
                                     f"(during: {job.message or job.stage or 'start'}).")
            job.state = "cancelled"
            job.message = _("Stopped")
            job.quiet, job.activity, job.stalled = 0.0, "", False
        self.model.changed(job.id)
        self._update_queue_view()

    def _theme_changed(self):
        self._refresh_engine_status()
        self.table.viewport().update()
        for ed in self.editors:
            if ed.isVisible():
                ed._fill()

    def _tick(self):
        for j in self.model.jobs:
            if j.state == "running":
                r = self.model.row_of(j.id)
                self.model.dataChanged.emit(self.model.index(r, 2), self.model.index(r, 3))   # progress + time
                note = quiet_note(j)
                self.overall.setText(f"{j.path.name}: {int(j.progress * 100)}%" + (f" — {note}" if note else ""))

    def _after_job(self):
        QTimer.singleShot(50, self._next)

    def _log(self, level: str, msg: str):
        t = theme.current()
        stamp = time.strftime("%H:%M:%S")
        color = {"ERROR": t["err"], "WARNING": t["warn"]}.get(level)
        body = _esc(msg).replace("  ", "&nbsp; ").replace("\n", "<br>")
        span = f'<span style="color:{color}">{body}</span>' if color else body
        self.log_view.appendHtml(f'<span style="color:{t["muted"]}">{stamp}</span>&nbsp;&nbsp;{span}')
        logging.getLogger("worker").log(getattr(logging, level, logging.INFO), msg)

    # ------------------------------------------------------------------ dialogs
    def open_settings(self):
        from .settings_dialog import SettingsDialog
        dlg = SettingsDialog(self.s, self)
        if dlg.exec():
            self.s.save()
            if theme.watcher is not None:
                theme.watcher.reapply(self.s.theme, self.s.accent)
            else:
                theme.apply(QApplication.instance(), self.s.theme, self.s.accent)
            self._load_quick_settings()
            self._refresh_engine_status()

    def open_wizard(self):
        from .setup_wizard import SetupWizard
        wiz = SetupWizard(self.s, self)
        wiz.exec()
        self.s.save()
        self._load_quick_settings()
        self._refresh_engine_status()

    def open_glossary(self, media: Path | None = None):
        from .glossary_dialog import GlossaryDialog
        GlossaryDialog(media if isinstance(media, Path) else None, self).exec()

    def open_job_in_editor(self, row: int):
        job = self.model.jobs[row]
        if job.project and Path(job.project).exists():
            self._open_editor(Path(job.project), job.path)
        elif job.state != "running":
            from ..pipeline import project_for
            p = project_for(job.path, self.s, existing=True)
            if p.exists():
                self._open_editor(p, job.path)

    def open_project_dialog(self):
        f, _unused = QFileDialog.getOpenFileName(self, _("Open subtitle project"), "",
                                           "Yakusuru project (*.yakusuru.json *.langinterp.json);;"
                                           "SRT subtitles (*.srt)")
        if f:
            self._open_editor(Path(f), None)

    def _open_editor(self, project: Path, media: Path | None):
        from .editor import SubtitleEditor
        ed = SubtitleEditor(project, self.s, media)
        ed.show()
        self.editors = [e for e in self.editors if e.isVisible()] + [ed]

    def test_translator(self):
        from ..translators import get_translator
        tkey = self.s.translator
        if tkey == "whisper":
            QMessageBox.information(self, _("Test"), _("Whisper translation runs during transcription — "
                                                  "nothing to test here."))
            return
        snap = Settings()
        snap.update(self.s.to_dict())
        self.statusBar().showMessage(f"Testing {TRANSLATORS[tkey]}…")

        def done(res):
            sample, out = res
            self.statusBar().clearMessage()
            QMessageBox.information(self, _("Translator works"),
                                    f"{TRANSLATORS[tkey]} ({snap.model_for(tkey)})\n"
                                    f"{L.name(snap.source_lang)} → {L.name(snap.target_lang)}\n\n"
                                    f"{sample}\n→ {out}")

        def fail(err):
            self.statusBar().clearMessage()
            QMessageBox.warning(self, _("Translator test failed"), err.split("\n")[0])

        run_async(lambda: get_translator(tkey, snap).test(), on_done=done, on_error=fail)

    def show_doctor(self):
        from ..deps import component_report
        from ..hardware import detect, format_report
        from .system_colors import describe
        txt = (format_report(detect()) + "\n\n" + component_report() + f"\n\nffmpeg: {audio.find_ffmpeg()}"
               + f"\nTheme: {'dark' if theme.is_dark() else 'light'}, accent {theme.current()['accent']} "
               f"({describe()})")
        box = QMessageBox(self)
        box.setWindowTitle(_("System Report"))
        box.setText(_("System report (copied to clipboard):"))
        box.setDetailedText(txt)
        QApplication.clipboard().setText(txt)
        box.exec()

    def about(self):
        QMessageBox.about(self, APP_NAME,
                          f"<b>{APP_NAME}</b> {__version__}<br>"
                          + _("AI subtitles in any language.") + "<br><br>"
                          + _("Created by {author}").format(author=AUTHOR) + "<br>"
                          + f'<a href="{HOMEPAGE}">{HOMEPAGE.replace("https://", "")}</a> · MIT License<br><br>'
                          + f"Python {platform.python_version()} · {platform.system()} {platform.machine()}<br>"
                          + _("Data folder: {path}").format(path=paths.data_dir()))

    def _reveal(self, p: Path):
        p = Path(p)
        if sys.platform == "darwin" and p.exists() and p.is_file():
            subprocess.Popen(["open", "-R", str(p)])
        elif os.name == "nt" and p.exists() and p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.is_dir() else p.parent)))

    # ------------------------------------------------------------------ window events
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            self.drop_hint.highlight = True
            self.drop_hint.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.drop_hint.highlight = False
        self.drop_hint.update()

    def dropEvent(self, e):
        self.drop_hint.highlight = False
        self.drop_hint.update()
        self.add_paths([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])

    def closeEvent(self, e):
        if self.bridge.busy():
            r = QMessageBox.question(self, _("Quit?"), _("A file is being processed. Stop it and quit?"))
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.s.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.s.queue_columns = "v5:" + bytes(self.table.horizontalHeader().saveState().toBase64()).decode()
        self.s.save()
        self.bridge.shutdown()
        e.accept()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
