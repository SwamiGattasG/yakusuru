"""Read the user's OS accent color (macOS, Windows, GNOME, KDE).

Returns a "#rrggbb" string, or None when the desktop doesn't expose one; the caller then
falls back to Qt's own reading of the platform palette, then to the Yakusuru vermilion."""
from __future__ import annotations

import configparser
import os
import subprocess
import sys
from pathlib import Path

# macOS: `defaults read -g AppleAccentColor` → NSColor.controlAccentColor (light, dark).
# Missing key = Blue (also used by "Multicolor").
_MAC = {
    None: ("#007AFF", "#0A84FF"),   # blue / multicolor
    "-1": ("#8C8C8C", "#989898"),   # graphite
    "0": ("#E0383E", "#FF5257"),    # red
    "1": ("#F7821B", "#F7821B"),    # orange
    "2": ("#FFC600", "#FFC600"),    # yellow
    "3": ("#62BA46", "#62BA46"),    # green
    "4": ("#007AFF", "#0A84FF"),    # blue
    "5": ("#953D96", "#A550A7"),    # purple
    "6": ("#F74F9E", "#F74F9E"),    # pink
}

# GNOME 47+ accent names → libadwaita colors.
_GNOME = {
    "blue": "#3584E4", "teal": "#2190A4", "green": "#3A944A", "yellow": "#C88800", "orange": "#ED5B00",
    "red": "#E62D42", "pink": "#D56199", "purple": "#9141AC", "slate": "#6F8396",
}


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# Asks AppKit for the exact accent (NSColor.controlAccentColor) in the requested appearance —
# correct for every macOS version and accent, including ones the table above doesn't know.
_JXA = """ObjC.import('AppKit');
var ap = $.NSAppearance.appearanceNamed(%s ? 'NSAppearanceNameDarkAqua' : 'NSAppearanceNameAqua');
$.NSAppearance.setCurrentAppearance(ap);
var c = $.NSColor.controlAccentColor.colorUsingColorSpace($.NSColorSpace.sRGBColorSpace);
[c.redComponent, c.greenComponent, c.blueComponent].map(function (v) { return Math.round(v * 255); }).join(',')"""
_mac_cache: dict = {}


def _macos_exact(dark: bool) -> str | None:
    out = _run(["osascript", "-l", "JavaScript", "-e", _JXA % ("true" if dark else "false")])
    try:
        r, g, b = (int(x) for x in (out or "").split(","))
        return f"#{r:02X}{g:02X}{b:02X}"
    except ValueError:
        return None


def _macos(dark: bool) -> str | None:
    raw = _run(["defaults", "read", "-g", "AppleAccentColor"])   # cheap: tells us when it changed
    key = (raw, dark)
    if key not in _mac_cache:
        light_hex, dark_hex = _MAC.get(raw, _MAC[None])
        exact = _macos_exact(dark)
        lum = None
        if exact:
            r, g, b = (int(exact[i:i + 2], 16) for i in (1, 3, 5))
            lum = (max(r, g, b) + min(r, g, b)) / 2
        # AppKit can hand back a washed-out tint (e.g. when the query runs outside a GUI session);
        # fall back to the known color for the chosen accent in that case.
        _mac_cache[key] = exact if lum is not None and 30 <= lum <= 225 else (dark_hex if dark else light_hex)
    return _mac_cache[key]


def describe() -> str:
    """One line for logs / System Report."""
    if sys.platform == "darwin":
        raw = _run(["defaults", "read", "-g", "AppleAccentColor"])
        return f"macOS AppleAccentColor={raw!r} → light {_macos(False)} / dark {_macos(True)}"
    return f"accent → {system_accent(False)}"


def _windows() -> str | None:
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as k:
            val, _unused = winreg.QueryValueEx(k, "AccentColor")       # 0xAABBGGRR
        r, g, b = val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        return None


def _kde() -> str | None:
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kdeglobals"
    if not cfg.exists():
        return None
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(cfg, encoding="utf-8")
    except Exception:
        return None
    for section, key in (("General", "AccentColor"), ("Colors:Selection", "BackgroundNormal")):
        v = cp.get(section, key, fallback="")
        parts = [p.strip() for p in v.split(",")]
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            r, g, b = (int(p) for p in parts[:3])
            return f"#{r:02X}{g:02X}{b:02X}"
    return None


def _gnome() -> str | None:
    raw = _run(["gsettings", "get", "org.gnome.desktop.interface", "accent-color"])
    if raw:
        return _GNOME.get(raw.strip("'\""))
    return None


def system_accent(dark: bool) -> str | None:
    if sys.platform == "darwin":
        return _macos(dark)
    if os.name == "nt":
        return _windows()
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    if "kde" in desktop:
        return _kde() or _gnome()
    return _gnome() or _kde()
