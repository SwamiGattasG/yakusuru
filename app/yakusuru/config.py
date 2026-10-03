"""Persistent user settings (JSON). Unknown keys are ignored, missing keys get defaults."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from . import paths


@dataclass
class Settings:
    # --- first run -------------------------------------------------------
    setup_complete: bool = False
    hardware_profile: str = ""            # see hardware.PROFILES

    # --- languages --------------------------------------------------------
    source_lang: str = "ja"               # Whisper code, or "auto" to detect
    target_lang: str = "en"               # any code in languages.py (or free BCP-47 for LLMs)

    # --- transcription ----------------------------------------------------
    engine: str = "faster_whisper"        # faster_whisper | mlx | transformers | whispercpp
    asr_model: str = "large-v3"
    device: str = "auto"                  # auto | cuda | mps | cpu
    compute_type: str = "auto"            # faster-whisper: auto | float16 | int8_float16 | int8
    vad_filter: bool = True
    beam_size: int = 5
    initial_prompt: str = ""              # ignored for anime-whisper (degrades it)
    whispercpp_binary: str = ""           # path to whisper-cli (whisper.cpp)
    filter_hallucinations: bool = True

    # --- translation ------------------------------------------------------
    translate: bool = True                # off = transcript only (translate later with "Run Again")
    translator: str = "ollama"            # whisper | ollama | anthropic | openai | gemini | deepl | openai_compatible
    translator_models: dict = field(default_factory=lambda: {
        "ollama": "qwen3:14b",
        "anthropic": "claude-sonnet-5-5",
        "openai": "gpt-5.4-mini",
        "gemini": "gemini-3.5-flash",
        "xai": "grok-4.7",
        "deepl": "quality_optimized",
        "openai_compatible": "",
    })
    ollama_url: str = "http://localhost:11434"
    openai_compatible_url: str = "http://localhost:1234/v1"
    content_type: str = "anime"           # anime | film | vlog | lecture
    honorifics: str = "keep"              # keep | localize
    batch_size: int = 25                  # subtitle lines per LLM request
    context_lines: int = 6                # previous lines sent as context
    show_notes: str = ""                  # free text: series / speaker info for the LLM

    # --- output -----------------------------------------------------------
    out_translation: bool = True          # name.<target>.srt
    out_original: bool = True             # name.<source>.srt (the transcript)
    out_bilingual: bool = False           # name.<source>-<target>.srt
    out_furigana: bool = False            # extra Japanese file(s) with kana readings over kanji
    furigana_style: str = "inline"        # inline (.furigana.srt) | ruby (.vtt) | both
    output_mode: str = "next_to_source"   # next_to_source | folder
    output_folder: str = ""
    overwrite: str = "rename"             # rename | overwrite | skip
    max_line_chars: int = 42              # languages written with spaces
    max_line_chars_cjk: int = 22          # full-width scripts (Japanese, Chinese, Korean)
    max_lines: int = 2
    max_cue_seconds: float = 7.0
    min_cue_seconds: float = 0.8

    # --- UI ---------------------------------------------------------------
    theme: str = "system"                 # system | dark | light
    accent: str = "system"                # system (OS accent color) | brand (vermilion) | "#rrggbb"
    window_geometry: str = ""
    queue_columns: str = ""               # saved column widths / order of the queue table
    hf_cache_dir: str = ""                # optional override for HF_HOME

    def model_for(self, translator: str | None = None) -> str:
        t = translator or self.translator
        return self.translator_models.get(t, "")

    # --- persistence --------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        p = paths.config_file()
        s = cls()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                s.update(data)
            except Exception:
                pass
        return s

    # Keys renamed when the app went multilingual → new names.
    _RENAMED = {"out_english": "out_translation", "out_japanese": "out_original",
                "max_line_chars_en": "max_line_chars", "max_line_chars_ja": "max_line_chars_cjk"}

    def line_limit(self, lang_code: str | None) -> int:
        from .languages import get
        return self.max_line_chars_cjk if get(lang_code).wide else self.max_line_chars

    def update(self, data: dict[str, Any]) -> None:
        names = {f.name for f in fields(self)}
        data = {self._RENAMED.get(k, k): v for k, v in data.items()
                if not (k in self._RENAMED and self._RENAMED[k] in data)}
        for k, v in data.items():
            if k not in names:
                continue
            if k == "translator_models" and isinstance(v, dict):
                merged = dict(self.translator_models)
                merged.update(v)
                v = merged
            setattr(self, k, v)

    def save(self) -> None:
        p = paths.config_file()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
