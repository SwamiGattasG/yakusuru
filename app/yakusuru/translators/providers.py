"""Concrete translation backends."""
from __future__ import annotations

import hashlib
import logging

from .. import keystore
from ..glossary import Term
from . import LLMTranslator, TranslationError, Translator, http_json

log = logging.getLogger(__name__)


def _need_key(provider: str) -> str:
    key = keystore.get_key(provider)
    if not key:
        raise TranslationError(f"No API key set for {provider}. Add it in Settings → API keys.")
    return key


# ----------------------------------------------------------------------------- Ollama
class OllamaTranslator(LLMTranslator):
    key = "ollama"

    def __init__(self, settings, model=""):
        super().__init__(settings, model)
        self.base = settings.ollama_url.rstrip("/")
        self._think_ok = True

    def complete(self, system, user):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.3, "num_ctx": self._num_ctx()},
            "keep_alive": "5m",
        }
        if self._think_ok:
            body["think"] = False  # Qwen3 etc.: skip the reasoning trace, much faster
        try:
            r = http_json("POST", f"{self.base}/api/chat", body=body, timeout=600)
        except TranslationError as e:
            if self._think_ok and "think" in str(e).lower():
                self._think_ok = False
                return self.complete(system, user)
            if getattr(e, "status", None) == 404:
                raise TranslationError(f"Ollama model '{self.model}' is not installed. "
                                       f"Pull it in the setup wizard or run: ollama pull {self.model}") from e
            if getattr(e, "status", None) is None:
                raise TranslationError("Cannot reach Ollama at " + self.base +
                                       ". Is the Ollama app running?") from e
            raise
        return (r.get("message") or {}).get("content", "")

    @staticmethod
    def _ram_gb() -> float:
        from ..hardware import _ram_gb
        return _ram_gb() or 16.0

    def _num_ctx(self) -> int:
        # The context window's KV cache costs memory; 4096 tokens fits a 25-line batch with context.
        return 4096 if self._ram_gb() <= 18 else 8192

    def finish(self):
        """On machines with ≤ 18 GB, unload the model right away so the Mac stays responsive
        (it reloads from disk in a few seconds for the next file)."""
        if self._ram_gb() > 18:
            return
        try:
            http_json("POST", f"{self.base}/api/generate", body={"model": self.model, "keep_alive": 0},
                      timeout=15, retries=0)
            log.info("Unloaded %s from Ollama to free memory.", self.model)
        except Exception as e:
            log.debug("Ollama unload failed: %s", e)

    def list_models(self):
        try:
            r = http_json("GET", f"{self.base}/api/tags", timeout=5, retries=0)
            return sorted(m["name"] for m in r.get("models", []))
        except Exception:
            return []

    # -- readiness -----------------------------------------------------------------
    def _alive(self) -> bool:
        try:
            import requests
            return requests.get(f"{self.base}/api/version", timeout=2).ok
        except Exception:
            return False

    def prepare(self, status=lambda m: None, cancelled=lambda: False):
        """Start Ollama if it's installed but not running, and pull the model if it's missing."""
        import time
        from ..deps import check_ollama
        if not self._alive():
            info = check_ollama(self.base)
            local = self.base.startswith(("http://localhost", "http://127.0.0.1"))
            if not local:
                raise TranslationError(f"Cannot reach the Ollama server at {self.base}.")
            if not info["binary"] and not _ollama_app():
                raise TranslationError("Ollama isn't installed. Click Start again and choose “Install "
                                       "Ollama”, or pick a different translator.")
            status("Starting Ollama…")
            log.info("Ollama is not running — starting it.")
            _start_ollama(info["binary"])
            for _ in range(60):
                if cancelled():
                    raise InterruptedError()
                if self._alive():
                    break
                time.sleep(0.5)
            else:
                raise TranslationError("Ollama is installed but didn't start. Open the Ollama app, then "
                                       "use Run Again.")
        installed = set(self.list_models())
        if self.model in installed or f"{self.model}:latest" in installed:
            return
        self._pull(status, cancelled)

    def _pull(self, status, cancelled):
        import json as _json
        import requests
        log.info("Ollama model %s is not downloaded yet — pulling it (first use only).", self.model)
        status(f"Downloading {self.model}…")
        with requests.post(f"{self.base}/api/pull", json={"model": self.model, "stream": True},
                           stream=True, timeout=(10, 3600)) as r:
            if r.status_code >= 400:
                raise TranslationError(f"Could not download '{self.model}' in Ollama: {r.text[:200]}")
            for line in r.iter_lines():
                if cancelled():
                    raise InterruptedError()
                if not line:
                    continue
                ev = _json.loads(line)
                if ev.get("error"):
                    raise TranslationError(f"Ollama could not download '{self.model}': {ev['error']}")
                tot, comp = ev.get("total"), ev.get("completed")
                if tot and comp:
                    status(f"Downloading {self.model} · {comp / 1e9:.1f} / {tot / 1e9:.1f} GB "
                           f"({comp * 100 // tot}%)")
                elif ev.get("status"):
                    status(f"{self.model}: {ev['status']}")
        log.info("Ollama model %s is ready.", self.model)


