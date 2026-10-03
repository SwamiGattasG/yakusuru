"""Look & feel: Fusion style + a palette and stylesheet in light and dark variants.

Follows the operating system by default: light/dark mode and the accent color
(macOS accent color, Windows accent color, GNOME/KDE accent), updating live when
the user changes them. The Yakusuru vermilion (朱色) or a custom color can be chosen
instead in Settings → Appearance."""
from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QStyledItemDelegate

log = logging.getLogger(__name__)

BRAND_ACCENT = {"dark": "#e8603c", "light": "#d24e2a"}

TOKENS = {
    "dark": {
        # Neutral macOS-style grays (no blue cast).
        "bg": "#27272b", "surface": "#2d2e33", "surface2": "#36373c", "border": "#404146",
        "text": "#ececee", "muted": "#9e9ea4", "accent": "#e8603c", "accent_hover": "#f07552",
        "accent_text": "#ffffff", "indigo": "#6f86d6", "ok": "#4fb37a", "warn": "#e0a43a", "err": "#e5534b",
        "selection": "#3a2f2c", "input": "#212125",
    },
    "light": {
        "bg": "#f6f5f2", "surface": "#ffffff", "surface2": "#efede8", "border": "#dcd8d0",
        "text": "#1e2028", "muted": "#6b6f7c", "accent": "#d24e2a", "accent_hover": "#bf4321",
        "accent_text": "#ffffff", "indigo": "#3f5bb5", "ok": "#2f8f5b", "warn": "#b9801c", "err": "#c63a32",
        "selection": "#f8ddd3", "input": "#ffffff",
    },
}

_current = "dark"
_tokens: dict = dict(TOKENS["dark"])
_mode_setting = "system"
_accent_setting = "system"
_qt_platform_accent: dict[str, str] = {}
watcher: "ThemeWatcher | None" = None
_captured = False   # Qt's reading of the OS accent, captured before we override it


def current() -> dict:
    return _tokens


def is_dark() -> bool:
    return _current == "dark"


def _system_is_dark(app: QApplication) -> bool:
    try:
        scheme = app.styleHints().colorScheme()
        if scheme != Qt.ColorScheme.Unknown:
            return scheme == Qt.ColorScheme.Dark
    except Exception:
        pass
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


# --------------------------------------------------------------------------- accent maths
def _luminance(c: QColor) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() * (1 - t) + b.red() * t), round(a.green() * (1 - t) + b.green() * t),
                  round(a.blue() * (1 - t) + b.blue() * t))


def derive(base: dict, accent_hex: str, dark: bool) -> dict:
    """Tokens with the accent (and everything derived from it) replaced."""
    t = dict(base)
    acc = QColor(accent_hex)
    if not acc.isValid():
        return t
    bg = QColor(t["bg"])
    # Keep accent-colored text readable on the background (e.g. yellow on white, graphite on dark).
    for _i in range(12):
        if _contrast(acc, bg) >= 3.0:
            break
        acc = acc.lighter(112) if dark else acc.darker(112)
    t["accent"] = acc.name()
    t["accent_hover"] = (acc.lighter(112) if dark else acc.darker(110)).name()
    white, ink = QColor("#ffffff"), QColor("#1e2028")
    # White on the accent like native buttons, unless the accent is too light for it (yellow, lime…).
    t["accent_text"] = "#ffffff" if _contrast(acc, white) >= 2.2 else ink.name()
    # Alternating queue rows: a quiet step from the surface, distinct from progress-bar tracks.
    t["row_alt"] = _mix(QColor(t["surface"]), QColor(t["text"]), 0.035 if dark else 0.04).name()
    t["selection"] = _mix(QColor(t["surface"]), acc, 0.28 if dark else 0.20).name()
    return t


def _usable_accent(hexval: str | None) -> bool:
    """Reject readings that can't be an accent: near-white or near-black (a failed OS query)."""
    c = QColor(hexval or "")
    return c.isValid() and 30 <= c.lightness() <= 225


def resolve_accent(app: QApplication, dark: bool, setting: str) -> str:
    """"system" → OS accent; "brand" → vermilion; "#rrggbb" → custom."""
    mode = "dark" if dark else "light"
    if setting.startswith("#") and QColor(setting).isValid():
        if _usable_accent(setting):
            return setting
        # A near-white / near-black custom accent makes buttons and highlights unreadable
        # (white text on it, or invisible against the background). Use the system accent instead.
        log.warning("Custom accent %s is too light or dark to use; following the system accent.", setting)
        setting = "system"
    if setting == "system":
        from .system_colors import system_accent
        for hexval in (system_accent(dark), _qt_platform_accent.get(mode)):
            if _usable_accent(hexval):
                return hexval
            if hexval:
                log.warning("Ignoring unusable system accent %s", hexval)
    return BRAND_ACCENT[mode]


