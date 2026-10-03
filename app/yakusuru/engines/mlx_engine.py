"""MLX Whisper engine (Apple Silicon GPU)."""
from __future__ import annotations

import importlib
import logging
import types

from ..subtitles import Cue, Word
from . import Engine

log = logging.getLogger(__name__)

# Whisper retries a 30-second window at higher temperatures when the output looks like a
# hallucination loop. The library default is six steps (0.0…1.0), which on music or silence
# can multiply the work by six; three steps keep the recovery and most of the speed.
TEMPERATURES = (0.0, 0.2, 0.4)


def _tmod():
    # NB: `from mlx_whisper import transcribe` returns the *function* (the package re-exports it),
    # so the module with tqdm and ModelHolder has to be fetched by its dotted name.
    return importlib.import_module("mlx_whisper.transcribe")


class _ProgressShim:
    """Stands in for `tqdm.tqdm` inside mlx_whisper so we can report progress and cancel."""
    callback = None
    cancelled = None

    def __init__(self, total=None, **_kw):
        self.total = total or 1
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, n=1):
        self.n += n
        if _ProgressShim.cancelled and _ProgressShim.cancelled():
            raise InterruptedError()
        if _ProgressShim.callback:
            _ProgressShim.callback(min(1.0, self.n / self.total))

    def close(self):
        pass


class MlxEngine(Engine):
    name = "mlx"
    requires = ("mlx_whisper",)

    def _local_path(self) -> str:
        """The model's folder on disk. mlx_whisper would otherwise call `snapshot_download` on every
        load — a network round-trip (and a silent download of any extra repo files) that can hang
        with no feedback. The pipeline already downloaded what we need, so resolve it offline."""
        from pathlib import Path
        if Path(self.model_id).expanduser().exists():
            return str(Path(self.model_id).expanduser())
        try:
            from huggingface_hub import snapshot_download
            from ..model_fetch import PATTERNS
            return snapshot_download(self.model_id, allow_patterns=PATTERNS["mlx"], local_files_only=True)
        except Exception as e:
            log.info("Model not in the local cache (%s); mlx_whisper will fetch it.", type(e).__name__)
            return self.model_id

    def load(self) -> None:
        import time
        from pathlib import Path
        t0 = time.time()
        log.info("Starting MLX…")
        import mlx.core as mx
        tmod = _tmod()
        log.info("MLX %s ready (%.1fs).", getattr(mx, "__version__", "?"), time.time() - t0)
        if hasattr(tmod, "tqdm"):
            tmod.tqdm = types.SimpleNamespace(tqdm=_ProgressShim)
        else:
            log.warning("mlx_whisper layout changed: progress reporting unavailable.")
        path = self._local_path()
        p = Path(path)
        if p.is_dir():
            weights = [f for f in p.iterdir() if f.suffix in (".safetensors", ".npz")]
            size = sum(f.stat().st_size for f in weights)
            log.info("Model files: %s (%s, %.2f GB)", p, ", ".join(f.name for f in weights) or "no weights!",
                     size / 1e9)
        self.model_id = path            # transcribe() must use the same local path
        # Load the weights now (mlx_whisper caches them), so the "Loading model" stage covers it
        # and transcription starts immediately.
        t1 = time.time()
        log.info("Reading weights into memory…")
        try:
            tmod.ModelHolder.get_model(path, mx.float16)
            log.info("Weights loaded in %.1fs.", time.time() - t1)
        except AttributeError:
            log.debug("mlx_whisper has no ModelHolder; the model loads on first use.")
        self.device_used = f"Apple GPU (MLX {getattr(mx, '__version__', '')})".replace(" )", ")")

    def unload(self) -> None:
        try:
            import mlx.core as mx
            tmod = _tmod()
            tmod.ModelHolder.model = None
            tmod.ModelHolder.model_path = None
            import gc
            gc.collect()
            (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()
        except Exception as e:
            log.debug("MLX unload: %s", e)

    def transcribe(self, audio, wav_path, duration, task, progress, cancelled):
        import mlx_whisper
        _ProgressShim.callback = progress
        _ProgressShim.cancelled = cancelled
        progress(0.0)
        try:
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.model_id,
                verbose=False,              # False = progress bar on (our shim); None = silent
                language=self.lang_arg(),
                task=task,
                temperature=TEMPERATURES,
                word_timestamps=True,
                condition_on_previous_text=False,   # avoids runaway repetition loops
                initial_prompt=self.s.initial_prompt or None,
            )
        finally:
            _ProgressShim.callback = None
            _ProgressShim.cancelled = None
        self.detected_language = result.get("language") if self.lang_arg() is None else None
        cues = []
        for seg in result.get("segments", []):
            words = [Word(w["start"], w["end"], w["word"]) for w in seg.get("words", []) or []]
            c = Cue(seg["start"], seg["end"], words=words)
            if task == "translate":
                c.tgt = seg["text"].strip()
            else:
                c.src = seg["text"].strip()
            cues.append(c)
        return cues