def _ollama_app() -> bool:
    from ..ollama_install import mac_app
    return mac_app() is not None


def _start_ollama(binary: str = "") -> None:
    from ..ollama_install import start
    start()


# ----------------------------------------------------------------------------- Anthropic
class AnthropicTranslator(LLMTranslator):
    key = "anthropic"
    needs_api_key = True
    URL = "https://api.anthropic.com/v1"

    def _headers(self):
        return {"x-api-key": _need_key("anthropic"), "anthropic-version": "2023-06-01",
                "content-type": "application/json"}

    def complete(self, system, user):
        body = {"model": self.model, "max_tokens": 8000, "system": system,
                "messages": [{"role": "user", "content": user}]}
        r = http_json("POST", f"{self.URL}/messages", headers=self._headers(), body=body)
        return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")

    def list_models(self):
        try:
            r = http_json("GET", f"{self.URL}/models?limit=100", headers=self._headers(), timeout=10, retries=0)
            return [m["id"] for m in r.get("data", [])]
        except Exception:
            return []


# ----------------------------------------------------------------------------- OpenAI
class OpenAITranslator(LLMTranslator):
    key = "openai"
    needs_api_key = True

    def __init__(self, settings, model=""):
        super().__init__(settings, model)
        self.base = "https://api.openai.com/v1"
        self._extra = {"response_format": {"type": "json_object"}, "reasoning_effort": "low"}

    def _headers(self):
        return {"Authorization": f"Bearer {_need_key(self.key)}", "Content-Type": "application/json"}

    def complete(self, system, user):
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        body.update(self._extra)
        try:
            r = http_json("POST", f"{self.base}/chat/completions", headers=self._headers(), body=body)
        except TranslationError as e:
            # Older / non-reasoning models reject some parameters: drop them and retry once.
            msg = str(e).lower()
            dropped = False
            for p in list(self._extra):
                if p in msg or (p == "response_format" and "json" in msg):
                    self._extra.pop(p)
                    dropped = True
            if dropped and getattr(e, "status", 0) == 400:
                return self.complete(system, user)
            raise
        return r["choices"][0]["message"].get("content") or ""

    def list_models(self):
        try:
            r = http_json("GET", f"{self.base}/models", headers=self._headers(), timeout=10, retries=0)
            ids = [m["id"] for m in r.get("data", [])]
            return sorted((i for i in ids if i.startswith(("gpt", "o")) and "audio" not in i
                           and "realtime" not in i and "image" not in i and "tts" not in i
                           and "transcribe" not in i), reverse=True)
        except Exception:
            return []


class OpenAICompatibleTranslator(OpenAITranslator):
    key = "openai_compatible"
    needs_api_key = False

    def __init__(self, settings, model=""):
        super().__init__(settings, model)
        self.base = settings.openai_compatible_url.rstrip("/")
        self._extra = {"temperature": 0.3}

    def _headers(self):
        key = keystore.get_key(self.key) or "not-needed"
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def list_models(self):
        try:
            r = http_json("GET", f"{self.base}/models", headers=self._headers(), timeout=5, retries=0)
            return [m["id"] for m in r.get("data", [])]
        except Exception:
            return []


class XAITranslator(OpenAITranslator):
    """xAI Grok — OpenAI-compatible chat completions API."""
    key = "xai"
    needs_api_key = True

    def __init__(self, settings, model=""):
        super().__init__(settings, model)
        self.base = "https://api.x.ai/v1"
        self._extra = {"response_format": {"type": "json_object"}}

    def list_models(self):
        try:
            r = http_json("GET", f"{self.base}/models", headers=self._headers(), timeout=10, retries=0)
            ids = [m["id"] for m in r.get("data", [])]
            return sorted((i for i in ids if i.startswith("grok") and "image" not in i and "vision" not in i),
                          reverse=True)
        except Exception:
            return []


