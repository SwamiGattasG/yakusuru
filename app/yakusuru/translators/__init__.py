"""Translators: turn a list of subtitle lines in the source language into the target language."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

from ..glossary import Term, relevant, to_prompt
from . import prompts

log = logging.getLogger(__name__)

Progress = Callable[[float], None]
Cancelled = Callable[[], bool]


class TranslationError(RuntimeError):
    pass


class Translator:
    key = "base"
    needs_api_key = False

    def __init__(self, settings, model: str = ""):
        self.s = settings
        self.model = model or settings.model_for(self.key)
        # The pipeline replaces src_lang with the detected language when the source is "auto".
        self.src_lang = getattr(settings, "source_lang", "ja")
        self.tgt_lang = getattr(settings, "target_lang", "en")

    # -- public ------------------------------------------------------------
    def translate(self, lines: list[str], glossary: list[Term], progress: Progress = lambda f: None,
                  cancelled: Cancelled = lambda: False, notes: str = "") -> list[str]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []

    def prepare(self, status: Callable[[str], None] = lambda m: None,
                cancelled: Cancelled = lambda: False) -> None:
        """Make the backend ready before a job (start services, fetch models). Default: nothing."""

    def finish(self) -> None:
        """Called after a file is translated (e.g. to free a local model's memory). Default: nothing."""

    def test(self) -> tuple[str, str]:
        """Translate one short line; returns (sample, translation) or raises."""
        sample = TEST_SAMPLES.get((self.src_lang or "").split("-")[0], TEST_SAMPLES["en"])
        if self.src_lang in ("auto", None):
            self.src_lang = "en"
        return sample, self.translate([sample], [])[0]


TEST_SAMPLES = {
    "ja": "こんにちは、元気ですか？", "en": "Hello, how are you today?", "zh": "你好，你今天好吗？",
    "ko": "안녕하세요, 오늘 어떻게 지내세요?", "es": "Hola, ¿cómo estás hoy?", "fr": "Bonjour, comment allez-vous ?",
    "de": "Hallo, wie geht es dir heute?", "pt": "Olá, como você está hoje?", "it": "Ciao, come stai oggi?",
    "ru": "Привет, как дела сегодня?",
}


class LLMTranslator(Translator):
    """Batching, context windows, JSON parsing and repair shared by every LLM backend."""

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def translate(self, lines, glossary, progress=lambda f: None, cancelled=lambda: False, notes=""):
        n = len(lines)
        result: list[str | None] = [None] * n
        system = prompts.system_prompt(self.s.content_type, self.s.honorifics, self.src_lang, self.tgt_lang)
        bs = max(1, int(self.s.batch_size))
        ctx_n = max(0, int(self.s.context_lines))
        i = 0
        while i < n:
            if cancelled():
                raise InterruptedError()
            idx = [k for k in range(i, min(n, i + bs)) if lines[k].strip()]
            for k in range(i, min(n, i + bs)):
                if not lines[k].strip():
                    result[k] = ""
            if idx:
                self._translate_indices(idx, lines, result, glossary, system, ctx_n, notes, cancelled)
            i += bs
            progress(min(1.0, i / n))
        return [r if r is not None else "" for r in result]

    def _translate_indices(self, idx, lines, result, glossary, system, ctx_n, notes, cancelled, depth=0):
        first, last = idx[0], idx[-1]
        before = [(lines[k], result[k]) for k in range(max(0, first - ctx_n), first)
                  if result[k] is not None and lines[k].strip()]
        after = [lines[k] for k in range(last + 1, min(len(lines), last + 4))]
        batch = [(k + 1, lines[k]) for k in idx]
        terms = relevant(glossary, [lines[k] for k in idx])
        user = prompts.user_prompt(batch, before, after, to_prompt(terms), notes)
        try:
            raw = self.complete(system, user)
            got = parse_response(raw)
        except InterruptedError:
            raise
        except TranslationError as e:
            status = getattr(e, "status", None)
            if status is None or status in (401, 403, 404):
                raise  # network down, bad key or unknown model: stop the job with a clear message
            log.warning("Batch %d-%d failed: %s", first + 1, last + 1, e)
            got = {}
        except Exception as e:
            log.warning("Batch %d-%d failed: %s", first + 1, last + 1, e)
            got = {}
        missing = []
        for k in idx:
            v = got.get(k + 1)
            if v is None:
                missing.append(k)
            else:
                result[k] = v.strip()
        if not missing:
            return
        if cancelled():
            raise InterruptedError()
        if depth >= 3:
            for k in missing:
                log.warning("Line %d could not be translated; keeping the Japanese text.", k + 1)
                result[k] = "[?] " + lines[k]
            return
        log.info("Retrying %d line(s) the model skipped…", len(missing))
        if len(missing) > 1:
            half = len(missing) // 2
            parts = [missing[:half], missing[half:]]
        else:
            parts = [missing]
        for part in parts:
            self._translate_indices(part, lines, result, glossary, system, ctx_n, notes, cancelled, depth + 1)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_response(raw: str) -> dict[int, str]:
    """Extract {id: english} from an LLM reply. Tolerates fences, chatter and minor shape drift."""
    text = _THINK.sub("", raw or "").strip()
    text = _FENCE.sub("", text).strip()
    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    out: dict[int, str] = {}
    if data is not None:
        items = None
        if isinstance(data, dict):
            for key in ("t", "translations", "lines", "result", "items", "output"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            if items is None and all(str(k).isdigit() for k in data):   # {"1": "...", "2": "..."}
                return {int(k): str(v) for k, v in data.items() if isinstance(v, str)}
        elif isinstance(data, list):
            items = data
        for it in items or []:
            if not isinstance(it, dict):
                continue
            i = it.get("i", it.get("id"))
            en = it.get("out", it.get("translation", it.get("tr", it.get("en", it.get("text")))))
            try:
                if i is not None and isinstance(en, str):
                    out[int(i)] = en
            except (TypeError, ValueError):
                continue
        if out:
            return out
    # Fallback: "1: text" / "[1] text" lines
    for line in text.splitlines():
        m = re.match(r"\s*\[?(\d+)\]?[:.)\-]\s*(.+)", line)
        if m:
            out[int(m.group(1))] = m.group(2).strip().strip('"')
    return out


# --------------------------------------------------------------------------- HTTP helper
def http_json(method: str, url: str, *, headers=None, body=None, timeout=180, retries=4):
    import requests
    delay = 2.0
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            last = e
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise TranslationError(f"Network error contacting {url.split('/')[2]}: {e}") from e
        if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < retries:
            ra = r.headers.get("retry-after")
            time.sleep(float(ra) if ra and ra.replace(".", "").isdigit() else delay)
            delay *= 2
            continue
        if r.status_code >= 400:
            msg = r.text[:600]
            try:
                j = r.json()
                msg = (j.get("error", {}) or {}).get("message") if isinstance(j.get("error"), dict) else \
                    (j.get("error") or j.get("message") or msg)
            except Exception:
                pass
            err = TranslationError(f"HTTP {r.status_code}: {msg}")
            err.status = r.status_code  # type: ignore[attr-defined]
            err.body = r.text  # type: ignore[attr-defined]
            raise err
        try:
            return r.json()
        except ValueError as e:
            raise TranslationError(f"Invalid JSON from server: {r.text[:200]}") from e
    raise TranslationError(str(last))


def get_translator(key: str, settings, model: str = "") -> Translator:
    from . import providers
    cls = {
        "ollama": providers.OllamaTranslator,
        "anthropic": providers.AnthropicTranslator,
        "openai": providers.OpenAITranslator,
        "openai_compatible": providers.OpenAICompatibleTranslator,
        "gemini": providers.GeminiTranslator,
        "xai": providers.XAITranslator,
        "deepl": providers.DeepLTranslator,
        "echo": providers.EchoTranslator,
    }.get(key)
    if cls is None:
        raise ValueError(f"No translator for '{key}'")
    return cls(settings, model)