def system_accent_preview(app: QApplication | None = None) -> str | None:
    """The OS accent as it would be used right now (for the Settings preview)."""
    from .system_colors import system_accent
    return system_accent(is_dark()) or _qt_platform_accent.get(_current)


def _capture_platform_accent(app: QApplication) -> None:
    """Before our palette replaces it, remember what Qt read from the OS (macOS/Windows/portal).
    Runs once: afterwards app.palette() is our own palette."""
    global _captured
    if _captured:
        return
    _captured = True
    pal = app.palette()
    try:
        c = pal.color(QPalette.ColorRole.Accent)
    except AttributeError:          # Qt < 6.6
        c = pal.color(QPalette.ColorRole.Highlight)
    # Ignore greys and Qt's built-in default (#308cc6) — those mean the platform has no accent.
    if c.isValid() and c.saturation() > 25 and c.name().lower() != "#308cc6":
        key = "dark" if pal.color(QPalette.ColorRole.Window).lightness() < 128 else "light"
        _qt_platform_accent[key] = c.name()
        _qt_platform_accent.setdefault("dark" if key == "light" else "light", c.name())


class _ComboPolisher(QObject):
    """Gives every dropdown a styled item delegate, so the stylesheet decides how its highlighted
    row looks (Fusion's default menu-style delegate ignores it and can pair light text with a
    light accent)."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Polish and isinstance(obj, QComboBox) \
                and not obj.property("_yk_delegate"):
            obj.setProperty("_yk_delegate", True)
            obj.setItemDelegate(QStyledItemDelegate(obj))
        return False


_combo_polisher: "_ComboPolisher | None" = None


def apply(app: QApplication, mode: str = "system", accent: str = "system") -> None:
    global _current, _tokens, _mode_setting, _accent_setting, _combo_polisher
    _capture_platform_accent(app)
    if _combo_polisher is None:
        _combo_polisher = _ComboPolisher(app)
        app.installEventFilter(_combo_polisher)
    _mode_setting, _accent_setting = mode, accent
    _current = ("dark" if _system_is_dark(app) else "light") if mode == "system" else mode
    t = derive(TOKENS[_current], resolve_accent(app, _current == "dark", accent), _current == "dark")
    _tokens = t
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    pal.setColor(QPalette.ColorRole.Window, c(t["bg"]))
    pal.setColor(QPalette.ColorRole.WindowText, c(t["text"]))
    pal.setColor(QPalette.ColorRole.Base, c(t["input"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, c(t["surface2"]))
    pal.setColor(QPalette.ColorRole.Text, c(t["text"]))
    pal.setColor(QPalette.ColorRole.Button, c(t["surface2"]))
    pal.setColor(QPalette.ColorRole.ButtonText, c(t["text"]))
    # Highlighted rows (dropdown lists, menus, tables) use the soft accent tint with normal text,
    # so they stay readable whatever the accent is. Buttons get the full accent via the stylesheet.
    pal.setColor(QPalette.ColorRole.Highlight, c(t["selection"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, c(t["text"]))
    try:
        pal.setColor(QPalette.ColorRole.Accent, c(t["accent"]))
    except AttributeError:
        pass
    pal.setColor(QPalette.ColorRole.ToolTipBase, c(t["surface2"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, c(t["text"]))
    pal.setColor(QPalette.ColorRole.PlaceholderText, c(t["muted"]))
    pal.setColor(QPalette.ColorRole.Link, c(t["indigo"]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, c(t["muted"]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(t) + indicator_css(t))


class ThemeWatcher(QObject):
    """Re-applies the theme when the OS switches light/dark or changes its accent color."""
    changed = Signal()

    def __init__(self, app: QApplication):
        super().__init__(app)
        self.app = app
        self._last = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)          # coalesce the burst of events a theme switch produces
        self._timer.timeout.connect(self.refresh)
        try:
            app.styleHints().colorSchemeChanged.connect(lambda *_a: self._timer.start())
        except Exception:
            pass
        app.installEventFilter(self)
        # Some desktops change the accent without telling Qt; check occasionally (cheap).
        self._poll = QTimer(self)
        self._poll.setInterval(4000)
        self._poll.timeout.connect(self._check_accent)
        self._poll.start()
        self._last = self._signature()

    def _signature(self):
        dark = _system_is_dark(self.app) if _mode_setting == "system" else _mode_setting == "dark"
        acc = resolve_accent(self.app, dark, _accent_setting) if _accent_setting == "system" else _accent_setting
        return dark, acc

    def eventFilter(self, obj, ev):
        if obj is self.app and ev.type() in (QEvent.Type.ThemeChange, QEvent.Type.ApplicationPaletteChange):
            if _mode_setting == "system" or _accent_setting == "system":
                self._timer.start()
        return False

    def _check_accent(self):
        if _accent_setting == "system" or _mode_setting == "system":
            if self._signature() != self._last:
                self.refresh()

    def refresh(self):
        sig = self._signature()
        if sig == self._last:
            return
        self._last = sig
        apply(self.app, _mode_setting, _accent_setting)
        for w in self.app.topLevelWidgets():
            w.update()
        self.changed.emit()

    def reapply(self, mode: str, accent: str):
        apply(self.app, mode, accent)
        self._last = self._signature()
        self.changed.emit()


def indicator_css(t: dict) -> str:
    """Checkbox / radio indicators drawn from tiny SVGs (Fusion's defaults are low-contrast in dark mode)."""
    try:
        from .. import paths
        d = paths.data_dir() / "ui"
        d.mkdir(parents=True, exist_ok=True)
        ink = t.get("accent_text", "#ffffff")
        tag = ink.lstrip("#").lower()
        files = {
            f"check-{tag}.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                                 f'<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{ink}" stroke-width="2.2" '
                                 'stroke-linecap="round" stroke-linejoin="round"/></svg>'),
            f"dot-{tag}.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                               f'<circle cx="8" cy="8" r="3.5" fill="{ink}"/></svg>'),
        }
        for name, svg in files.items():
            f = d / name
            if not f.exists() or f.read_text() != svg:
                f.write_text(svg)
        check = (d / f"check-{tag}.svg").as_posix()
        dot = (d / f"dot-{tag}.svg").as_posix()
    except Exception:
        return ""
    return f"""
    QCheckBox::indicator, QRadioButton::indicator, QTableView::indicator {{ width: 16px; height: 16px; }}
    QCheckBox::indicator {{ border: 1.5px solid {t['muted']}; border-radius: 4px; background: {t['input']}; }}
    QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
    QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url({check}); }}
    QCheckBox::indicator:disabled {{ border-color: {t['border']}; background: {t['surface2']}; }}
    QRadioButton::indicator {{ border: 1.5px solid {t['muted']}; border-radius: 9px; background: {t['input']}; }}
    QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
    QRadioButton::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url({dot}); }}
    """


