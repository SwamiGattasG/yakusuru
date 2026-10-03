"""Glossary: names and terms the translator must render consistently.

* Global glossary   → settings folder (glossary.json)
* Per-file override → "<media name>.glossary.json" next to the media file
Entries: {"source": "田中", "target": "Tanaka", "note": "male, protagonist"}"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import paths


@dataclass
class Term:
    source: str
    target: str
    note: str = ""


def _load(p: Path) -> list[Term]:
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("terms", data) if isinstance(data, dict) else data
        return [Term(d.get("source", ""), d.get("target", ""), d.get("note", ""))
                for d in items if d.get("source")]
    except Exception:
        return []


def _save(p: Path, terms: list[Term]) -> None:
    p.write_text(json.dumps({"terms": [asdict(t) for t in terms if t.source.strip()]},
                            ensure_ascii=False, indent=2), encoding="utf-8")


def load_global() -> list[Term]:
    return _load(paths.glossary_file())


def save_global(terms: list[Term]) -> None:
    _save(paths.glossary_file(), terms)


def sidecar_path(media: Path) -> Path:
    return media.with_name(media.stem + ".glossary.json")


def load_for(media: Path | None) -> list[Term]:
    """Global terms, overridden/extended by the per-file sidecar."""
    merged = {t.source: t for t in load_global()}
    if media is not None:
        for t in _load(sidecar_path(media)):
            merged[t.source] = t
    return list(merged.values())


def save_sidecar(media: Path, terms: list[Term]) -> None:
    _save(sidecar_path(media), terms)


def load_sidecar(media: Path) -> list[Term]:
    return _load(sidecar_path(media))


def relevant(terms: list[Term], texts: list[str]) -> list[Term]:
    """Only send terms that actually occur in this batch (keeps prompts short)."""
    blob = "\n".join(texts)
    return [t for t in terms if t.source and t.source in blob]


def to_prompt(terms: list[Term]) -> str:
    if not terms:
        return ""
    lines = []
    for t in terms:
        note = f"  ({t.note})" if t.note else ""
        lines.append(f"- {t.source} → {t.target}{note}")
    return "\n".join(lines)
