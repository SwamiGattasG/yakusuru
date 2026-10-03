"""Glossary editor (global + per-file)."""
from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QPushButton,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import glossary
from ..glossary import Term
from .widgets import label


class TermTable(QWidget):
    def __init__(self, terms: list[Term], parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Japanese", "English", "Note for the translator"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 200)
        self.table.verticalHeader().setVisible(False)
        for t in terms:
            self._append(t)
        v.addWidget(self.table)
        h = QHBoxLayout()
        for text, slot in [("Add", lambda: self._append(Term("", ""), edit=True)), ("Remove", self._remove),
                           ("Import CSV…", self._import), ("Export CSV…", self._export)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)

    def _append(self, t: Term, edit: bool = False):
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, val in enumerate((t.source, t.target, t.note)):
            self.table.setItem(r, c, QTableWidgetItem(val))
        if edit:
            self.table.setCurrentCell(r, 0)
            self.table.editItem(self.table.item(r, 0))

    def _remove(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def terms(self) -> list[Term]:
        out = []
        for r in range(self.table.rowCount()):
            vals = [(self.table.item(r, c).text().strip() if self.table.item(r, c) else "") for c in range(3)]
            if vals[0]:
                out.append(Term(*vals))
        return out

    def _import(self):
        f, _ = QFileDialog.getOpenFileName(self, "Import glossary", "", "CSV (*.csv *.tsv *.txt)")
        if not f:
            return
        with open(f, encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(2048)
            fh.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;") if sample else csv.excel
            for row in csv.reader(fh, dialect):
                if len(row) >= 2 and row[0].strip() and row[0].strip().lower() not in ("japanese", "source"):
                    self._append(Term(row[0].strip(), row[1].strip(), row[2].strip() if len(row) > 2 else ""))

    def _export(self):
        f, _ = QFileDialog.getSaveFileName(self, "Export glossary", "glossary.csv", "CSV (*.csv)")
        if not f:
            return
        with open(f, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["japanese", "english", "note"])
            for t in self.terms():
                w.writerow([t.source, t.target, t.note])


class GlossaryDialog(QDialog):
    def __init__(self, media: Path | None = None, parent=None):
        super().__init__(parent)
        self.media = media
        self.setWindowTitle("Glossary")
        self.setMinimumSize(760, 520)
        v = QVBoxLayout(self)
        v.addWidget(label("Names and terms the translator must keep consistent. Character notes "
                          "(gender, how they speak) help LLMs choose pronouns and tone. "
                          "Per-file entries override global ones.  Example: 田中 → Tanaka (note: \"female, "
                          "the narrator\") · 魔法学園 → Magic Academy", "Hint"))
        self.tabs = QTabWidget()
        g = glossary.load_global()
        self.global_tab = TermTable(g)
        self.tabs.addTab(self.global_tab, "All files")
        self.file_tab = None
        if media is not None:
            self.file_tab = TermTable(glossary.load_sidecar(media))
            self.tabs.addTab(self.file_tab, f"Only “{media.name}”")
            self.tabs.setCurrentIndex(1)
        v.addWidget(self.tabs)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def accept(self):
        glossary.save_global(self.global_tab.terms())
        if self.file_tab is not None and self.media is not None:
            terms = self.file_tab.terms()
            if terms or glossary.sidecar_path(self.media).exists():
                glossary.save_sidecar(self.media, terms)
        super().accept()
