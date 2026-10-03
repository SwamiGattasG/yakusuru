"""Transcription engines. Each engine turns 16 kHz mono audio into Cues: the transcript in
`Cue.src`, or English in `Cue.tgt` when task == 'translate' (Whisper's built-in translation).
Engines set `detected_language` when the source language was auto-detected."""
from __future__ import annotations

import importlib.util
import logging
from typing import Callable

from ..subtitles import Cue

log = logging.getLogger(__name__)

Progress = Callable[[float], None]
Cancelled = Callable[[], bool]


class Engine:
    name = "base"
    requires: tuple[str, ...] = ()

    def __init__(self, model_id: str, settings):
        self.model_id = model_id
        self.s = settings
        self.device_used = "?"
        self.detected_language: str | None = None

    def lang_arg(self) -> str | None:
        """Whisper language code for the configured source language, or None to auto-detect."""
        from ..languages import AUTO, get
        sl = getattr(self.s, "source_lang", "ja")
        if not sl or sl == AUTO:
            return None
        return get(sl).whisper

    @classmethod
    def available(cls) -> tuple[bool, str]:
        missing = [m for m in cls.requires if importlib.util.find_spec(m) is None]
        if missing:
            return False, "missing Python package(s): " + ", ".join(missing)
        return True, ""

    def load(self) -> None:
        raise NotImplementedError

    def transcribe(self, audio, wav_path: str, duration: float, task: str,
                   progress: Progress, cancelled: Cancelled) -> list[Cue]:
        raise NotImplementedError

    def unload(self) -> None:
        pass


def get_engine_class(name: str) -> type[Engine]:
    if name == "faster_whisper":
        from .faster_whisper_engine import FasterWhisperEngine
        return FasterWhisperEngine
    if name == "mlx":
        from .mlx_engine import MlxEngine
        return MlxEngine
    if name == "transformers":
        from .transformers_engine import TransformersEngine
        return TransformersEngine
    if name == "whispercpp":
        from .whispercpp_engine import WhisperCppEngine
        return WhisperCppEngine
    if name == "dummy":
        from .dummy_engine import DummyEngine
        return DummyEngine
    raise ValueError(f"Unknown engine: {name}")


def engine_status(name: str) -> tuple[bool, str]:
    try:
        return get_engine_class(name).available()
    except Exception as e:  # pragma: no cover
        return False, str(e)
