"""Per-user, per-OS locations.

Everything heavy (virtual environment, logs, whisper.cpp binaries) lives in the
user's local application-data folder — never inside the project folder, which
may be synced by iCloud / OneDrive / Dropbox. Downloaded models use the normal
Hugging Face cache (~/.cache/huggingface) unless overridden in Settings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID

LEGACY_IDS = ("LanguageInterpreter",)   # earlier name of the app — data is moved over once


def _adopt_legacy(new: Path) -> None:
    """Move a folder created under the app's old name to the new name (keeps the environment,
    settings, glossary and logs, so nothing has to be reinstalled)."""
    if new.exists():
        return
    for old_id in LEGACY_IDS:
        old = new.with_name(old_id)
        if old.is_dir():
            try:
                old.rename(new)
            except OSError:
                pass
            return


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    d = base / APP_ID
    _adopt_legacy(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    if sys.platform == "darwin" or os.name == "nt":
        d = data_dir()
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / APP_ID
        _adopt_legacy(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tools_dir() -> Path:
    d = data_dir() / "tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    """Folder for models that are not managed by the Hugging Face cache (whisper.cpp ggml files)."""
    d = data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    return config_dir() / "settings.json"


def glossary_file() -> Path:
    return config_dir() / "glossary.json"
