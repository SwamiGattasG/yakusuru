"""First-run setup wizard: detect hardware → install components → download models → API keys."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
                               QWizard, QWizardPage)

from .. import APP_NAME
from ..config import Settings
from ..deps import check_ollama, check_python_packages, components_for, status_of
from ..hardware import PROFILE_DEFAULTS, PROFILES, detect, format_report
from ..models import ASR_MODELS, ENGINES, TRANSLATOR_MODELS
from . import theme
from .async_util import run_async
from .widgets import Card, ModelCombo, StatusDot, label
from ..i18n import _

log = logging.getLogger(__name__)
APP_ROOT = Path(__file__).resolve().parents[2]   # .../app  (contains the yakusuru package)


# ============================================================================ process runner
class CommandQueue(QObject):
    """Runs a list of commands one after another, streaming output."""
    output = Signal(str)
    finished = Signal(bool)      # all succeeded?
    step = Signal(int, int, str)  # index, total, title

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cmds: list[tuple[str, str, list[str]]] = []
        self.proc: QProcess | None = None
        self.i = 0
        self.ok = True

    def run(self, cmds: list[tuple[str, str, list[str]]]):
        self.cmds, self.i, self.ok = cmds, 0, True
        self._start_next()

    def busy(self) -> bool:
        return self.proc is not None

    def cancel(self):
        if self.proc is not None:
            self.cmds = self.cmds[: self.i + 1]
            self.proc.kill()

    def _start_next(self):
        if self.i >= len(self.cmds):
            self.proc = None
            self.finished.emit(self.ok)
            return
        title, program, args = self.cmds[self.i]
        self.step.emit(self.i, len(self.cmds), title)
        self.output.emit(f"\n━━ {title}\n$ {Path(program).name} {' '.join(args)}\n")
        p = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        env.insert("PYTHONPATH", str(APP_ROOT) + os.pathsep + env.value("PYTHONPATH", ""))
        p.setProcessEnvironment(env)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(lambda: self._read(p))
        p.finished.connect(self._done)
        p.errorOccurred.connect(lambda err: self.output.emit(f"Could not start: {err}\n"))
        self.proc = p
        p.start(program, args)

    def _read(self, p: QProcess):
        data = bytes(p.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.output.emit(data.replace("\r", "\n"))

    def _done(self, code, _status):
        if code != 0:
            self.ok = False
            self.output.emit(f"✗ exited with code {code}\n")
        else:
            self.output.emit("✓ done\n")
        self.i += 1
        self._start_next()


def pip_cmd(title: str, args: list[str], force: bool = False) -> tuple[str, str, list[str]]:
    extra = ["--force-reinstall"] if force and args and args[0] == "torch" else []
    return title, sys.executable, ["-m", "pip", "install", "--progress-bar", "off", *extra, *args]


# ============================================================================ Ollama pull thread
class OllamaPull(QThread):
    progress = Signal(float, str)
    done = Signal(bool, str)

    def __init__(self, url: str, model: str, parent=None):
        super().__init__(parent)
        self.url, self.model = url.rstrip("/"), model

    def run(self):
        import requests
        try:
            with requests.post(f"{self.url}/api/pull", json={"model": self.model, "stream": True},
                               stream=True, timeout=(5, 3600)) as r:
                if r.status_code >= 400:
                    self.done.emit(False, r.text[:300])
                    return
                for line in r.iter_lines():
                    if not line:
                        continue
                    ev = json.loads(line)
                    if "error" in ev:
                        self.done.emit(False, ev["error"])
                        return
                    tot, comp = ev.get("total"), ev.get("completed")
                    frac = (comp / tot) if tot and comp else -1.0
                    self.progress.emit(frac, ev.get("status", ""))
            self.done.emit(True, "")
        except Exception as e:
            self.done.emit(False, str(e))


# ============================================================================ pages
class WelcomePage(QWizardPage):
    def __init__(self, wiz: "SetupWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle(_("Welcome to {app}").format(app=APP_NAME))
        self.setSubTitle(_("Let's get your computer ready to transcribe and translate media. "
                         "This takes a few minutes; everything is installed into the app's own environment."))
        v = QVBoxLayout(self)
        card = Card(_("Your system"))
        self.report = QLabel(_("Detecting hardware…"))
        self.report.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = self.report.font()
        f.setFamily("Menlo" if sys.platform == "darwin" else "Consolas" if os.name == "nt" else "Monospace")
        self.report.setFont(f)
        card.add(self.report)
        v.addWidget(card)
        prof = Card(_("Acceleration profile"))
        self.profile = QComboBox()
        for k, lab in PROFILES.items():
            self.profile.addItem(_(lab), k)
        prof.add(self.profile)
        prof.add(label(_("Only profiles that work on this OS and CPU architecture are listed. The recommended "
                       "one is pre-selected; it decides which engines are installed next."), "Hint"))
        v.addWidget(prof)
        self.warn = label("", "")
        self.warn.hide()
        v.addWidget(self.warn)
        v.addStretch(1)
        run_async(detect, on_done=self._detected)

    def _detected(self, hw):
        self.wiz.hw = hw
        self.report.setText(format_report(hw))
        # Only offer profiles that exist for this OS + CPU architecture.
        self.profile.clear()
        for k in (hw.profiles or list(PROFILES)):
            self.profile.addItem(_(PROFILES[k]) + (_("  (recommended)") if k == hw.recommended else ""), k)
        cur = self.wiz.s.hardware_profile if self.wiz.s.hardware_profile in (hw.profiles or PROFILES) \
            else hw.recommended
        self.profile.setCurrentIndex(max(0, self.profile.findData(cur)))
        t = theme.current()
        if not hw.python_ok:
            self.warn.setText(f"<b style='color:{t['err']}'>" + _("Python problem:") + f"</b> {hw.python_note}<br>"
                              + _("Quit, then run the launcher again (macOS: <i>Install or Repair.command</i>). "
                                  "It will rebuild the environment."))
            self.warn.show()
        elif hw.notes:
            self.warn.setText("<br>".join(hw.notes))
            self.warn.show()
        self.completeChanged.emit()

    def isComplete(self):
        return self.wiz.hw is not None

    def validatePage(self):
        hw = self.wiz.hw
        if hw is not None and not hw.python_ok:
            r = QMessageBox.warning(self, _("Unsupported Python"), hw.python_note +
                                    "\n\nComponents can't be installed with this Python. Continue anyway?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return False
        prof = self.profile.currentData()
        if prof != self.wiz.s.hardware_profile:
            self.wiz.s.hardware_profile = prof
            d = PROFILE_DEFAULTS[prof]
            self.wiz.s.engine, self.wiz.s.asr_model, self.wiz.s.device = d["engine"], d["asr_model"], d["device"]
        self.wiz.s.save()
        return True


STATUS_W = 210      # width of the Status column, shared by the header and every row
CHECK_W = 22        # width of the checkbox column


class StatusCell(QWidget):
    """The Status column of one component: a coloured dot, a short state, and the detail under it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(STATUS_W)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.dot = StatusDot()
        self.text = QLabel()
        top.addWidget(self.dot)
        top.addWidget(self.text, 1)
        v.addLayout(top)
        self.detail = QLabel()
        self.detail.setObjectName("Hint")
        v.addWidget(self.detail)
        v.addStretch(1)

    def show_state(self, color: str, text: str, detail: str = ""):
        self.dot.set_color(color)
        self.text.setText(f"<b>{text}</b>")
        fm = self.detail.fontMetrics()
        self.detail.setText(fm.elidedText(detail, Qt.TextElideMode.ElideMiddle, STATUS_W - 4))
        self.setToolTip(detail if detail else "")


