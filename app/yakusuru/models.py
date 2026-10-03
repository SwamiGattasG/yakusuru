"""Curated model catalog. Every combo box in the UI is editable, so any other
Hugging Face repo / Ollama tag / API model id can be typed in as well."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AsrModel:
    id: str                 # what the engine loads
    label: str
    size_gb: float
    notes: str = ""
    can_translate: bool = True   # supports Whisper's built-in "translate to English" task
    hf_repo: str = ""            # repo to pre-download in the wizard (if different from id)
    anime: bool = False          # anime-whisper: no initial prompt, anti-repetition settings
    languages: tuple = ()        # languages the model was trained for; () = all of Whisper's ~100


ENGINES = {
    "faster_whisper": "faster-whisper (CUDA / CPU)",
    "mlx": "MLX Whisper (Apple Silicon)",
    "transformers": "Transformers (CUDA / ROCm / MPS / CPU)",
    "whispercpp": "whisper.cpp (Vulkan / Metal / CPU)",
}

ASR_MODELS: dict[str, list[AsrModel]] = {
    "faster_whisper": [
        AsrModel("large-v3", "Whisper large-v3 — best accuracy", 3.1,
                 "Best all-round accuracy in every language; slower.", hf_repo="Systran/faster-whisper-large-v3"),
        AsrModel("kotoba-tech/kotoba-whisper-v2.0-faster", "Kotoba-Whisper v2.0 — Japanese-tuned", 1.5,
                 "Distilled large-v3 trained on Japanese. ~6x faster; Japanese output only.",
                 can_translate=False, languages=("ja",)),
        AsrModel("large-v3-turbo", "Whisper large-v3-turbo — fast", 1.6,
                 "Fast and accurate in every language. Poor at Whisper's built-in translation.", can_translate=False,
                 hf_repo="mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
        AsrModel("medium", "Whisper medium — low VRAM", 1.5, hf_repo="Systran/faster-whisper-medium"),
        AsrModel("small", "Whisper small — very low-end", 0.5, hf_repo="Systran/faster-whisper-small"),
    ],
    "mlx": [
        AsrModel("mlx-community/whisper-large-v3-turbo", "Whisper large-v3-turbo — fast", 1.6,
                 "Best speed/quality balance on Apple Silicon, any language.", can_translate=False),
        AsrModel("mlx-community/whisper-large-v3-mlx", "Whisper large-v3 — best accuracy", 3.1),
        AsrModel("mlx-community/whisper-medium-mlx", "Whisper medium", 1.5),
    ],
    "transformers": [
        AsrModel("litagin/anime-whisper", "Anime-Whisper — anime / game dialogue", 1.5,
                 "Fine-tuned on ~5,300 h of anime speech. Great for emotive dialogue; "
                 "omits final periods; Japanese output only.", can_translate=False, anime=True, languages=("ja",)),
        AsrModel("kotoba-tech/kotoba-whisper-v2.0", "Kotoba-Whisper v2.0 — Japanese-tuned", 1.5,
                 can_translate=False, languages=("ja",)),
        AsrModel("openai/whisper-large-v3", "Whisper large-v3", 3.1),
        AsrModel("openai/whisper-large-v3-turbo", "Whisper large-v3-turbo", 1.6, can_translate=False),
    ],
    "whispercpp": [
        AsrModel("ggml-large-v3-turbo.bin", "Whisper large-v3-turbo (ggml)", 1.6, can_translate=False,
                 hf_repo="ggerganov/whisper.cpp"),
        AsrModel("ggml-large-v3.bin", "Whisper large-v3 (ggml)", 3.1, hf_repo="ggerganov/whisper.cpp"),
        AsrModel("ggml-kotoba-whisper-v2.0.bin", "Kotoba-Whisper v2.0 (ggml)", 1.5, can_translate=False, languages=("ja",),
                 hf_repo="kotoba-tech/kotoba-whisper-v2.0-ggml"),
        AsrModel("ggml-medium.bin", "Whisper medium (ggml)", 1.5, hf_repo="ggerganov/whisper.cpp"),
    ],
}


def find_asr(engine: str, model_id: str) -> AsrModel | None:
    for m in ASR_MODELS.get(engine, []):
        if m.id == model_id:
            return m
    return None


def can_whisper_translate(engine: str, model_id: str) -> bool:
    """Was this model trained for Whisper's built-in translate-to-English task?"""
    m = find_asr(engine, model_id)
    if m is not None:
        return m.can_translate
    low = model_id.lower()
    return not ("turbo" in low or "kotoba" in low or "anime" in low or "distil" in low)