# ----------------------------------------------------------------------------- Gemini
class GeminiTranslator(LLMTranslator):
    key = "gemini"
    needs_api_key = True
    URL = "https://generativelanguage.googleapis.com/v1beta"

    def _headers(self):
        return {"x-goog-api-key": _need_key("gemini"), "Content-Type": "application/json"}

    def complete(self, system, user):
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        model = self.model if self.model.startswith("models/") else f"models/{self.model}"
        r = http_json("POST", f"{self.URL}/{model}:generateContent", headers=self._headers(), body=body)
        cands = r.get("candidates") or []
        if not cands:
            fb = r.get("promptFeedback", {})
            raise TranslationError(f"Gemini returned no candidates ({fb.get('blockReason', 'unknown')}).")
        parts = (cands[0].get("content") or {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    def list_models(self):
        try:
            r = http_json("GET", f"{self.URL}/models?pageSize=200", headers=self._headers(), timeout=10, retries=0)
            return [m["name"].split("/", 1)[1] for m in r.get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                    and "gemini" in m["name"]]
        except Exception:
            return []


# ----------------------------------------------------------------------------- DeepL
class DeepLTranslator(Translator):
    key = "deepl"
    needs_api_key = True
    BATCH = 40

    def _base(self, key):
        return "https://api-free.deepl.com/v2" if key.endswith(":fx") else "https://api.deepl.com/v2"

    def _headers(self, key):
        return {"Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/json"}

    def _glossary_id(self, key, terms: list[Term]) -> str | None:
        if not terms:
            return None
        entries = "\n".join(f"{t.source}\t{t.target}" for t in terms
                            if t.source and t.target and "\t" not in t.source + t.target)
        if not entries:
            return None
        src, tgt = self._codes()
        name = f"yakusuru-{src}-{tgt}-" + hashlib.sha1(entries.encode()).hexdigest()[:12]
        base, h = self._base(key), self._headers(key)
        try:
            existing = http_json("GET", f"{base}/glossaries", headers=h, timeout=15, retries=1)
            for g in existing.get("glossaries", []):
                if g.get("name") == name:
                    return g["glossary_id"]
            g = http_json("POST", f"{base}/glossaries", headers=h, timeout=30, body={
                "name": name, "source_lang": src.lower(), "target_lang": tgt.split("-")[0].lower(),
                "entries": entries, "entries_format": "tsv"})
            return g.get("glossary_id")
        except Exception as e:
            log.warning("DeepL glossary could not be created (%s); continuing without it.", e)
            return None

    def _codes(self) -> tuple[str | None, str]:
        """DeepL source/target codes for the current pair (source None = DeepL auto-detects)."""
        from ..languages import get
        src = get(self.src_lang).deepl_src if self.src_lang not in ("auto", None, "und") else None
        if self.src_lang not in ("auto", None, "und") and not src:
            raise TranslationError(f"DeepL doesn't translate from {get(self.src_lang).name}. "
                                   "Choose an LLM translator for this language.")
        tgt = get(self.tgt_lang).deepl_tgt
        if not tgt:
            raise TranslationError(f"DeepL doesn't translate into {get(self.tgt_lang).name}. "
                                   "Choose an LLM translator for this language.")
        return src, tgt

    def translate(self, lines, glossary, progress=lambda f: None, cancelled=lambda: False, notes=""):
        key = _need_key("deepl")
        src_code, tgt_code = self._codes()
        base, h = self._base(key), self._headers(key)
        gid = self._glossary_id(key, glossary) if src_code else None   # glossaries need a fixed source
        out: list[str] = []
        model_type = self.model if self.model in ("quality_optimized", "prefer_quality_optimized",
                                                  "latency_optimized") else None
        formality = "prefer_less" if self.s.content_type in ("anime", "vlog") else "default"
        for i in range(0, len(lines), self.BATCH):
            if cancelled():
                raise InterruptedError()
            chunk = lines[i:i + self.BATCH]
            body = {"text": [l or " " for l in chunk], "source_lang": src_code, "target_lang": tgt_code,
                    "context": " ".join(lines[max(0, i - 8):i])[-1500:] or None,
                    "split_sentences": "nonewlines", "preserve_formatting": True}
            if model_type:
                body["model_type"] = model_type
            if gid:
                body["glossary_id"] = gid
            if formality != "default":
                body["formality"] = formality
            body = {k: v for k, v in body.items() if v is not None}
            try:
                r = http_json("POST", f"{base}/translate", headers=h, body=body)
            except TranslationError as e:
                if getattr(e, "status", 0) == 400 and ("model_type" in body or "formality" in body):
                    body.pop("model_type", None)
                    body.pop("formality", None)
                    r = http_json("POST", f"{base}/translate", headers=h, body=body)
                else:
                    raise
            out.extend(t["text"] if src.strip() else "" for t, src in zip(r.get("translations", []), chunk))
            progress(min(1.0, (i + len(chunk)) / max(1, len(lines))))
        return out

    def list_models(self):
        return ["quality_optimized", "prefer_quality_optimized", "latency_optimized"]


# ----------------------------------------------------------------------------- test helper
class EchoTranslator(Translator):
    """Offline translator used by tests: returns "[EN] <source>"."""
    key = "echo"

    def translate(self, lines, glossary, progress=lambda f: None, cancelled=lambda: False, notes=""):
        progress(1.0)
        return [f"[EN] {l}" if l.strip() else "" for l in lines]
