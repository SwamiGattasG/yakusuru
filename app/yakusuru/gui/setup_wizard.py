"""First-run setup wizard: detect hardware → install components → download models → API keys."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
                               QWizard, QWizardPage)

from .. import APP_NAME, paths
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


class ComponentRow(QWidget):
    def __init__(self, comp, parent=None):
        super().__init__(parent)
        self.comp = comp
        g = QGridLayout(self)
        g.setContentsMargins(4, 6, 4, 6)
        g.setHorizontalSpacing(10)
        self.check = QCheckBox()
        self.check.setChecked(comp.recommended and comp.available and not comp.manual)
        self.check.setEnabled(comp.available and not comp.manual)
        self.dot = StatusDot()
        title = QLabel(f"<b>{_(comp.title)}</b>  <span style='color:{theme.current()['muted']}'>{comp.size}</span>"
                       + (f"  <span style='color:{theme.current()['accent']}'>" + _("recommended") + "</span>"
                          if comp.recommended else ""))
        desc = label(_(comp.description) if comp.available else f"{_(comp.description)} ({_(comp.unavailable_reason)})",
                     "Hint")
        self.state = QLabel(_("checking…"))
        self.state.setObjectName("Hint")
        self.actions = QHBoxLayout()
        g.addWidget(self.check, 0, 0, 2, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(title, 0, 1)
        g.addWidget(self.dot, 0, 2, Qt.AlignmentFlag.AlignRight)
        g.addWidget(self.state, 0, 3)
        g.addWidget(desc, 1, 1, 1, 3)
        g.addLayout(self.actions, 2, 1, 1, 3)
        g.setColumnStretch(1, 1)
        if not comp.available:
            self.setEnabled(False)

    def set_status(self, ok: bool, detail: str):
        t = theme.current()
        self.dot.set_color(t["ok"] if ok else (t["warn"] if self.comp.recommended else t["muted"]))
        self.state.setText((_("Installed · ") if ok else "") + detail)
        if ok and not self.comp.manual:
            self.check.setChecked(False)


class ComponentsPage(QWizardPage):
    def __init__(self, wiz: "SetupWizard"):
        super().__init__()
        self.wiz = wiz
        self.setTitle(_("Install components"))
        self.setSubTitle(_("Tick what you want and press Install. Green means ready."))
        v = QVBoxLayout(self)
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
            b = QPushButton(_("Download prebuilt…"))
            b.clicked.connect(self._download_whispercpp)
            row.actions.addWidget(b)
            b = QPushButton(_("Locate whisper-cli…"))
            b.clicked.connect(self._locate_whispercpp)
            row.actions.addWidget(b)
            b = QPushButton(_("Build instructions"))
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/ggml-org/whisper.cpp#vulkan-gpu-support")))
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
            r.state.setText(_("checking…"))
        s = self.wiz.s

        def work():
            from ..engines.whispercpp_engine import find_binary
            return check_python_packages(), check_ollama(s.ollama_url), find_binary(s.whispercpp_binary)

        run_async(work, on_done=self._apply_status)

    def _apply_status(self, res):
        pk, ol, wc = res
        self.wiz.ollama_state = ol
        for key, row in self.rows.items():
            ok, detail = status_of(key, pk, ol, wc)
            if not row.comp.available:
                detail = row.comp.unavailable_reason
            row.set_status(ok, detail)
        if "error" in pk:
            self._out(f"Package check failed: {pk['error']}\n")

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
        self._out("\nLooking up the latest whisper.cpp release on GitHub…\n")

        def fetch():
            import requests
            r = requests.get("https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest", timeout=15)
            r.raise_for_status()
            j = r.json()
            return j.get("tag_name", ""), [(a["name"], a["browser_download_url"], a.get("size", 0))
                                           for a in j.get("assets", []) if a["name"].endswith(".zip")]

        def got(res):
            tag, assets = res
            if not assets:
                self._out("No prebuilt zip files in the latest release — use Homebrew, build from source, or "
                          "Locate an existing whisper-cli.\n")
                return
            plat = {"win32": ("win", "x64"), "darwin": ("mac", "apple", "xcframework")}.get(sys.platform, ("linux",))
            ranked = sorted(assets, key=lambda a: (not any(k in a[0].lower() for k in plat),
                                                   "vulkan" not in a[0].lower()))
            names = [f"{a[0]}  ({a[2] / 1e6:.0f} MB)" for a in ranked]
            choice, ok = QInputDialog.getItem(self, f"whisper.cpp {tag}",
                                              _("Choose a build (Vulkan = AMD/Intel GPU, cuBLAS = NVIDIA, "
                                              "BLAS/plain = CPU):"), names, 0, False)
            if not ok:
                return
            name, url, _size = ranked[names.index(choice)]
            self._out(f"Downloading {name}…\n")
            run_async(self._fetch_zip, name, url, on_done=lambda p: (self._out(f"✓ Extracted to {p}\n"),
                                                                     self.refresh()),
                      on_error=lambda e: self._out("✗ " + e.split("\n")[0] + "\n"))

        run_async(fetch, on_done=got, on_error=lambda e: self._out("✗ GitHub lookup failed: " +
                                                                     e.split("\n")[0] + "\n"))

    @staticmethod
    def _fetch_zip(name: str, url: str) -> str:
        import requests
        dest = paths.tools_dir() / "whisper.cpp" / Path(name).stem
        dest.mkdir(parents=True, exist_ok=True)
        zpath = dest.with_suffix(".zip")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(zpath, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
        zpath.unlink(missing_ok=True)
        if os.name != "nt":
            for p in dest.rglob("whisper-cli"):
                p.chmod(0o755)
        return str(dest)

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
        s.engine = self.engine.currentData()
        s.asr_model = self.model.value()
        if self.rb_whisper.isChecked():
            s.translator = "whisper"
        elif self.rb_ollama.isChecked():
            s.translator = "ollama"
            s.translator_models["ollama"] = self.ollama_model.value()
        elif s.translator in ("ollama", "whisper"):
            s.translator = "anthropic"
        s.save()
        return True

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
