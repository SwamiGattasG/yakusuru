"""Prompt construction for LLM subtitle translation (any language pair).

Japanese → English keeps its specific guidance (dropped subjects, keigo, honorifics);
other pairs get the same structure with general rules."""
from __future__ import annotations

import json

from ..languages import get as get_lang

STYLE = {
    "anime": (
        "The source is anime or a TV drama. Keep each character's voice: casual speech stays casual, "
        "rough speech can be blunt, formal or polite speech stays formal. Keep the energy of exclamations, "
        "and keep catchphrases and attack names consistent. Do not over-explain jokes."
    ),
    "film": (
        "The source is a feature film. Aim for natural, cinematic dialogue that a professional subtitler "
        "would write. Keep register differences between characters, prefer brevity, and keep the tone "
        "(tension, irony, tenderness) intact."
    ),
    "vlog": (
        "The source is a YouTube video, livestream or vlog. Use relaxed, conversational language. Filler "
        "words can be dropped or lightly rendered. Internet slang becomes its natural equivalent in the "
        "target language. Keep chat/viewer references understandable."
    ),
    "lecture": (
        "The source is a lecture, presentation or interview. Prioritise accuracy and clarity over style. "
        "Keep technical terms precise and consistent; render polite speech as neutral, professional "
        "language. Remove disfluencies unless they change meaning."
    ),
}

# Extra guidance for particular source languages.
SOURCE_NOTES = {
    "ja": ("Japanese often omits subjects and objects — infer them from the surrounding lines. "
           "Render keigo as appropriately polite {tgt}, casual speech as casual. Fillers like えーと, なんか, "
           "まあ may be dropped; 草 / ｗｗｗ mean laughter."),
    "ko": ("Korean often omits subjects — infer them from context. Reflect speech levels (formal vs. banmal) "
           "through register in {tgt}."),
    "zh": "Chinese often omits subjects and tense markers — infer them from context.",
    "yue": "Cantonese is colloquial; render slang and particles naturally rather than literally.",
}

HONORIFICS = {
    "keep": ("Keep honorifics attached to names (Tanaka-san, Yuki-chan, Sensei, -senpai, -sama; Korean -ssi, "
             "-nim, oppa, unnie). Romanize names consistently, family-name order as spoken."),
    "localize": ("Localize honorifics into natural {tgt} forms of address (drop -san/-kun/-chan; use titles "
                 "only where the formality matters)."),
}

SYSTEM_TEMPLATE = """You are an expert {src}-to-{tgt} subtitle translator.

Translate each {src} subtitle line into natural, idiomatic {tgt} subtitles.

Rules:
1. Return exactly one {tgt} line per input id. Never merge, split, skip or reorder ids.
2. Lines are speech fragments cut by timing. If a sentence spans several ids, distribute the translation so each id carries the matching part and reads naturally on screen.
3. Use the surrounding context lines to resolve pronouns, omitted words and who is speaking.
4. Subtitles must be concise and readable at a glance.
5. Sound cues in brackets, e.g. (laughs) or (applause), become short {tgt} cues. Lines with ♪ are lyrics: translate them and keep the ♪.
6. If a line is unintelligible or only filler/noise, return a short faithful rendering rather than leaving it empty.
7. Follow the glossary exactly when a term appears.
{extra}
Style: {style}

Output ONLY a JSON object of the form:
{{"t": [{{"i": <id>, "out": "<{tgt} line>"}}, ...]}}
No commentary, no markdown fences."""


def system_prompt(content_type: str, honorifics: str, src_lang: str | None = "ja", tgt_lang: str = "en") -> str:
    src = get_lang(src_lang).name if src_lang and src_lang not in ("auto", "und") else "source-language"
    tgt = get_lang(tgt_lang).name
    extra = []
    base = (src_lang or "").split("-")[0]
    if base in SOURCE_NOTES:
        extra.append("8. " + SOURCE_NOTES[base].format(tgt=tgt))
    if base in ("ja", "ko"):
        extra.append(f"{len(extra) + 8}. " + HONORIFICS.get(honorifics, HONORIFICS["keep"]).format(tgt=tgt))
    return SYSTEM_TEMPLATE.format(src=src, tgt=tgt, style=STYLE.get(content_type, STYLE["anime"]),
                                  extra=("\n".join(extra) + "\n") if extra else "")


def user_prompt(batch: list[tuple[int, str]], before: list[tuple[str, str]], after: list[str],
                glossary_text: str, notes: str) -> str:
    parts = []
    if notes.strip():
        parts.append("About this video (from the user):\n" + notes.strip())
    if glossary_text:
        parts.append("Glossary (source → required translation):\n" + glossary_text)
    if before:
        ctx = "\n".join(f"Source: {s}\nTranslation: {t}" for s, t in before)
        parts.append("Previous lines (context only — already translated, do not output):\n" + ctx)
    payload = {"lines": [{"i": i, "text": t} for i, t in batch]}
    parts.append("Translate these lines:\n" + json.dumps(payload, ensure_ascii=False))
    if after:
        parts.append("Following lines (context only — do not translate):\n" + "\n".join(after))
    return "\n\n".join(parts)
