"""Application entry point."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_NAME, __version__, paths
from ..config import Settings
from . import theme


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir() / "app.log", maxBytes=2_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)


ICON_RED = ("#D0202E", "#D0202E")       # flat red (no gradient)


def make_icon(size: int = 256) -> QPixmap:
    """The app icon: white 訳 on red. Uses the shipped artwork when present (identical to the
    Finder / Dock / taskbar icon); otherwise draws it."""
    from pathlib import Path
    asset = Path(__file__).resolve().parents[2] / "assets" / "icon.png"
    if asset.exists():
        pm = QPixmap(str(asset))
        if not pm.isNull():
            return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    from PySide6.QtGui import QLinearGradient
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    m = size * 0.0977
    r = QRectF(m, m, size - 2 * m, size - 2 * m)
    path = QPainterPath()
    path.addRoundedRect(r, r.width() * 0.225, r.width() * 0.225)
    g = QLinearGradient(0, r.top(), 0, r.bottom())
    g.setColorAt(0, QColor(ICON_RED[0]))
    g.setColorAt(1, QColor(ICON_RED[1]))
    p.fillPath(path, g)
    p.setPen(QColor("#ffffff"))
    f = QFont()
    f.setPixelSize(int(r.height() * 0.62))
    f.setBold(True)
    p.setFont(f)
    p.drawText(r, Qt.AlignmentFlag.AlignCenter, "訳")
    p.end()
    return pm


def _excepthook(exc_type, exc, tb):
    import traceback
    msg = "".join(traceback.format_exception(exc_type, exc, tb))
    logging.getLogger("crash").error(msg)
    app = QApplication.instance()
    if app is not None:
        box = QMessageBox(QMessageBox.Icon.Critical, APP_NAME, f"Unexpected error: {exc}")
        box.setDetailedText(msg)
        box.exec()


def run(argv: list[str]) -> int:
    _setup_logging()
    logging.getLogger(__name__).info("%s %s starting (Python %s, %s)", APP_NAME, __version__,
                                     sys.version.split()[0], sys.platform)
    sys.excepthook = _excepthook
    settings = Settings.load()
    if settings.hf_cache_dir:
        os.environ["HF_HOME"] = settings.hf_cache_dir

    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setOrganizationName("Yakusuru")
    app = QApplication(argv)
    app.setWindowIcon(QIcon(make_icon()))
    theme.apply(app, settings.theme, settings.accent)
    theme.watcher = theme.ThemeWatcher(app)       # follow OS light/dark + accent changes live
    try:
        from .system_colors import describe
        logging.getLogger(__name__).info("Theme: %s mode, accent %s (%s)", "dark" if theme.is_dark() else "light",
                                         theme.current()["accent"], describe())
    except Exception:
        pass

    if not settings.setup_complete or "--setup" in argv:
        from .setup_wizard import SetupWizard
        wiz = SetupWizard(settings)
        wiz.exec()
        if not settings.setup_complete:
            # Let the user in anyway; the status bar explains what's missing.
            settings.setup_complete = True
            settings.save()

    from .main_window import MainWindow
    win = MainWindow(settings)
    win.show()
    files = [a for a in argv[1:] if not a.startswith("-") and os.path.exists(a)]
    if files:
        from pathlib import Path
        win.add_paths([Path(f) for f in files])
    return app.exec()