def whisper_translate_model(engine: str) -> str | None:
    """The model to use for Whisper's translate pass when the chosen one can't do it (large-v3)."""
    for m in ASR_MODELS.get(engine, []):
        if m.can_translate and "large-v3" in m.id and "turbo" not in m.id:
            return m.id
    return None


def is_anime_model(model_id: str) -> bool:
    return "anime-whisper" in model_id.lower()


TRANSLATORS = {
    "ollama": "Local LLM (Ollama)",
    "anthropic": "Anthropic Claude",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "xai": "xAI Grok",
    "deepl": "DeepL",
    "openai_compatible": "OpenAI-compatible server (LM Studio, vLLM…)",
    "whisper": "Whisper built-in (direct to English, fastest)",
}

# Suggestions only — the UI also lists what each provider reports live.
TRANSLATOR_MODELS = {
    "ollama": [
        ("qwen3:14b", "Qwen3 14B — strong Japanese (≈10 GB)"),
        ("qwen3:8b", "Qwen3 8B — good on 16 GB machines (≈6 GB)"),
        ("qwen3:4b", "Qwen3 4B — light, for 8–16 GB machines (≈3 GB)"),
        ("gemma3:12b", "Gemma 3 12B — natural English (≈8 GB)"),
        ("gemma3:27b", "Gemma 3 27B — best local quality (≈17 GB)"),
        ("qwen3:30b", "Qwen3 30B-A3B MoE — fast & strong (≈19 GB)"),
    ],
    "anthropic": [
        ("claude-sonnet-5-5", "Claude Sonnet 5.5 — recommended"),
        ("claude-opus-5-5", "Claude Opus 5.5 — highest quality"),
        ("claude-haiku-4-5", "Claude Haiku 4.5 — cheapest"),
    ],
    "openai": [
        ("gpt-5.4-mini", "GPT-5.4 mini"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.4-nano", "GPT-5.4 nano — cheapest"),
    ],
    "gemini": [
        ("gemini-3.5-flash", "Gemini 3.5 Flash"),
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro (preview)"),
        ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ],
    "xai": [
        ("grok-4.7", "Grok 4.7 — latest"),
        ("grok-4.3", "Grok 4.3 — cheaper"),
    ],
    "deepl": [
        ("quality_optimized", "Quality optimized (next-gen model)"),
        ("prefer_quality_optimized", "Prefer quality optimized"),
        ("latency_optimized", "Latency optimized (classic)"),
    ],
    "openai_compatible": [],
    "whisper": [],
}

API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "deepl": "DEEPL_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}

CONTENT_TYPES = {
    "anime": "Anime / drama",
    "film": "Film",
    "vlog": "YouTube / stream / vlog",
    "lecture": "Lecture / interview",
}


# Approximate memory each local Ollama model needs while running (GB), for low-RAM warnings.
OLLAMA_MODEL_GB = {"qwen3:4b": 3.0, "qwen3:8b": 6.0, "qwen3:14b": 10.0, "gemma3:4b": 3.5, "gemma3:12b": 8.5,
                   "gemma3:27b": 17.0, "qwen3:30b": 19.0}

# Translators that run in the cloud (need an API key, use no local memory).
CLOUD_TRANSLATORS = ("anthropic", "openai", "gemini", "xai", "deepl")


def recommended_ollama_model(ram_gb: float | None) -> str:
    if ram_gb and ram_gb <= 8:
        return "qwen3:4b"
    if ram_gb and ram_gb <= 18:
        return "qwen3:8b"
    return "qwen3:14b"