class ComponentRow(QWidget):
    def __init__(self, comp, parent=None):
        super().__init__(parent)
        self.comp = comp
        g = QGridLayout(self)
        g.setContentsMargins(4, 8, 4, 8)
        g.setHorizontalSpacing(10)
        g.setColumnMinimumWidth(0, CHECK_W)
        self.check = QCheckBox()
        self.check.setChecked(comp.recommended and comp.available and not comp.manual)
        self.check.setEnabled(comp.available and not comp.manual)
        title = QLabel(f"<b>{_(comp.title)}</b>  <span style='color:{theme.current()['muted']}'>{comp.size}</span>"
                       + (f"  <span style='color:{theme.current()['accent']}'>" + _("recommended") + "</span>"
                          if comp.recommended else ""))
        desc = label(_(comp.description), "Hint")
        self.status = StatusCell()
        self.actions = QHBoxLayout()
        g.addWidget(self.check, 0, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(title, 0, 1)
        g.addWidget(desc, 1, 1)
        g.addLayout(self.actions, 2, 1)
        g.addWidget(self.status, 0, 2, 3, 1, Qt.AlignmentFlag.AlignTop)
        g.setColumnStretch(1, 1)
        self.set_checking()
        if not comp.available:
            self.check.setEnabled(False)
            self.status.show_state(theme.current()["muted"], _("Not available"), _(comp.unavailable_reason))

    def set_checking(self):
        if self.comp.available:
            self.status.show_state(theme.current()["muted"], _("Checking…"))

    def set_status(self, ok: bool, detail: str):
        t = theme.current()
        if not self.comp.available:
            return
        if ok:
            self.status.show_state(t["ok"], _("Installed"), detail)
            if not self.comp.manual:
                self.check.setChecked(False)
        elif detail == "installed but not running":
            self.status.show_state(t["warn"], _("Not running"), _("Installed, press Start Ollama"))
        else:
            missing = detail in ("not installed", "not found", "whisper-cli not found", "")
            self.status.show_state(t["warn"] if self.comp.recommended else t["muted"], _("Not installed"),
                                   "" if missing else _(detail))

    def set_error(self, detail: str):
        if self.comp.available:
            self.status.show_state(theme.current()["err"], _("Couldn't check"), detail)


PIP_KEYS = ("torch", "faster_whisper", "mlx_whisper", "transformers", "furigana")


class ComponentsPage(QWizardPage):
    def __init__(self, wiz: "SetupWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle(_("Install components"))
        self.setSubTitle(_("Tick what you want and press Install. Green means ready."))
        v = QVBoxLayout(self)
        head = QWidget()
        hg = QGridLayout(head)
        hg.setContentsMargins(14, 0, 14 + self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent), 0)
        hg.setHorizontalSpacing(10)
        hg.setColumnMinimumWidth(0, CHECK_W)
        for col, text in ((1, _("Component")), (2, _("Status"))):
            lab = QLabel(f"<b>{text}</b>")
            lab.setObjectName("Hint")
            if col == 2:
                lab.setFixedWidth(STATUS_W)
            hg.addWidget(lab, 0, col)
        hg.setColumnStretch(1, 1)
        v.addWidget(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.rows_host = QWidget()
        self.rows_lay = QVBoxLayout(self.rows_host)
        self.rows_lay.setSpacing(2)
        self.scroll.setWidget(self.rows_host)
        v.addWidget(self.scroll, 3)
        bar = QHBoxLayout()
        self.btn_install = QPushButton(_("Install selected"))
        self.btn_install.setObjectName("Primary")
        self.btn_install.clicked.connect(self.install)
        self.btn_cancel = QPushButton(_("Cancel install"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(lambda: self.runner.cancel())
        self.force = QCheckBox(_("Force-reinstall PyTorch"))
        self.force.setToolTip(_("Use when switching between the CPU and GPU builds of PyTorch"))
        self.btn_refresh = QPushButton(_("Re-check"))
        self.btn_refresh.clicked.connect(self.refresh)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(180)
        self.step_label = QLabel("")
        self.step_label.setObjectName("Hint")
        for w in (self.btn_install, self.btn_cancel, self.force):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.step_label)
        bar.addWidget(self.progress)
        bar.addWidget(self.btn_refresh)
        v.addLayout(bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setPlaceholderText(_("Installer output appears here."))
        v.addWidget(self.log, 2)
        self.runner = CommandQueue(self)
        self.runner.output.connect(self._out)
        self.runner.step.connect(self._step)
        self.runner.finished.connect(self._finished)
        self.rows: dict[str, ComponentRow] = {}

    def initializePage(self):
        for r in self.rows.values():
            r.setParent(None)
        self.rows.clear()
        while self.rows_lay.count():
            it = self.rows_lay.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
        for comp in components_for(self.wiz.s.hardware_profile or "cpu", self.wiz.hw):
            row = ComponentRow(comp)
            self.rows[comp.key] = row
            self.rows_lay.addWidget(row)
            self._add_actions(row)
        self.rows_lay.addStretch(1)
        self.refresh()

    def _add_actions(self, row: ComponentRow):
        key = row.comp.key
        if key == "whispercpp":
            if sys.platform == "darwin" and shutil.which("brew"):
                b = QPushButton(_("Install with Homebrew"))
                b.clicked.connect(lambda: self._run([("Homebrew: whisper-cpp", shutil.which("brew"),
                                                      ["install", "whisper-cpp"])]))
                row.actions.addWidget(b)
            if row.comp.fetch:
                b = QPushButton(_("Download prebuilt…"))
                b.clicked.connect(self._download_whispercpp)
                row.actions.addWidget(b)
            b = QPushButton(_("Locate whisper-cli…"))
            b.clicked.connect(self._locate_whispercpp)
            row.actions.addWidget(b)
            b = QPushButton(_("Build instructions"))
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/ggml-org/whisper.cpp#quick-start")))
            row.actions.addWidget(b)
            row.actions.addStretch(1)
        elif key == "ollama":
            b = QPushButton(_("Install Ollama"))
            b.setObjectName("Primary")
            b.setToolTip(_("Downloads Ollama from ollama.com and installs it for your account (no admin needed)"))
            b.clicked.connect(self._install_ollama)
            row.actions.addWidget(b)
            b = QPushButton(_("Start Ollama"))
            b.clicked.connect(self._start_ollama)
            row.actions.addWidget(b)
            row.actions.addStretch(1)

    def refresh(self):
        for r in self.rows.values():
            r.set_checking()
        self.btn_refresh.setEnabled(False)
        s = self.wiz.s

        def work():
            # Each check on its own, so one failure can't leave the other rows without a status.
            from ..engines.whispercpp_engine import find_binary
            out = {}
            for name, fn in (("pk", check_python_packages), ("ol", lambda: check_ollama(s.ollama_url)),
                             ("wc", lambda: find_binary(s.whispercpp_binary))):
                try:
                    out[name] = fn()
                except Exception as e:
                    out[name + "_error"] = f"{type(e).__name__}: {e}"
            return out

        run_async(work, on_done=self._apply_status, on_error=self._check_failed)

    def _check_failed(self, err: str):
        self.btn_refresh.setEnabled(True)
        msg = err.strip().splitlines()[-1] if err.strip() else ""
        for row in self.rows.values():
            row.set_error(msg)
        self._out(f"Component check failed: {msg}\n")

    def _apply_status(self, res):
        self.btn_refresh.setEnabled(True)
        pk = res.get("pk") or {"error": res.get("pk_error", "")}
        ol, wc = res.get("ol"), res.get("wc")
        self.wiz.ollama_state = ol
        pk_err = pk.get("error")
        for key, row in self.rows.items():
            if key == "ollama" and "ol_error" in res:
                row.set_error(res["ol_error"])
                continue
            if pk_err and key in PIP_KEYS:
                row.set_error(pk_err.strip().splitlines()[-1] if pk_err.strip() else "")
                continue
            try:
                ok, detail = status_of(key, pk, ol, wc)
            except Exception as e:
                row.set_error(str(e))
                continue
            row.set_status(ok, detail)
        if pk_err:
            self._out(f"Package check failed: {pk_err}\n")

    def _run(self, cmds):
        if self.runner.busy():
            return
        self.btn_install.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setRange(0, len(cmds))
        self.progress.setValue(0)
        self.runner.run(cmds)

    def install(self):
        cmds = []
        for key, row in self.rows.items():
            if row.check.isChecked() and not row.comp.manual:
                if row.comp.fetch:
                    cmds.append((row.comp.title, sys.executable, ["-m", "yakusuru.tools.fetch", *row.comp.fetch]))
                for args in row.comp.commands:
                    cmds.append(pip_cmd(row.comp.title, args, self.force.isChecked()))
        if not cmds:
            QMessageBox.information(self, _("Nothing selected"), _("Tick at least one component to install."))
            return
        self._run(cmds)

    def _out(self, text: str):
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()

    def _step(self, i, n, title):
        self.progress.setValue(i)
        self.step_label.setText(f"{i + 1}/{n}: {title}")

    def _finished(self, ok):
        self.progress.setValue(self.progress.maximum())
        self.btn_install.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.step_label.setText(_("All done ✓") if ok else _("Finished with errors — see log"))
        if not ok:
            self._out("\nSome steps failed. Common fixes: check your internet connection, make sure you "
                      "have a few GB free, then press Install again.\n")
        self.refresh()

    # --- whisper.cpp ------------------------------------------------------
    def _locate_whispercpp(self):
        f, _unused = QFileDialog.getOpenFileName(self, _("Locate whisper-cli"), str(Path.home()))
        if f:
            self.wiz.s.whispercpp_binary = f
            self.wiz.s.save()
            self.refresh()

    def _download_whispercpp(self):
        comp = self.rows["whispercpp"].comp if "whispercpp" in self.rows else None
        if not comp or not comp.fetch:
            QMessageBox.information(self, "whisper.cpp", _("There is no ready-made whisper.cpp for this "
                                    "system. On a Mac, install it with Homebrew: brew install whisper-cpp"))
            return
        self._run([("whisper.cpp", sys.executable, ["-m", "yakusuru.tools.fetch", *comp.fetch])])

    # --- Ollama -------------------------------------------------------------
    def _install_ollama(self):
        from .ollama_dialog import InstallOllamaDialog
        dlg = InstallOllamaDialog(self)
        if dlg.exec():
            self._out(f"✓ Ollama installed at {dlg.where} and starting.\n")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, self.refresh)

    def _start_ollama(self):
        from ..ollama_install import is_installed, start
        ol = self.wiz.ollama_state or {}
        if ol.get("running"):
            QMessageBox.information(self, _("Ollama"), _("Ollama is already running."))
            return
        if not is_installed():
            self._install_ollama()
            return
        try:
            start()
            self._out("Starting Ollama…\n")
        except Exception as e:
            self._out(f"Could not start Ollama: {e}\n")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3500, self.refresh)


class ModelsPage(QWizardPage):
    def __init__(self, wiz: "SetupWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle(_("Choose and download models"))
        self.setSubTitle(_("Models download once and are reused. You can change them any time on the main window."))
        v = QVBoxLayout(self)

        asr = Card(_("Speech recognition"))
        f = QFormLayout()
        self.engine = QComboBox()
        for k, lab in ENGINES.items():
            self.engine.addItem(_(lab), k)
        self.model = ModelCombo()
        f.addRow(_("Engine"), self.engine)
        f.addRow(_("Model"), self.model)
        asr.lay.addLayout(f)
        h = QHBoxLayout()
        self.btn_dl = QPushButton(_("Download model now"))
        self.btn_dl.clicked.connect(self.download_asr)
        self.dl_state = QLabel("")
        self.dl_state.setObjectName("Hint")
        h.addWidget(self.btn_dl)
        h.addWidget(self.dl_state, 1)
        asr.lay.addLayout(h)
        asr.add(label(_("<b>large-v3</b> and <b>turbo</b> handle ~100 languages. For Japanese: "
                      "<b>kotoba-whisper</b> is fast and accurate for clean speech; "
                      "<b>anime-whisper</b> (Transformers engine) excels at emotive anime dialogue. "
                      "Both are Japanese-only."), "Hint"))
        v.addWidget(asr)

        tr = Card(_("Translation"))
        self.rb_ollama = QRadioButton(_("Local LLM via Ollama — free && private"))
        self.rb_cloud = QRadioButton(_("Cloud API (Claude, Grok, OpenAI, Gemini, DeepL) — best quality, light on your Mac, needs a key"))
        self.rb_whisper = QRadioButton(_("Whisper built-in — fastest, most literal"))
        grp = QButtonGroup(self)
        for rb in (self.rb_ollama, self.rb_cloud, self.rb_whisper):
            grp.addButton(rb)
            tr.add(rb)
        oh = QHBoxLayout()
        self.ollama_model = ModelCombo()
        self.ollama_model.set_items(TRANSLATOR_MODELS["ollama"], wiz.s.translator_models.get("ollama", "qwen3:14b"))
        self.btn_pull = QPushButton(_("Pull model"))
        self.btn_pull.clicked.connect(self.pull)
        self.pull_bar = QProgressBar()
        self.pull_bar.setRange(0, 1000)
        self.pull_bar.setMaximumWidth(160)
        self.pull_bar.hide()
        oh.addWidget(QLabel(_("Ollama model")))
        oh.addWidget(self.ollama_model, 1)
        oh.addWidget(self.btn_pull)
        oh.addWidget(self.pull_bar)
        tr.lay.addLayout(oh)
        self.pull_state = label("", "Hint")
        tr.add(self.pull_state)
        v.addWidget(tr)
        v.addStretch(1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.setMaximumBlockCount(800)
        v.addWidget(self.log)

        self.runner = CommandQueue(self)
        self.runner.output.connect(lambda t: (self.log.moveCursor(self.log.textCursor().MoveOperation.End),
                                              self.log.insertPlainText(t), self.log.ensureCursorVisible()))
        self.runner.finished.connect(self._dl_done)
        self.engine.currentIndexChanged.connect(self._fill_models)
        self.puller = None

    def initializePage(self):
        s = self.wiz.s
        self.engine.blockSignals(True)
        self.engine.setCurrentIndex(max(0, self.engine.findData(s.engine)))
        self.engine.blockSignals(False)
        self._fill_models(keep=s.asr_model)
        t = s.translator
        (self.rb_whisper if t == "whisper" else self.rb_ollama if t == "ollama" else self.rb_cloud).setChecked(True)
        ol = self.wiz.ollama_state or check_ollama(s.ollama_url)
        if ol.get("running"):
            self.pull_state.setText(_("Installed in Ollama: ") + (", ".join(ol.get("models", [])) or _("none yet")))
        else:
            self.pull_state.setText(_("Ollama isn't running — start it on the previous page to pull a model."))

    def _fill_models(self, *_a, keep: str = ""):
        eng = self.engine.currentData()
        items = [(m.id, f"{m.label} · {m.size_gb:.1f} GB") for m in ASR_MODELS.get(eng, [])]
        self.model.set_items(items, keep or (items[0][0] if items else ""))

    def download_asr(self):
        eng, mid = self.engine.currentData(), self.model.value()
        if not mid:
            return
        self.btn_dl.setEnabled(False)
        self.dl_state.setText(_("Downloading… (large models can take a while)"))
        self.runner.run([(f"Download {mid}", sys.executable, ["-m", "yakusuru.tools.fetch", "asr", eng, mid])])

    def _dl_done(self, ok):
        self.btn_dl.setEnabled(True)
        self.dl_state.setText(_("✓ Model downloaded") if ok else _("✗ Download failed — see log"))

    def pull(self):
        name = self.ollama_model.value()
        if not name:
            return
        self.btn_pull.setEnabled(False)
        self.pull_bar.show()
        self.pull_bar.setValue(0)
        self.puller = OllamaPull(self.wiz.s.ollama_url, name, self)
        self.puller.progress.connect(self._pull_progress)
        self.puller.done.connect(self._pull_done)
        self.puller.start()

    def _pull_progress(self, frac, status):
        if frac >= 0:
            self.pull_bar.setRange(0, 1000)
            self.pull_bar.setValue(int(frac * 1000))
        self.pull_state.setText(status)

    def _pull_done(self, ok, err):
        self.btn_pull.setEnabled(True)
        self.pull_bar.hide()
        self.pull_state.setText(f"✓ {self.ollama_model.value()} is ready" if ok else f"✗ {err}")

    def validatePage(self):
        s = self.wiz.s
        if self.runner.busy():
            QMessageBox.information(self, _("Still downloading"),
                                    _("The speech model is still downloading. Please wait until it finishes."))
            return False
        s.engine = self.engine.currentData()
        s.asr_model = self.model.value()
        if not self._model_ready_or_ok():
            return False
        if self.rb_whisper.isChecked():
            s.translator = "whisper"
        elif self.rb_ollama.isChecked():
            s.translator = "ollama"
            s.translator_models["ollama"] = self.ollama_model.value()
        elif s.translator in ("ollama", "whisper"):
            s.translator = "anthropic"
        s.save()
        return True

    def _model_ready_or_ok(self) -> bool:
        """The speech model is required. If it isn't downloaded, say so and offer the choices."""
        from ..model_fetch import describe, needs_download
        from ..models import find_asr
        eng, mid = self.engine.currentData(), self.model.value()
        try:
            if not mid or not needs_download(eng, mid):
                return True
        except Exception:
            return True
        m = find_asr(eng, mid)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(_("Speech model required"))
        box.setText(_("Yakusuru can't transcribe without a speech recognition model, and this one "
                      "isn't downloaded yet:"))
        box.setInformativeText(describe([(mid, m.size_gb if m else None)]) + "\n\n" +
                               _("Download it now, or let Yakusuru download it automatically the first "
                                 "time you press Start."))
        now = box.addButton(_("Download Now"), QMessageBox.ButtonRole.AcceptRole)
        now.setObjectName("Primary")
        later = box.addButton(_("Download on First Run"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(now)
        box.exec()
        if box.clickedButton() is now:
            self.download_asr()
            return False          # stay here and show the progress; Next works once it's done
        return box.clickedButton() is later

    def cleanupPage(self):
        pass


class KeysPage(QWizardPage):
    def __init__(self, wiz: "SetupWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle(_("API keys (optional)"))
        self.setSubTitle(_("Only needed for cloud translators. Keys are saved in your system keychain."))
        v = QVBoxLayout(self)
        from .settings_dialog import ApiKeyRow
        from ..models import TRANSLATORS
        f = QFormLayout()
        self.rows = []
        for prov in ("anthropic", "openai", "gemini", "xai", "deepl"):
            r = ApiKeyRow(prov, wiz.s)
            self.rows.append(r)
            f.addRow(TRANSLATORS[prov], r)
        v.addLayout(f)
        self.pick = QComboBox()
        for prov in ("anthropic", "openai", "gemini", "xai", "deepl"):
            self.pick.addItem(_(TRANSLATORS[prov]), prov)
        h = QHBoxLayout()
        h.addWidget(QLabel(_("Default cloud translator")))
        h.addWidget(self.pick, 1)
        v.addLayout(h)
        v.addWidget(label(_('Get keys: <a href="https://console.anthropic.com/">Anthropic</a> · '
                          '<a href="https://platform.openai.com/api-keys">OpenAI</a> · '
                          '<a href="https://aistudio.google.com/apikey">Google AI Studio</a> · '
                      '<a href="https://console.x.ai/">xAI</a> · '
                          '<a href="https://www.deepl.com/your-account/keys">DeepL</a>'), "Hint"))
        v.addStretch(1)

    def initializePage(self):
        t = self.wiz.s.translator
        if t in ("anthropic", "openai", "gemini", "xai", "deepl"):
            self.pick.setCurrentIndex(self.pick.findData(t))

    def validatePage(self):
        for r in self.rows:
            r.save()
        if self.wiz.s.translator not in ("ollama", "whisper", "openai_compatible"):
            self.wiz.s.translator = self.pick.currentData()
        self.wiz.s.save()
        return True


class DonePage(QWizardPage):
    def __init__(self, wiz: "SetupWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle(_("You're all set"))
        v = QVBoxLayout(self)
        self.summary = label("", "")
        v.addWidget(self.summary)
        v.addWidget(label(_("Drop videos onto the main window and press <b>Start</b>. Finished files can be "
                          "reviewed in the <b>Subtitle Editor</b> (double-click a finished row). "
                          "Re-open this wizard any time with the <b>Setup</b> button on the main window."), "Muted"))
        v.addStretch(1)

    def initializePage(self):
        from ..models import TRANSLATORS
        s = self.wiz.s
        model = s.model_for() if s.translator != "whisper" else ""
        self.summary.setText(
            f"<p><b>{_('Transcription:')}</b> {_(ENGINES.get(s.engine, ''))} · {s.asr_model}</p>"
            f"<p><b>{_('Translation:')}</b> {_(TRANSLATORS.get(s.translator, ''))} {('· ' + model) if model else ''}</p>"
            f"<p><b>{_('Profile:')}</b> {_(PROFILES.get(s.hardware_profile, ''))}</p>")

    def validatePage(self):
        self.wiz.s.setup_complete = True
        self.wiz.s.save()
        return True


class SetupWizard(QWizard):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.hw = None
        self.ollama_state = None
        self.setWindowTitle(f"{APP_NAME} — Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setMinimumSize(860, 700)
        for page in (WelcomePage(self), ComponentsPage(self), ModelsPage(self), KeysPage(self), DonePage(self)):
            self.addPage(page)
        self.setButtonText(QWizard.WizardButton.FinishButton, _("Open the app"))
