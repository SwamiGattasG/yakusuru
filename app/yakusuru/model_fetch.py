"""Model download with real progress reporting.

Engines would otherwise fetch weights silently inside their first `transcribe()` call,
which looks like a frozen app. The pipeline calls `ensure_model()` first: it checks the
Hugging Face cache, and if needed downloads the files while polling the bytes written
so the UI can show "Downloading model · 620 / 1,620 MB"."""
from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from .models import find_asr

from .i18n import _ as _t

log = logging.getLogger(__name__)

# Only the files each engine actually loads (keeps downloads lean).
PATTERNS = {
    "faster_whisper": ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"],
    "mlx": ["*.json", "*.npz", "*.safetensors", "*.tiktoken", "*.txt"],
    "transformers": ["*.json", "*.safetensors", "*.txt", "*.model", "vocab*", "merges*"],
}


def resolve(engine: str, model_id: str) -> tuple[str, str | None] | None:
    """(repo_id, single_filename or None) to fetch, or None when the model is a local path."""
    if not model_id or Path(model_id).expanduser().exists():
        return None
    entry = find_asr(engine, model_id)
    if engine == "whispercpp":
        from . import paths
        if (paths.models_dir() / model_id).is_file():
            return None
        return (entry.hf_repo if entry and entry.hf_repo else "ggerganov/whisper.cpp"), model_id
    if engine == "faster_whisper":
        repo = entry.hf_repo if entry and entry.hf_repo else model_id
        if "/" not in repo:
            repo = f"Systran/faster-whisper-{repo}"
        return repo, None
    if engine in ("mlx", "transformers") and "/" in model_id:
        return model_id, None
    return None


def _cache_root() -> Path:
    try:
        from huggingface_hub import constants
        return Path(constants.HF_HUB_CACHE)
    except Exception:
        return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def _repo_cache(repo: str) -> Path:
    return _cache_root() / ("models--" + repo.replace("/", "--"))


def _bytes_in(path: Path) -> int:
    total = 0
    blobs = path / "blobs"
    for p in (blobs.iterdir() if blobs.is_dir() else []):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _plan(repo: str, filename: str | None, engine: str) -> tuple[list[str], int]:
    """Files to fetch and their total size (bytes, 0 if unknown)."""
    from huggingface_hub import HfApi
    info = HfApi().model_info(repo, files_metadata=True)
    sibs = [(s.rfilename, getattr(s, "size", None) or 0) for s in (info.siblings or [])]
    if filename:
        chosen = [(n, sz) for n, sz in sibs if n == filename]
    else:
        pats = list(PATTERNS.get(engine, ["*"]))
        names = [n for n, _ in sibs]
        if engine == "transformers" and not any(n.endswith(".safetensors") for n in names):
            pats.append("*.bin")       # older repos only ship pytorch_model.bin
        chosen = [(n, sz) for n, sz in sibs if any(fnmatch.fnmatch(n, p) for p in pats)]
    return [n for n, _ in chosen], sum(sz for _, sz in chosen)


def is_cached(repo: str, filename: str | None, engine: str) -> bool:
    try:
        from huggingface_hub import hf_hub_download, snapshot_download
        if filename:
            hf_hub_download(repo, filename, local_files_only=True)
        else:
            snapshot_download(repo, allow_patterns=PATTERNS.get(engine), local_files_only=True)
            # snapshot_download "succeeds" on an empty snapshot dir; require real weight files.
            snap = _repo_cache(repo) / "snapshots"
            if not any(p.suffix in (".bin", ".npz", ".safetensors") for p in snap.rglob("*")):
                return False
        return True
    except Exception:
        return False


def needs_download(engine: str, model_id: str) -> bool:
    """True when this speech model still has to be downloaded before it can run."""
    target = resolve(engine, model_id)
    return target is not None and not is_cached(*target, engine)


