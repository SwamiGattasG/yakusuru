"""API key storage: OS keychain via `keyring` (macOS Keychain, Windows Credential
Manager, Secret Service on Linux). Environment variables always win, so keys
can also be supplied as ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, DEEPL_API_KEY."""
from __future__ import annotations

import json
import logging
import os

from . import APP_ID, paths
from .models import API_KEY_ENV

log = logging.getLogger(__name__)
_FALLBACK = None  # path to a plain-file fallback when no keyring backend works


def _fallback_path():
    return paths.config_dir() / "keys.json"


def _keyring():
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def get_key(provider: str) -> str:
    env = API_KEY_ENV.get(provider)
    if env and os.environ.get(env):
        return os.environ[env].strip()
    kr = _keyring()
    if kr:
        try:
            v = kr.get_password(APP_ID, provider)
            if v:
                return v
            from .paths import LEGACY_IDS
            for old in LEGACY_IDS:            # keys saved under the app's previous name
                v = kr.get_password(old, provider)
                if v:
                    try:
                        kr.set_password(APP_ID, provider, v)
                    except Exception:
                        pass
                    return v
        except Exception as e:
            log.warning("keyring read failed: %s", e)
    p = _fallback_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get(provider, "")
        except Exception:
            pass
    return ""


def set_key(provider: str, value: str) -> str:
    """Store a key. Returns where it was stored ('keychain' or 'file')."""
    value = value.strip()
    kr = _keyring()
    if kr:
        try:
            if value:
                kr.set_password(APP_ID, provider, value)
            else:
                try:
                    kr.delete_password(APP_ID, provider)
                except Exception:
                    pass
            return "keychain"
        except Exception as e:
            log.warning("keyring write failed, using file fallback: %s", e)
    p = _fallback_path()
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if value:
        data[provider] = value
    else:
        data.pop(provider, None)
    p.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return "file"


def has_key(provider: str) -> bool:
    return bool(get_key(provider))
