"""Subtitle data model, SRT I/O, re-segmentation, line wrapping and clean-up.

Language-aware: scripts written without spaces (Japanese, Chinese, Thai…) are wrapped
and joined by characters, everything else by words. A cue holds the original
transcript (`src`) and its translation (`tgt`)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .languages import get as get_lang
from .languages import uses_nospace_text


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Cue:
    start: float
    end: float
    src: str = ""        # transcript in the source language
    tgt: str = ""        # translation in the target language
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "src": self.src, "tgt": self.tgt}

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        # "ja"/"en" keys come from project files written before the app was multilingual.
        return cls(float(d["start"]), float(d["end"]), d.get("src", d.get("ja", "")), d.get("tgt", d.get("en", "")))


# --------------------------------------------------------------------------- time
def fmt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_TS = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def parse_ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"bad timestamp: {s!r}")
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


# --------------------------------------------------------------------------- text helpers
SENT_END = "。！？!?…♪.؟।"
SOFT_BREAK = "、,，」』）);:；：،"
_CJK = "぀-ヿ㐀-鿿가-힯฀-๿"


def nospace(lang: str | None, text: str = "") -> bool:
    """Is text in `lang` written without spaces? Falls back to looking at the text itself."""
    if lang and lang not in ("auto", "und"):
        return get_lang(lang).nospace
    return uses_nospace_text(text)


def joiner(lang: str | None, sample: str = "") -> str:
    return "" if nospace(lang, sample) else " "


def text_width(text: str) -> int:
    """Display width: full-width chars count 2."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def normalize_text(text: str, lang: str | None = None) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if nospace(lang, text):
        # Whisper sometimes inserts spaces between CJK characters; remove them.
        text = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", text)
    return text


