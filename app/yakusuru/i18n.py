"""Interface translations.

Usage in code:   from .i18n import _          label = _("Add Files")
                 _("Done · {n} lines").format(n=12)      # placeholders stay in the key

Each language is one JSON file in yakusuru/locales/<code>.json mapping the English text to its
translation. Anything missing falls back to English, so a partial file is fine. To add a
language: copy locales/es.json, translate the values, and add the code to UI_LANGUAGES.

No Qt here: the worker process uses it too (progress messages)."""
from __future__ import annotations

import json
import locale
import os
from pathlib import Path

LOCALES = Path(__file__).resolve().parent / "locales"

# code → name shown in the language picker (in its own language)
UI_LANGUAGES = {
    "en": "English", "de": "Deutsch", "es": "Español", "fr": "Français", "it": "Italiano",
    "pt": "Português (Brasil)", "ru": "Русский", "ja": "日本語", "ko": "한국어",
}
# Qt's own translations (OK / Cancel buttons) are named by locale; Brazilian for Portuguese.
QT_LOCALE = {"pt": "pt_BR"}

_catalog: dict[str, str] = {}
_current = "en"


def system_language() -> str:
    """Best guess of the OS interface language, as one of UI_LANGUAGES (else English)."""
    cands = []
    try:
        from PySide6.QtCore import QLocale
        cands += [n.replace("_", "-") for n in QLocale.system().uiLanguages()]
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        if os.environ.get(var):
            cands += os.environ[var].replace("_", "-").split(":")
    try:
        loc = locale.getlocale()[0]
        if loc:
            cands.append(loc.replace("_", "-"))
    except Exception:
        pass
    for c in cands:
        base = c.split("-")[0].split(".")[0].lower()
        if base in UI_LANGUAGES:
            return base
    return "en"


def set_language(code: str) -> str:
    """Activate a language ("system" picks the OS one). Returns the code actually used."""
    global _catalog, _current
    if not code or code == "system":
        code = system_language()
    code = code if code in UI_LANGUAGES else "en"
    _catalog = {}
    if code != "en":
        try:
            _catalog = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _catalog = {}
    _current = code
    return code


def current() -> str:
    return _current


def _(text: str) -> str:
    """Translate interface text (English in, current language out)."""
    return _catalog.get(text) or text


def lang_label(code: str, english_name: str, native: str) -> str:
    """'Japanese — 日本語' in English, 'Japonés — 日本語' in Spanish, '日本語' in Japanese."""
    name = _(english_name)
    return name if not native or native == name else f"{name} — {native}"
