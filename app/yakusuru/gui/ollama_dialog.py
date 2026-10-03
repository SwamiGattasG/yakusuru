"""One-click Ollama installer dialog (download progress, cancel, clear errors)."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QProgressBar, QVBoxLayout

from .widgets import label


class _Worker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        from ..ollama_install import install
        try:
            where = install(lambda f, m: self.progress.emit(f, m), lambda: self._cancel)
        except InterruptedError:
            self.failed.emit("Cancelled.")
            return
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.finished_ok.emit(where)


class InstallOllamaDialog(QDialog):
    """Run with exec(); returns Accepted once Ollama is installed and starting."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install Ollama")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        title = QLabel("Installing Ollama — the free, private local translator")
        title.setObjectName("H2")
        v.addWidget(title)
        v.addWidget(label("Downloaded from ollama.com and installed for your user account (no admin "
                          "password needed). Yakusuru starts it and downloads the translation model "
                          "the first time you translate.", "Hint"))
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        v.addWidget(self.bar)
        self.status = QLabel("Connecting…")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.rejected.connect(self._cancel)
        v.addWidget(self.buttons)
        self.where = ""
        self.worker = _Worker(self)
        self.worker.progress.connect(self._progress)
        self.worker.finished_ok.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _progress(self, frac: float, msg: str):
        if frac < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
        self.status.setText(msg)

    def _done(self, where: str):
        self.where = where
        self.accept()

    def _failed(self, err: str):
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.status.setText("✗ " + err + "\n\nYou can also install it manually from ollama.com/download.")
        self.buttons.clear()
        self.buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)

    def _cancel(self):
        if self.worker.isRunning():
            self.status.setText("Cancelling…")
            self.worker.cancel()
            self.worker.wait(5000)
        self.reject()

    def closeEvent(self, e):
        if self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        super().closeEvent(e)
