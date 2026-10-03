"""Hugging Face Transformers engine — runs on CUDA, ROCm (AMD on Linux), Apple MPS or CPU.
Used for Japanese-specialised checkpoints such as anime-whisper and kotoba-whisper."""
from __future__ import annotations

import logging

from ..models import is_anime_model
from ..subtitles import Cue
from . import Engine

log = logging.getLogger(__name__)

BLOCK_SECONDS = 300   # we feed the pipeline 5-minute blocks so progress can be reported


def _quiet_cut(audio, target: int, sr: int, search_s: float = 15.0) -> int:
    """Find the quietest 0.5 s window within ±search_s of `target` so blocks split at a pause."""
    import numpy as np
    lo = max(0, target - int(search_s * sr))
    hi = min(len(audio), target + int(search_s * sr))
    win = sr // 2
    if hi - lo <= win:
        return target
    seg = audio[lo:hi]
    nwin = len(seg) // win
    energy = np.square(seg[: nwin * win].reshape(nwin, win)).mean(axis=1)
    return lo + int(np.argmin(energy)) * win + win // 2


class TransformersEngine(Engine):
    name = "transformers"
    requires = ("torch", "transformers")

    def load(self) -> None:
        import torch
        from transformers import pipeline

        want = self.s.device
        if want in ("auto", "cuda") and torch.cuda.is_available():
            device, dtype = "cuda:0", torch.float16
            name = "ROCm" if getattr(torch.version, "hip", None) else "CUDA"
            self.device_used = f"{name}: {torch.cuda.get_device_name(0)}"
        elif want in ("auto", "mps") and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device, dtype = "mps", torch.float16
            self.device_used = "Apple GPU (MPS)"
        else:
            device, dtype = "cpu", torch.float32
            self.device_used = "CPU"
        log.info("Loading transformers model %s on %s…", self.model_id, self.device_used)
        self.pipe = pipeline("automatic-speech-recognition", model=self.model_id,
                             torch_dtype=dtype, device=device)
        self.anime = is_anime_model(self.model_id)

    def transcribe(self, audio, wav_path, duration, task, progress, cancelled):
        sr = 16000
        gen = {"task": task}
        if self.lang_arg():
            gen["language"] = self.lang_arg()    # omitted → the model detects the language
        if self.anime:
            # Recommended by the model author: no prompt, discourage repetition loops.
            gen.update({"no_repeat_ngram_size": 5, "repetition_penalty": 1.0, "num_beams": 1})
        else:
            gen["num_beams"] = max(1, int(self.s.beam_size))
        n = len(audio)
        cues: list[Cue] = []
        off = 0
        while off < n:
            if cancelled():
                raise InterruptedError()
            end = _quiet_cut(audio, off + BLOCK_SECONDS * sr, sr) if off + BLOCK_SECONDS * sr < n else n
            chunk = audio[off:end]
            t0 = off / sr
            out = self.pipe({"raw": chunk, "sampling_rate": sr}, chunk_length_s=30, batch_size=8,
                            return_timestamps=True, generate_kwargs=gen)
            chunk_end = t0 + len(chunk) / sr
            for ch in out.get("chunks", []):
                a, b = ch.get("timestamp", (None, None))
                if a is None:
                    continue
                b = b if b is not None else min(a + 5.0, chunk_end - t0)
                text = (ch.get("text") or "").strip()
                if not text:
                    continue
                c = Cue(t0 + a, t0 + b)
                if task == "translate":
                    c.tgt = text
                else:
                    c.src = text
                cues.append(c)
            off = end
            progress(min(1.0, off / n))
        return cues

    def unload(self) -> None:
        self.pipe = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