def _wrap_words(text: str, width: int, max_lines: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    total = len(text)
    if total <= width:
        return [text]
    n_lines = max(2, min(max_lines, -(-total // width)))
    if total > width * max_lines:
        n_lines = max_lines  # too long: keep line count, let lines run longer
    target = total / n_lines
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        remaining_lines = n_lines - len(lines)
        if cur and len(cand) > max(target, 1) and remaining_lines > 1:
            # Prefer breaking where the line is closest to target length.
            if abs(len(cur) - target) <= abs(len(cand) - target):
                lines.append(cur)
                cur = w
                continue
        cur = cand
    if cur:
        lines.append(cur)
    return lines


def _wrap_chars(text: str, width_chars: int, max_lines: int) -> list[str]:
    if len(text) <= width_chars:
        return [text]
    n_lines = min(max_lines, -(-len(text) // width_chars))
    if n_lines <= 1:
        return [text]
    target = len(text) / n_lines
    # Candidate break positions: after punctuation, after Japanese particles, or anywhere.
    good = [i + 1 for i, c in enumerate(text[:-1]) if c in SENT_END + SOFT_BREAK + " "]
    okay = [i + 1 for i, c in enumerate(text[:-1]) if c in "はがをにでとものへやかね" and
            i + 1 < len(text) and not unicodedata.name(text[i + 1], "").startswith("HIRAGANA")]
    lines, start = [], 0
    for _ in range(1, n_lines):
        ideal = start + target
        best = None
        for pool, tol in ((good, 0.35), (okay, 0.3)):
            cands = [p for p in pool if start < p < len(text) and abs(p - ideal) <= target * tol]
            if cands:
                best = min(cands, key=lambda p: abs(p - ideal))
                break
        if best is None:
            best = int(round(ideal))
        lines.append(text[start:best].strip())
        start = best
    lines.append(text[start:].strip())
    return [l for l in lines if l]


def wrap(text: str, lang: str | None = None, max_chars: int = 42, max_chars_cjk: int = 22,
         max_lines: int = 2) -> str:
    text = text.strip()
    if not text:
        return ""
    ns = nospace(lang, text)
    wide = get_lang(lang).wide if lang and lang not in ("auto", "und") else ns
    lim = max_chars_cjk if wide else max_chars
    # Respect explicit line breaks the user/LLM gave if they already fit.
    if "\n" in text:
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) <= max_lines and all(len(p) <= lim for p in parts):
            return "\n".join(parts)
        text = ("" if ns else " ").join(parts)
    if ns:
        return "\n".join(_wrap_chars(text, lim, max_lines))
    return "\n".join(_wrap_words(text, lim, max_lines))


# --------------------------------------------------------------------------- hallucinations
# Phrases Whisper is known to invent on silence or music. Only whole, short cues are removed.
HALLUCINATION_PATTERNS = [
    # Japanese
    r"^ご視聴ありがとうございました[。！!]*$", r"^ご清聴ありがとうございました[。！!]*$",
    r"^(最後まで)?(ご覧|視聴)いただき(まして)?ありがとうございます?[。！!]*$",
    r"チャンネル登録", r"高評価", r"^字幕(は|：|:)", r"^(提供|翻訳)[:：]",
    r"^お疲れ様でした[。！!]*$", r"^おやすみなさい[。！!]*$", r"^\(?(音楽|拍手|笑)\)?$",
    # Chinese
    r"请不吝点赞", r"订阅.*(转发|打赏)", r"^字幕(由|製作|制作)", r"明镜与点点栏目",
    # Korean
    r"^시청(해 ?주셔서)? ?감사합니다[.!]*$", r"구독.*좋아요",
    # English
    r"^Thank you (so much )?for watching[.!]*$", r"^Please subscribe", r"(?i)^subtitles? by",
    r"(?i)^transcription by", r"(?i)amara\.org",
    # Spanish / Portuguese / French / German / Italian / Russian
    r"(?i)^subtítulos (realizados )?por", r"(?i)^legendas? (pela|por)", r"(?i)^sous-titr(es|age) (réalisés )?par",
    r"(?i)^untertitel (im auftrag|der|von)", r"(?i)^sottotitoli (creati |a cura )?(da|di)",
    r"(?i)^субтитры (сделал|создавал|подогнал)", r"(?i)^продолжение следует",
    # Any language
    r"^[♪～〜\s]+$",
]
_HALLU = [re.compile(p) for p in HALLUCINATION_PATTERNS]


def collapse_repeats(text: str) -> str:
    # Same char 6+ times → 4 (ああああああああ → ああああ)
    text = re.sub(r"(.)\1{5,}", lambda m: m.group(1) * 4, text)
    # Same phrase (2-15 chars) repeated 3+ times → once
    text = re.sub(r"(.{2,15}?)(?:[、,， ]?\1){2,}", r"\1", text)
    return text


def clean_cues(cues: list[Cue], filter_hallucinations: bool = True, lang: str | None = None,
               attr: str = "src") -> list[Cue]:
    out: list[Cue] = []
    for c in cues:
        t = collapse_repeats(normalize_text(getattr(c, attr), lang))
        if not t or not re.search(r"\w", t):
            continue
        if filter_hallucinations and len(t) <= 60 and any(p.search(t) for p in _HALLU):
            continue      # genuine dialogue containing these words is longer and survives
        # Identical text repeated back-to-back (classic Whisper loop) → merge
        if out and getattr(out[-1], attr) == t and c.start - out[-1].end < 1.0:
            out[-1].end = c.end
            continue
        setattr(c, attr, t)
        out.append(c)
    return out


# --------------------------------------------------------------------------- re-segmentation
def _split_by_words(c: Cue, max_s: float, max_chars: int) -> list[Cue]:
    words = c.words
    if (c.duration <= max_s and len(c.src) <= max_chars) or len(words) < 2:
        return [c]
    total = c.duration
    best_i, best_score = None, -1e9
    for i in range(1, len(words)):
        prev = words[i - 1].text.strip()
        gap = max(0.0, words[i].start - words[i - 1].end)
        balance = 1 - abs((words[i].start - c.start) / total - 0.5) * 2 if total > 0 else 0
        s = balance * 2 + gap * 3
        if prev and prev[-1] in SENT_END:
            s += 3
        elif prev and prev[-1] in SOFT_BREAK:
            s += 1.5
        if s > best_score:
            best_i, best_score = i, s
    left_w, right_w = words[:best_i], words[best_i:]
    # Whisper word tokens carry their own leading spaces, so plain concatenation is correct.
    left = Cue(c.start, left_w[-1].end, "".join(w.text for w in left_w).strip(), words=left_w)
    right = Cue(right_w[0].start, c.end, "".join(w.text for w in right_w).strip(), words=right_w)
    return _split_by_words(left, max_s, max_chars) + _split_by_words(right, max_s, max_chars)


_SENT_SPLIT = re.compile(r"(?<=[。！？!?؟।])|(?<=[.…])(?=\s)")
_SOFT_SPLIT = re.compile(r"(?<=[、，,;；:：،])")


def _split_by_text(c: Cue, max_s: float, max_chars: int) -> list[Cue]:
    if c.duration <= max_s and len(c.src) <= max_chars:
        return [c]
    pieces = [p for p in _SENT_SPLIT.split(c.src) if p and p.strip()]
    if len(pieces) < 2:
        pieces = [p for p in _SOFT_SPLIT.split(c.src) if p and p.strip()]
    if len(pieces) < 2:
        return [c]
    total = sum(len(p) for p in pieces)
    acc, k = 0, 0
    for k, p in enumerate(pieces):
        acc += len(p)
        if acc >= total / 2:
            break
    k = min(max(k + 1, 1), len(pieces) - 1)
    a, b = "".join(pieces[:k]), "".join(pieces[k:])
    t_split = c.start + c.duration * len(a) / max(1, total)
    left = Cue(c.start, t_split, a.strip())
    right = Cue(t_split, c.end, b.strip())
    return _split_by_text(left, max_s, max_chars) + _split_by_text(right, max_s, max_chars)


def resegment(cues: list[Cue], max_seconds: float = 7.0, max_chars: int = 44,
              min_seconds: float = 0.8, lang: str | None = None) -> list[Cue]:
    out: list[Cue] = []
    for c in cues:
        if c.words:
            out.extend(_split_by_words(c, max_seconds, max_chars))
        else:
            out.extend(_split_by_text(c, max_seconds, max_chars))
    sample = " ".join(c.src for c in cues[:20])
    sep = joiner(lang, sample)
    # merge very short fragments into a neighbour when it stays within limits
    merged: list[Cue] = []
    for c in out:
        if (merged and c.duration < min_seconds * 0.6 and c.start - merged[-1].end < 0.3
                and len(merged[-1].src) + len(c.src) <= max_chars
                and c.end - merged[-1].start <= max_seconds):
            m = merged[-1]
            m.end, m.src, m.words = c.end, (m.src + sep + c.src).strip(), m.words + c.words
            continue
        merged.append(c)
    return fix_timing(merged, min_seconds)


def fix_timing(cues: list[Cue], min_seconds: float = 0.8, gap: float = 0.05) -> list[Cue]:
    cues = sorted(cues, key=lambda c: c.start)
    for i, c in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else None
        if c.duration < min_seconds:
            want = c.start + min_seconds
            c.end = min(want, nxt - gap) if nxt is not None else want
        if nxt is not None and c.end > nxt - gap:
            c.end = max(c.start + 0.2, nxt - gap)
    return cues


def align_by_overlap(base: list[Cue], other: list[Cue], attr: str = "tgt") -> None:
    """Copy text from `other` cues onto `base` cues by time overlap (Whisper's direct translation)."""
    for o in other:
        best, best_ov = None, 0.0
        for i, b in enumerate(base):
            ov = min(b.end, o.end) - max(b.start, o.start)
            if ov > best_ov:
                best, best_ov = i, ov
        if best is not None:
            cur = getattr(base[best], attr)
            new = getattr(o, attr)
            setattr(base[best], attr, (cur + " " + new).strip() if cur else new)


# --------------------------------------------------------------------------- SRT I/O
RLM = "\u200f"          # right-to-left mark
_BIDI_MARKS = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"


def is_rtl(lang: str | None) -> bool:
    return bool(lang) and lang not in ("auto", "und") and get_lang(lang).rtl


def bidi(text: str, lang: str | None) -> str:
    """Mark each line of right-to-left text so players lay it out right to left.

    Without this, a line that starts or ends with punctuation or a number (very common in
    subtitles: "...", "?", "2024") is laid out left to right by many players, putting the period
    at the start of an Arabic or Hebrew sentence. A right-to-left mark at both ends fixes it."""
    if not text or not is_rtl(lang):
        return text
    return "\n".join(f"{RLM}{line.strip(_BIDI_MARKS)}{RLM}" if line.strip() else line for line in text.split("\n"))


def render_srt(cues: Iterable[Cue], mode: str, src_lang: str | None = None, tgt_lang: str | None = None,
               max_chars: int = 42, max_chars_cjk: int = 22, max_lines: int = 2) -> str:
    """mode: 'tgt' (translation) | 'src' (transcript) | 'bi' (transcript above translation)."""
    kw = dict(max_chars=max_chars, max_chars_cjk=max_chars_cjk, max_lines=max_lines)
    blocks = []
    n = 0
    for c in cues:
        if mode == "tgt":
            text = bidi(wrap(c.tgt, tgt_lang, **kw), tgt_lang)
        elif mode == "src":
            text = bidi(wrap(c.src, src_lang, **kw), src_lang)
        else:
            text = "\n".join(t for t in (bidi(wrap(c.src, src_lang, **kw), src_lang),
                                         bidi(wrap(c.tgt, tgt_lang, **kw), tgt_lang)) if t)
        if not text.strip():
            continue
        n += 1
        blocks.append(f"{n}\n{fmt_ts(c.start)} --> {fmt_ts(c.end)}\n{text}\n")
    return "\n".join(blocks)


def furigana_srt(srt_text: str) -> str:
    """The same SRT with inline readings after every kanji word: 漢字（かんじ）."""
    from . import furigana
    return _map_srt_text(srt_text, furigana.inline)


def srt_to_vtt(srt_text: str, ruby: bool = False) -> str:
    """Convert rendered SRT to WebVTT; with ruby=True, add <ruby> furigana to Japanese text."""
    from . import furigana
    body = _map_srt_text(srt_text, furigana.ruby if ruby else (lambda t: t.replace("&", "&amp;")
                                                                  .replace("<", "&lt;")))
    out = ["WEBVTT", ""]
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) >= 2 and "-->" in lines[1]:
            out.append(lines[1].replace(",", "."))
            out.extend(lines[2:])
            out.append("")
    return "\n".join(out)


def _map_srt_text(srt_text: str, fn) -> str:
    blocks = []
    for block in srt_text.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) >= 3 and "-->" in lines[1]:
            blocks.append("\n".join(lines[:2] + [fn("\n".join(lines[2:]))]) + "\n")
        else:
            blocks.append(block + "\n")
    return "\n".join(blocks)


def write_srt(path: Path, cues: Iterable[Cue], mode: str, **kw) -> None:
    path.write_text(render_srt(cues, mode, **kw), encoding="utf-8")


def read_srt(path: Path) -> list[tuple[float, float, str]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    out = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        ts_line = lines[1] if "-->" in lines[1] else lines[0]
        if "-->" not in ts_line:
            continue
        a, b = ts_line.split("-->")
        text_lines = lines[lines.index(ts_line) + 1:]
        text = "\n".join(l.strip(_BIDI_MARKS) for l in text_lines)      # direction marks are added on write
        out.append((parse_ts(a), parse_ts(b), text))
    return out