def missing_for(settings) -> list[tuple[str, float | None]]:
    """Speech models the next run needs that aren't on disk yet: [(model id, size in GB or None)].
    Includes large-v3 when Whisper's own translation needs it as a stand-in."""
    from .models import can_whisper_translate, whisper_translate_model
    eng = settings.engine
    ids = [settings.asr_model]
    if settings.translate and settings.translator == "whisper" and not can_whisper_translate(eng, settings.asr_model):
        alt = whisper_translate_model(eng)
        if alt and alt not in ids:
            ids.append(alt)
    out = []
    for mid in ids:
        if mid and needs_download(eng, mid):
            m = find_asr(eng, mid)
            out.append((mid, m.size_gb if m else None))
    return out


def describe(missing: list[tuple[str, float | None]]) -> str:
    """'kotoba-whisper-v2.0-faster (~1.5 GB)' style list, one per line."""
    return "\n".join(f"• {mid.split('/')[-1]}" + (f" (~{gb:.1f} GB)" if gb else "") for mid, gb in missing)


Progress = Callable[[float, str], None]


def ensure_model(engine: str, model_id: str, progress: Progress, cancelled: Callable[[], bool]) -> None:
    """Download the model if it isn't cached yet, reporting (fraction, message)."""
    target = resolve(engine, model_id)
    if target is None:
        return
    repo, filename = target
    if is_cached(repo, filename, engine):
        return
    from huggingface_hub import constants, hf_hub_download, snapshot_download

    from . import netcheck
    short = repo.split("/")[-1]
    progress(0.0, _t("Connecting to Hugging Face to download {model}…").format(model=short))
    log.info("%s isn't on this computer yet — checking the connection to %s…", short, constants.ENDPOINT)
    ok, detail = netcheck.check(constants.ENDPOINT)
    if not ok:
        raise RuntimeError(f"Can't download {short}: {detail}")
    log.info("Connection OK (%s).", detail)
    if cancelled():
        raise InterruptedError()

    progress(0.0, _t("Looking up {model} on Hugging Face…").format(model=short))
    plan: list = []
    def _list():
        try:
            plan.append(_plan(repo, filename, engine))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not list %s (%s); downloading without a size estimate.", repo, e)
    t = threading.Thread(target=_list, daemon=True)
    t.start()
    t.join(30)                  # the listing is one small request; don't let it hang the job
    if plan:
        files, total = plan[0]
    else:
        log.warning("Couldn't list %s within 30 s; downloading without a size estimate.", repo)
        files, total = [], 0
    mb_total = total / 1e6
    log.info("Downloading model %s (%s) — first use only…", repo,
             f"{mb_total:,.0f} MB" if total else "size unknown")
    progress(0.0, _t("Downloading model · {done} / {total} MB").format(done=0, total=f"{mb_total:,.0f}") if total
             else _t("Downloading model…"))

    cache = _repo_cache(repo)
    start_bytes = _bytes_in(cache)
    err: list[BaseException] = []

    def work():
        try:
            if filename:
                hf_hub_download(repo, filename)
            elif files:
                snapshot_download(repo, allow_patterns=files)
            else:
                snapshot_download(repo, allow_patterns=PATTERNS.get(engine))
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t0 = time.time()
    while t.is_alive():
        t.join(0.5)
        if cancelled():
            raise InterruptedError()
        done = max(0, _bytes_in(cache) - start_bytes)
        rate = done / max(0.5, time.time() - t0)
        if total:
            frac = min(0.99, done / total)
            eta = (total - done) / rate if rate > 0 else 0
            msg = _t("Downloading model · {done} / {total} MB").format(done=f"{done / 1e6:,.0f}", total=f"{mb_total:,.0f}")
            if rate > 0 and done > 5e6:
                msg += f" · {rate / 1e6:,.1f} MB/s · " + _t("~{time} left").format(time=_fmt(eta))
            progress(frac, msg)
        else:
            progress(0.0, _t("Downloading model · {done} MB").format(done=f"{done / 1e6:,.0f}"))
    if err:
        raise RuntimeError(f"Model download failed for {repo}: {err[0]}") from err[0]
    log.info("Model downloaded in %s.", _fmt(time.time() - t0))
    progress(1.0, _t("Model downloaded"))


def _fmt(sec: float) -> str:
    sec = int(max(0, sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
