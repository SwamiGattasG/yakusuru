"""Settings dialog (everything not on the main window's quick panel)."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .. import keystore
from ..config import Settings
from ..models import API_KEY_ENV, TRANSLATORS
from . import theme
from .async_util import run_async
from .widgets import label


def _form() -> tuple[QWidget, QFormLayout]:
    w = QWidget()
    f = QFormLayout(w)
    f.setContentsMargins(18, 18, 18, 18)
    f.setVerticalSpacing(10)
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return w, f


def _path_row(edit: QLineEdit, pick_dir: bool, title: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, 1)
    b = QPushButton("Browse…")

    def pick():
        if pick_dir:
            d = QFileDialog.getExistingDirectory(w, title, edit.text())
        else:
            d, _ = QFileDialog.getOpenFileName(w, title, edit.text())
        if d:
            edit.setText(d)

    b.clicked.connect(pick)
    h.addWidget(b)
    return w


class ApiKeyRow(QWidget):
    def __init__(self, provider: str, settings: Settings, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.s = settings
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(keystore.get_key(provider))
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(f"or set {API_KEY_ENV.get(provider, '')} in your environment")
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        test = QPushButton("Test")
        test.clicked.connect(self.test)
        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setMinimumWidth(140)
        h.addWidget(self.edit, 1)
        h.addWidget(show)
        h.addWidget(test)
        h.addWidget(self.status)

    def save(self):
        if self.edit.text().strip() != keystore.get_key(self.provider):
            where = keystore.set_key(self.provider, self.edit.text())
            return where
        return None

    def test(self):
        self.save()
        self.status.setText("Testing…")
        from ..translators import get_translator
        snap = Settings()
        snap.update(self.s.to_dict())

        def ok(res):
            self.status.setText("✓ " + str(res)[:40])

        def bad(err):
            self.status.setText("✗ " + err.split("\n")[0][:80])
            self.status.setToolTip(err.split("\n")[0])

        run_async(lambda: get_translator(self.provider, snap).test(), on_done=ok, on_error=bad)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("Settings")
        self.setMinimumSize(720, 560)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # Transcription ---------------------------------------------------------
        w, f = _form()
        self.device = QComboBox()
        for k, v in [("auto", "Automatic"), ("cuda", "GPU — CUDA / ROCm"), ("mps", "GPU — Apple MPS"),
                     ("cpu", "CPU")]:
            self.device.addItem(v, k)
        self.device.setCurrentIndex(max(0, self.device.findData(settings.device)))
        self.compute = QComboBox()
        for k in ("auto", "float16", "int8_float16", "int8", "float32"):
            self.compute.addItem(k, k)
        self.compute.setCurrentIndex(max(0, self.compute.findData(settings.compute_type)))
        self.beam = QSpinBox()
        self.beam.setRange(1, 10)
        self.beam.setValue(settings.beam_size)
        self.vad = QCheckBox("Skip silence / music with voice-activity detection (faster-whisper)")
        self.vad.setChecked(settings.vad_filter)
        self.hallu = QCheckBox("Remove typical Whisper hallucinations (\"ご視聴ありがとうございました\" on silence, loops)")
        self.hallu.setChecked(settings.filter_hallucinations)
        self.prompt = QLineEdit(settings.initial_prompt)
        self.prompt.setPlaceholderText("Optional: names/terms to bias recognition, in the spoken language")
        self.wcpp = QLineEdit(settings.whispercpp_binary)
        self.wcpp.setPlaceholderText("Auto-detected if empty")
        self.hf = QLineEdit(settings.hf_cache_dir)
        self.hf.setPlaceholderText("Default Hugging Face cache (~/.cache/huggingface)")
        f.addRow("Device", self.device)
        f.addRow("Precision", self.compute)
        f.addRow("Beam size", self.beam)
        f.addRow("", self.vad)
        f.addRow("", self.hallu)
        f.addRow("Initial prompt", self.prompt)
        f.addRow("", label("Ignored for anime-whisper, whose author advises against prompts.", "Hint"))
        f.addRow("whisper-cli path", _path_row(self.wcpp, False, "Locate whisper-cli"))
        f.addRow("Model cache folder", _path_row(self.hf, True, "Model cache folder"))
        tabs.addTab(w, "Transcription")

        # Translation -----------------------------------------------------------
        w, f = _form()
        self.batch = QSpinBox()
        self.batch.setRange(1, 100)
        self.batch.setValue(settings.batch_size)
        self.ctx = QSpinBox()
        self.ctx.setRange(0, 30)
        self.ctx.setValue(settings.context_lines)
        self.ollama_url = QLineEdit(settings.ollama_url)
        self.compat_url = QLineEdit(settings.openai_compatible_url)
        f.addRow("Lines per request", self.batch)
        f.addRow("", label("Larger batches give the model more context but small local models may "
                           "skip lines (they are retried automatically).", "Hint"))
        f.addRow("Context lines", self.ctx)
        f.addRow("Ollama URL", self.ollama_url)
        f.addRow("OpenAI-compatible URL", self.compat_url)
        tabs.addTab(w, "Translation")

        # API keys --------------------------------------------------------------
        w, f = _form()
        f.addRow(label("Keys are stored in your system keychain (macOS Keychain, Windows Credential "
                       "Manager, or Secret Service on Linux). Only subtitle text is sent — never audio.",
                       "Hint"))
        self.key_rows = []
        for prov in ("anthropic", "openai", "gemini", "xai", "deepl", "openai_compatible"):
            row = ApiKeyRow(prov, settings)
            self.key_rows.append(row)
            f.addRow(TRANSLATORS[prov].split(" (")[0], row)
        links = label('Get keys: <a href="https://console.anthropic.com/">Anthropic</a> · '
                      '<a href="https://platform.openai.com/api-keys">OpenAI</a> · '
                      '<a href="https://aistudio.google.com/apikey">Google AI Studio</a> · '
                      '<a href="https://console.x.ai/">xAI</a> · '
                      '<a href="https://www.deepl.com/your-account/keys">DeepL</a>', "Hint")
        f.addRow("", links)
        tabs.addTab(w, "API keys")

        # Output ----------------------------------------------------------------
        w, f = _form()
        self.next_to = QRadioButton("Next to each source file")
        self.to_folder = QRadioButton("In this folder:")
        (self.to_folder if settings.output_mode == "folder" else self.next_to).setChecked(True)
        self.out_dir = QLineEdit(settings.output_folder)
        f.addRow("Save subtitles", self.next_to)
        f.addRow("", self.to_folder)
        f.addRow("", _path_row(self.out_dir, True, "Output folder"))
        self.overwrite = QComboBox()
        for k, v in [("rename", "Keep both (add a number)"), ("overwrite", "Overwrite"),
                     ("skip", "Skip files that already have subtitles")]:
            self.overwrite.addItem(v, k)
        self.overwrite.setCurrentIndex(max(0, self.overwrite.findData(settings.overwrite)))
        f.addRow("If subtitles exist", self.overwrite)
        self.max_en = QSpinBox()
        self.max_en.setRange(20, 80)
        self.max_en.setValue(settings.max_line_chars)
        self.max_ja = QSpinBox()
        self.max_ja.setRange(10, 40)
        self.max_ja.setValue(settings.max_line_chars_cjk)
        self.max_lines = QSpinBox()
        self.max_lines.setRange(1, 3)
        self.max_lines.setValue(settings.max_lines)
        self.max_cue = QDoubleSpinBox()
        self.max_cue.setRange(2, 15)
        self.max_cue.setSingleStep(0.5)
        self.max_cue.setValue(settings.max_cue_seconds)
        self.min_cue = QDoubleSpinBox()
        self.min_cue.setRange(0.3, 3)
        self.min_cue.setSingleStep(0.1)
        self.min_cue.setValue(settings.min_cue_seconds)
        f.addRow("Characters / line", self.max_en)
        f.addRow("Characters / line (CJK)", self.max_ja)
        f.addRow("", label("The CJK limit applies to Japanese, Chinese and Korean, whose characters "
                           "are twice as wide.", "Hint"))
        f.addRow("Max lines / subtitle", self.max_lines)
        f.addRow("Max subtitle duration (s)", self.max_cue)
        f.addRow("Min subtitle duration (s)", self.min_cue)
        tabs.addTab(w, "Output")

        # Appearance ------------------------------------------------------------
        w, f = _form()
        self.theme = QComboBox()
        for k, v in [("system", "Match system"), ("dark", "Dark"), ("light", "Light")]:
            self.theme.addItem(v, k)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        f.addRow("Light / dark", self.theme)
        self.accent = QComboBox()
        self.accent.addItem("Match system accent color", "system")
        self.accent.addItem("Yakusuru vermilion", "brand")
        self.accent.addItem("Custom…", "custom")
        self.custom_accent = settings.accent if settings.accent.startswith("#") else ""
        if self.custom_accent:
            self.accent.setItemText(2, f"Custom ({self.custom_accent})")
        self.accent.setCurrentIndex(2 if self.custom_accent else max(0, self.accent.findData(settings.accent)))
        self.accent.activated.connect(self._accent_chosen)
        self.swatch = QLabel()
        self.swatch.setFixedSize(22, 22)
        arow = QWidget()
        ah = QHBoxLayout(arow)
        ah.setContentsMargins(0, 0, 0, 0)
        ah.addWidget(self.accent, 1)
        ah.addWidget(self.swatch)
        f.addRow("Accent color", arow)
        f.addRow("", label("“Match system” follows your OS accent color (macOS System Settings → Appearance, "
                           "Windows Settings → Personalization → Colors, GNOME or KDE) and updates live, "
                           "as does light/dark mode.", "Hint"))
        self._update_swatch()
        tabs.addTab(w, "Appearance")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _accent_value(self) -> str:
        k = self.accent.currentData()
        return self.custom_accent if k == "custom" and self.custom_accent else ("brand" if k == "custom" else k)

    def _accent_chosen(self, idx: int):
        if self.accent.itemData(idx) == "custom":
            from PySide6.QtGui import QColor
            from PySide6.QtWidgets import QColorDialog
            start = QColor(self.custom_accent or theme.current()["accent"])
            c = QColorDialog.getColor(start, self, "Accent color")
            if c.isValid():
                self.custom_accent = c.name()
                self.accent.setItemText(2, f"Custom ({self.custom_accent})")
            elif not self.custom_accent:
                self.accent.setCurrentIndex(0)
        self._update_swatch()

    def _update_swatch(self):
        from PySide6.QtWidgets import QApplication
        k = self.accent.currentData()
        dark = theme.is_dark()
        if k == "system":
            col = theme.system_accent_preview() or theme.BRAND_ACCENT["dark" if dark else "light"]
        elif k == "custom" and self.custom_accent:
            col = self.custom_accent
        else:
            col = theme.BRAND_ACCENT["dark" if dark else "light"]
        col = theme.resolve_accent(QApplication.instance(), dark, col)
        self.swatch.setStyleSheet(f"background: {col}; border-radius: 11px; border: 1px solid "
                                  f"{theme.current()['border']};")

    def accept(self):
        s = self.s
        s.accent = self._accent_value()
        s.device = self.device.currentData()
        s.compute_type = self.compute.currentData()
        s.beam_size = self.beam.value()
        s.vad_filter = self.vad.isChecked()
        s.filter_hallucinations = self.hallu.isChecked()
        s.initial_prompt = self.prompt.text().strip()
        s.whispercpp_binary = self.wcpp.text().strip()
        s.hf_cache_dir = self.hf.text().strip()
        s.batch_size = self.batch.value()
        s.context_lines = self.ctx.value()
        s.ollama_url = self.ollama_url.text().strip() or "http://localhost:11434"
        s.openai_compatible_url = self.compat_url.text().strip()
        s.output_mode = "folder" if self.to_folder.isChecked() else "next_to_source"
        s.output_folder = self.out_dir.text().strip()
        s.overwrite = self.overwrite.currentData()
        s.max_line_chars = self.max_en.value()
        s.max_line_chars_cjk = self.max_ja.value()
        s.max_lines = self.max_lines.value()
        s.max_cue_seconds = self.max_cue.value()
        s.min_cue_seconds = self.min_cue.value()
        s.theme = self.theme.currentData()
        for row in self.key_rows:
            row.save()
        s.save()
        super().accept()
