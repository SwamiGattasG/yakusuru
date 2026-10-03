from __future__ import annotations

import logging

from ..models import is_anime_model
from ..subtitles import Cue, Word
from . import Engine

log = logging.getLogger(__name__)


class FasterWhisperEngine(Engine):
    name = "faster_whisper"
    requires = ("faster_whisper",)

    def load(self) -> None:
        from . import cuda_libs
        cuda_libs.preload()
        import ctranslate2
        from faster_whisper import WhisperModel

        want = self.s.device
        has_cuda = False
        try:
            has_cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            pass
        device = "cuda" if (want in ("auto", "cuda") and has_cuda) else "cpu"
        if want == "cuda" and not has_cuda:
            log.warning("CUDA requested but no CUDA device visible to CTranslate2 — using CPU.")
        compute = self.s.compute_type
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        log.info("Loading faster-whisper model %s on %s (%s)…", self.model_id, device, compute)
        try:
            self.model = WhisperModel(self.model_id, device=device, compute_type=compute)
        except ValueError:
            # e.g. float16 unsupported on older GPUs
            compute = "int8_float16" if device == "cuda" else "int8"
            self.model = WhisperModel(self.model_id, device=device, compute_type=compute)
        self.device_used = f"{device}/{compute}"

    def transcribe(self, audio, wav_path, duration, task, progress, cancelled):
        kotoba = "kotoba" in self.model_id.lower()
        anime = is_anime_model(self.model_id)
        self.detected_language = None
        segments, info = self.model.transcribe(
            audio,
            language=self.lang_arg(),
            task=task,
            beam_size=max(1, int(self.s.beam_size)),
            vad_filter=bool(self.s.vad_filter),
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=True,
            condition_on_previous_text=False,   # avoids runaway repetition loops
            initial_prompt=(self.s.initial_prompt or None) if not anime else None,
            chunk_length=15 if kotoba else None,
            no_repeat_ngram_size=5 if anime else 0,
        )
        if self.lang_arg() is None:
            self.detected_language = getattr(info, "language", None)
        cues: list[Cue] = []
        for seg in segments:  # generator: decoding happens while iterating
            if cancelled():
                raise InterruptedError()
            words = [Word(w.start, w.end, w.word) for w in (seg.words or [])]
            text = seg.text.strip()
            c = Cue(seg.start, seg.end, words=words)
            if task == "translate":
                c.tgt = text
            else:
                c.src = text
            cues.append(c)
            if duration:
                progress(min(1.0, seg.end / duration))
        return cues

    def unload(self) -> None:
        self.model = None
