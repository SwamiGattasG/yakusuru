"""Furigana (kana readings over kanji) for Japanese subtitles.

Readings come from SudachiPy + its core dictionary (a morphological analyzer, so 今日 is きょう and
一緒 is いっしょ in context). Two renderings:

  * inline  — 漢字（かんじ） in a normal .srt: works in every player.
  * ruby    — <ruby>漢字<rt>かんじ</rt></ruby> in a WebVTT file: real furigana above the kanji in
              browsers (HTML5 video) and players that support WebVTT ruby; others show plain text.

Only kanji get readings; okurigana stays outside them (取り扱い → 取(と)り扱(あつか)い)."""
from __future__ import annotations

import importlib.util
import re
from functools import lru_cache

PACKAGES = ("sudachipy", "sudachidict_core")
PIP_ARGS = ["sudachipy", "sudachidict_core"]

_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿々〆ヶ]")
_KANA = re.compile(r"[぀-ヿー]")


def available() -> bool:
    return all(importlib.util.find_spec(p) is not None for p in PACKAGES)


def kata_to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


@lru_cache(maxsize=1)
def _tokenizer():
    from sudachipy import Dictionary, SplitMode
    d = Dictionary(dict="core")
    tok = d.tokenizer() if hasattr(d, "tokenizer") else d.create()
    return tok, SplitMode.A          # short units: readings line up with the kanji they belong to


# The dictionary gives the formal / written reading for a few words that speech almost always
# reads differently (私 → わたくし, 明日 → あす). Subtitles are speech, so prefer the spoken one.
SPOKEN = {"私": "わたし", "明日": "あした", "昨日": "きのう", "一昨日": "おととい", "明後日": "あさって",
          "今朝": "けさ", "一人": "ひとり", "二人": "ふたり", "大人": "おとな", "貴方": "あなた"}


def _align(surface: str, reading: str) -> list[tuple[str, str | None]]:
    """Split one word into (text, reading-or-None) pieces, keeping kana outside the readings."""
    # Runs of kanji vs. everything else, e.g. 取り扱い → [取][り][扱][い]
    runs = re.findall(r"[㐀-䶿一-鿿豈-﫿々〆ヶ]+|[^㐀-䶿一-鿿豈-﫿々〆ヶ]+",
                      surface)
    if len(runs) == 1:
        return [(surface, reading)]
    pattern = "".join("(.+?)" if _KANJI.match(r) else re.escape(kata_to_hira(r)) for r in runs)
    m = re.fullmatch(pattern, reading)
    if not m:
        return [(surface, reading)]          # irregular reading: annotate the whole word
    out, g = [], iter(m.groups())
    for r in runs:
        out.append((r, next(g)) if _KANJI.match(r) else (r, None))
    return out


def segments(text: str) -> list[tuple[str, str | None]]:
    """[(text, reading or None)] covering `text` exactly."""
    if not text or not _KANJI.search(text):
        return [(text, None)] if text else []
    tok, mode = _tokenizer()
    out: list[tuple[str, str | None]] = []
    for m in tok.tokenize(text, mode):
        surf = m.surface()
        if not _KANJI.search(surf):
            out.append((surf, None))
            continue
        reading = SPOKEN.get(surf) or kata_to_hira(m.reading_form() or "")
        if not reading or reading == surf or not _KANA.search(reading) or reading == "きごう":
            out.append((surf, None))
            continue
        out.extend(_align(surf, reading))
    return out


def inline(text: str, open_: str = "（", close: str = "）") -> str:
    """漢字（かんじ）, line by line (keeps the subtitle's line breaks)."""
    return "\n".join("".join(f"{t}{open_}{r}{close}" if r else t for t, r in segments(line))
                     for line in text.split("\n"))


def ruby(text: str) -> str:
    """WebVTT/HTML ruby markup."""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "\n".join("".join(f"<ruby>{esc(t)}<rt>{esc(r)}</rt></ruby>" if r else esc(t) for t, r in segments(line))
                     for line in text.split("\n"))
