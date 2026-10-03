"""Download helpers run as a subprocess by the setup wizard (so output streams into its log).

    python -m yakusuru.tools.fetch hf <repo_id>
    python -m yakusuru.tools.fetch hf-file <repo_id> <filename> <dest_dir>
    python -m yakusuru.tools.fetch asr <engine> <model_id>
"""
from __future__ import annotations

import os
import sys
import time


def _hf_snapshot(repo: str) -> str:
    from huggingface_hub import snapshot_download
    print(f"Downloading {repo} from Hugging Face…", flush=True)
    t = time.time()
    p = snapshot_download(repo, allow_patterns=["*.json", "*.txt", "*.bin", "*.safetensors", "*.model",
                                                "*.npz", "*.tiktoken", "*.py", "vocab*", "*merges*",
                                                "*.msgpack"],
                          ignore_patterns=["*.onnx", "*.h5", "*.ot", "*flax*", "*tf_model*", "*.pt",
                                           "*.ckpt"])
    print(f"✓ {repo} ready ({time.time() - t:.0f}s) → {p}", flush=True)
    return p


def _hf_file(repo: str, filename: str, dest: str) -> str:
    from huggingface_hub import hf_hub_download
    print(f"Downloading {filename} from {repo}…", flush=True)
    p = hf_hub_download(repo, filename, local_dir=dest)
    print(f"✓ saved {p}", flush=True)
    return p


def fetch_asr(engine: str, model_id: str) -> None:
    from yakusuru import paths
    from yakusuru.models import find_asr
    entry = find_asr(engine, model_id)
    if engine == "whispercpp":
        repo = entry.hf_repo if entry and entry.hf_repo else "ggerganov/whisper.cpp"
        _hf_file(repo, model_id, str(paths.models_dir()))
        return
    if engine == "faster_whisper":
        repo = entry.hf_repo if entry and entry.hf_repo else model_id
        if "/" not in repo:
            repo = f"Systran/faster-whisper-{repo}"
        _hf_snapshot(repo)
        return
    _hf_snapshot(model_id)


def main(argv: list[str]) -> int:
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
    if len(argv) < 2:
        print(__doc__)
        return 2
    try:
        if argv[0] == "hf":
            _hf_snapshot(argv[1])
        elif argv[0] == "hf-file":
            _hf_file(argv[1], argv[2], argv[3])
        elif argv[0] == "asr":
            fetch_asr(argv[1], argv[2])
        else:
            print(__doc__)
            return 2
    except Exception as e:
        print(f"✗ Download failed: {type(e).__name__}: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