def stylesheet(t: dict) -> str:
    return f"""
    QWidget {{ font-size: 13px; }}
    QMainWindow, QDialog, QWizard {{ background: {t['bg']}; }}
    QToolTip {{ background: {t['surface2']}; color: {t['text']}; border: 1px solid {t['border']};
                padding: 6px; border-radius: 6px; }}

    QFrame#Card {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px; }}
    QLabel#CardTitle {{ font-size: 12px; font-weight: 600; color: {t['muted']};
                        letter-spacing: 1px; text-transform: uppercase; }}
    QLabel#H1 {{ font-size: 22px; font-weight: 700; }}
    QLabel#H2 {{ font-size: 16px; font-weight: 600; }}
    QLabel#Muted {{ color: {t['muted']}; }}
    QLabel#Hint {{ color: {t['muted']}; font-size: 12px; }}
    QLabel#Brand {{ font-size: 17px; font-weight: 700; }}
    QLabel#BrandMark {{ color: {t['accent']}; font-size: 20px; font-weight: 800; }}

    QPushButton {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 8px;
                   padding: 7px 14px; color: {t['text']}; }}
    QPushButton:hover {{ border-color: {t['accent']}; }}
    QPushButton:pressed {{ background: {t['border']}; }}
    QPushButton:disabled {{ color: {t['muted']}; border-color: {t['border']}; }}
    QPushButton#Primary {{ background: {t['accent']}; color: {t['accent_text']}; border: none; font-weight: 600; }}
    QPushButton#Primary:hover {{ background: {t['accent_hover']}; }}
    QPushButton#Primary:disabled {{ background: {t['border']}; color: {t['muted']}; }}
    QPushButton#Danger {{ color: {t['err']}; }}
    QPushButton#Danger[armed="true"] {{ background: {t['err']}; color: #ffffff; border: none; font-weight: 600; }}
    QPushButton#Danger[armed="true"]:hover {{ background: {t['err']}; border: 1px solid {t['text']}; }}
    QPushButton#Ghost {{ background: transparent; border: none; color: {t['muted']}; padding: 6px 8px; }}
    QPushButton#Ghost:hover {{ color: {t['text']}; }}
    QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 6px 10px;
                   color: {t['text']}; }}
    QToolButton:hover {{ background: {t['surface2']}; }}
    QToolButton:checked {{ background: {t['selection']}; }}

    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {t['input']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 6px 8px;
        selection-background-color: {t['accent']}; min-height: 20px; }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus,
    QDoubleSpinBox:focus {{ border-color: {t['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled,
    QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {t['muted']}; background: {t['surface2']};
        border-color: {t['surface2']}; }}
    QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['border']};
                                   selection-background-color: {t['selection']}; selection-color: {t['text']};
                                   outline: none; padding: 4px; }}
    QComboBox QAbstractItemView::item {{ padding: 5px 8px; border-radius: 5px; color: {t['text']}; }}
    QComboBox QAbstractItemView::item:disabled {{ color: {t['muted']}; }}
    QComboBox QAbstractItemView::item:selected, QComboBox QAbstractItemView::item:hover {{
        background: {t['accent']}; color: {t['accent_text']}; }}
    QComboBox QAbstractItemView::item:disabled:selected, QComboBox QAbstractItemView::item:disabled:hover {{
        background: {t['surface2']}; color: {t['muted']}; }}

    QCheckBox, QRadioButton {{ spacing: 8px; }}

    QTableView, QTreeView, QListView {{ background: {t['surface']}; alternate-background-color: {t['surface2']};
        border: 1px solid {t['border']}; border-radius: 10px; gridline-color: {t['border']};
        selection-background-color: {t['selection']}; selection-color: {t['text']}; outline: none; }}
    QHeaderView::section {{ background: {t['surface']}; color: {t['muted']}; border: none;
        border-bottom: 1px solid {t['border']}; padding: 8px 6px; font-weight: 600; }}
    QTableView#Queue QHeaderView::section {{ border-right: 1px solid {t['border']}; padding: 8px; }}
    QTableView#Queue QHeaderView::section:last {{ border-right: none; }}
    QTableView#Queue {{ alternate-background-color: {t['row_alt']}; }}
    QTableView#Queue::item {{ padding-left: 4px; }}
    QTableView#Queue::item:alternate {{ background: {t['row_alt']}; }}
    QTableView#Queue::item:selected {{ background: {t['selection']}; }}
    QTableView#Queue QHeaderView {{ background: transparent; border: none;
        border-top-left-radius: 9px; border-top-right-radius: 9px; }}
    QTableView#Queue QHeaderView::section:first {{ border-top-left-radius: 9px; }}
    QTableView#Queue QHeaderView::section:last {{ border-top-right-radius: 9px; }}
    QTableView#Queue QHeaderView::section:only-one {{ border-top-left-radius: 9px; border-top-right-radius: 9px; }}
    QTableCornerButton::section {{ background: {t['surface']}; border: none; }}

    QTabWidget::pane {{ border: 1px solid {t['border']}; border-radius: 10px; top: -1px;
                        background: {t['surface']}; }}
    QTabBar::tab {{ background: transparent; color: {t['muted']}; padding: 8px 16px; border: none;
                    border-bottom: 2px solid transparent; }}
    QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; }}
    QTabBar::tab:hover {{ color: {t['text']}; }}

    QProgressBar {{ background: {t['surface2']}; border: none; border-radius: 5px; height: 10px;
                    text-align: center; color: transparent; }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 5px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 4px; min-height: 30px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {t['border']}; border-radius: 4px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

    QSplitter::handle {{ background: transparent; }}
    QScrollArea#SidePanel, QScrollArea#SidePanel > QWidget > QWidget {{ background: transparent; border: none; }}
    QStatusBar {{ background: {t['surface']}; border-top: 1px solid {t['border']}; color: {t['muted']}; }}
    QMenuBar {{ background: {t['bg']}; }}
    QMenuBar::item:selected {{ background: {t['surface2']}; }}
    QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; padding: 4px; }}
    QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {t['selection']}; }}
    QGroupBox {{ border: 1px solid {t['border']}; border-radius: 10px; margin-top: 14px; padding: 10px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {t['muted']}; }}
    """
