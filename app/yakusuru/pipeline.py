"""The processing pipeline for one media file. Qt-free so it can run in a worker process."""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__, audio, glossary
from .config import Settings
from .engines import Engine, get_engine_class
from .models import can_whisper_translate, find_asr, is_anime_model, whisper_translate_model
from .languages import AUTO, WHISPER_TRANSLATE_TARGETS, copied_share, guess_from_text, untranslated_share
from .languages import get as get_lang
from .languages import name as lang_name
from .subtitles import Cue, align_by_overlap, clean_cues, fix_timing, render_srt, resegment

from .i18n import _ as _t

log = logging.getLogger(__name__)

PROJECT_SUFFIX = ".yakusuru.json"
LEGACY_PROJECT_SUFFIXES = (".langinterp.json",)   # written by earlier versions; still readable


def is_project_file(p: Path) -> bool:
    return p.name.endswith((PROJECT_SUFFIX, *LEGACY_PROJECT_SUFFIXES))


@dataclass
class OutputPlan:
    translation: Path | None
    original: Path | None
    bilingual: Path | None
    project: Path


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    stem = p.name[: -len("".join(p.suffixes[-2:]))] if len(p.suffixes) >= 2 else p.stem
    tail = "".join(p.suffixes[-2:]) if len(p.suffixes) >= 2 else p.suffix
    k = 2
    while True:
        cand = p.with_name(f"{stem} ({k}){tail}")
        if not cand.exists():
            return cand
        k += 1


def _out_folder(src: Path, s: Settings) -> Path:
    return Path(s.output_folder).expanduser() if (s.output_mode == "folder" and s.output_folder) else src.parent


def lang_suffixes(src_code: str | None, tgt_code: str) -> dict[str, str]:
    """File suffixes, e.g. ja→en: .en.srt / .ja.srt / .ja-en.srt (players pick the language from these)."""
    sc = src_code if src_code and src_code not in (AUTO, "und") else "orig"
    return {"translation": f".{tgt_code}.srt", "original": f".{sc}.srt", "bilingual": f".{sc}-{tgt_code}.srt"}


def plan_outputs(src: Path, s: Settings, src_code: str | None = None) -> OutputPlan:
    folder = _out_folder(src, s)
    folder.mkdir(parents=True, exist_ok=True)
    suf = lang_suffixes(src_code or s.source_lang, s.target_lang)
    same = _same_language(src_code or s.source_lang, s.target_lang)

    def mk(kind: str, enabled: bool) -> Path | None:
        if not enabled:
            return None
        p = folder / f"{src.stem}{suf[kind]}"
        return _unique(p) if s.overwrite == "rename" else p

    return OutputPlan(
        # Same language in and out: the "translation" *is* the transcript, so write it once.
        translation=mk("translation", s.translate and s.out_translation and not (same and s.out_original)),
        original=mk("original", s.out_original or not s.translate),
        bilingual=mk("bilingual", s.translate and s.out_bilingual and not same),
        project=folder / f"{src.stem}{PROJECT_SUFFIX}",
    )


def outputs_exist(src: Path, s: Settings) -> bool:
    folder = _out_folder(src, s)
    suf = lang_suffixes(s.source_lang, s.target_lang)
    wanted = [(suf["translation"], s.translate and s.out_translation),
              (suf["original"], (s.out_original or not s.translate) and s.source_lang != AUTO),
              (suf["bilingual"], s.translate and s.out_bilingual and s.source_lang != AUTO)]
    wanted = [(x, on) for x, on in wanted if on]
    return bool(wanted) and all((folder / f"{src.stem}{x}").exists() for x, _ in wanted)


def _same_language(a: str | None, b: str | None) -> bool:
    if not a or not b or AUTO in (a, b):
        return False
    return get_lang(a).whisper == get_lang(b).whisper and a.split("-")[0] == b.split("-")[0] and "-" not in b


class EngineCache:
    """Keeps the last ASR model loaded between jobs (loading large-v3 can take a while)."""

    def __init__(self):
        self.key = None
        self.engine: Engine | None = None

    def _key(self, s: Settings):
        return (s.engine, s.asr_model, s.device, s.compute_type, s.whispercpp_binary)

    def release(self) -> None:
        """Unload the speech model and give its memory back (GPU/unified memory included)."""
        if self.engine is not None:
            try:
                self.engine.unload()
            except Exception:
                pass
        self.engine, self.key = None, None
        import gc
        gc.collect()

    def is_loaded(self, s: Settings) -> bool:
        return self.engine is not None and self.key == self._key(s)

    def get(self, s: Settings) -> Engine:
        key = self._key(s)
        if self.engine is not None and self.key == key:
            self.engine.s = s
            return self.engine
        if self.engine is not None:
            try:
                self.engine.unload()
            except Exception:
                pass
            self.engine = None
        cls = get_engine_class(s.engine)
        ok, why = cls.available()
        if not ok:
            raise RuntimeError(f"Engine '{s.engine}' is not ready: {why}. Click Setup on the main window to install it.")
        eng = cls(s.asr_model, s)
        eng.load()
        self.engine, self.key = eng, key
        return eng


Emit = Callable[[str, float, str], None]   # (stage, overall_progress 0..1, message)


STAGE_LABELS = {"extracting": "audio", "downloading": "download", "loading": "model load",
                "transcribing": "transcribe", "preparing": "translator setup", "translating": "translate",
                "writing": "write"}


def fmt_clock(sec: float) -> str:
    sec = int(round(max(0.0, sec)))
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"


def process(src: Path, s: Settings, cache: EngineCache, emit: Emit, cancelled: Callable[[], bool]) -> dict:
    t_start = time.time()
    marks: list[tuple[str, float]] = []           # (stage, start time) — for the timing breakdown
    _emit = emit

    def emit(stage: str, prog: float, msg: str) -> None:   # noqa: F811 — wraps the caller's emit
        if not marks or marks[-1][0] != stage:
            marks.append((stage, time.time()))
        _emit(stage, prog, msg)

    def breakdown() -> str:
        spent: dict[str, float] = {}
        ends = [t for _s, t in marks[1:]] + [time.time()]
        for (st, t0), t1 in zip(marks, ends):
            label = STAGE_LABELS.get(st, st)
            spent[label] = spent.get(label, 0.0) + (t1 - t0)
        return " · ".join(f"{k} {fmt_clock(v)}" for k, v in spent.items() if v >= 0.5)
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    if s.overwrite == "skip" and outputs_exist(src, s):
        emit("skipped", 1.0, _t("Outputs already exist, skipped"))
        return {"skipped": True, "outputs": []}

    whisper_mode = s.translator == "whisper"
    if (s.translate and whisper_mode and s.target_lang not in WHISPER_TRANSLATE_TARGETS
            and (s.out_translation or s.out_bilingual)):
        raise RuntimeError(f"Whisper's built-in translation only produces English, not {lang_name(s.target_lang)}. "
                           "Pick an LLM or DeepL translator for other languages.")
    want_tgt = s.translate and (s.out_translation or s.out_bilingual)
    want_src = s.out_original or s.out_bilingual or not whisper_mode
    if whisper_mode and want_tgt and not want_src and not can_whisper_translate(s.engine, s.asr_model):
        # Only the English file is wanted and the chosen model can't translate: skip it entirely
        # and run the one pass with large-v3.
        alt = whisper_translate_model(s.engine)
        if alt:
            log.info("%s can't do Whisper's translation, so this file uses %s (downloaded once).",
                     s.asr_model.split("/")[-1], alt.split("/")[-1])
            s2 = Settings()
            s2.update(s.to_dict())
            s2.asr_model = alt
            s = s2
    src_lang = None if s.source_lang == AUTO else s.source_lang     # None until detected
    src_label = lang_name(s.source_lang) if src_lang else _t("speech")
    # Stage weights for the overall progress bar
    w_extract, w_load = 0.05, 0.05
    if whisper_mode:
        w_asr, w_asr_tr, w_tr = (0.40 if want_src else 0.0), (0.45 if want_tgt else 0.0), 0.0
    else:
        w_asr, w_asr_tr, w_tr = 0.55, 0.0, (0.30 if want_tgt else 0.0)
    total_w = w_extract + w_load + w_asr + w_asr_tr + w_tr + 0.05
    done = 0.0

    def stage(name: str, w: float, msg: str = ""):
        def cb(f: float):
            emit(name, (done + w * max(0.0, min(1.0, f))) / total_w, msg)
        return cb

    glossary_terms = glossary.load_for(src)
    tmpdir = Path(tempfile.mkdtemp(prefix="yakusuru_"))
    try:
        cues: list[Cue] = []
        tr_cues: list[Cue] = []
        duration = 0.0
        # 0. resume: reuse a saved transcript from an earlier run of the same file/model/language
        reused = None if whisper_mode else load_checkpoint(src, s)
        if reused:
            cues, src_lang = reused
            duration = max((c.end for c in cues), default=0.0)
            done = w_extract + w_load + w_asr
            emit("transcribing", done / total_w, _t("Reusing saved transcript"))
            log.info("Reusing the saved %s transcript (%d lines) — skipping transcription.",
                     lang_name(src_lang), len(cues))
        else:
            # 1. audio -----------------------------------------------------------
            emit("extracting", 0.0, _t("Extracting audio"))
            wav = tmpdir / "audio.wav"
            audio.extract_audio(src, wav, progress=stage("extracting", w_extract, _t("Extracting audio")),
                                cancelled=cancelled)
            done += w_extract
            pcm = audio.load_wav(wav)
            duration = len(pcm) / audio.SAMPLE_RATE
            log.info("%s: %.1f min of audio", src.name, duration / 60)

            # 2. model -----------------------------------------------------------
            if not cache.is_loaded(s):
                from .model_fetch import ensure_model
                short = s.asr_model.split("/")[-1]
                ensure_model(s.engine, s.asr_model,
                             lambda f, msg: emit("downloading", (done + w_load * 0.5 * f) / total_w, msg),
                             cancelled)
                emit("loading", (done + w_load * 0.5) / total_w, _t("Loading {model} into memory").format(model=short))
            engine = cache.get(s)
            log.info("Engine: %s · model %s · device %s", s.engine, s.asr_model, engine.device_used)
            entry = find_asr(s.engine, s.asr_model)
            if entry and entry.languages and src_lang and get_lang(src_lang).whisper not in entry.languages:
                log.warning("%s is trained for %s only — %s may come out poorly. Use large-v3 or turbo.",
                            s.asr_model, ", ".join(lang_name(x) for x in entry.languages), lang_name(src_lang))
            done += w_load
            if cancelled():
                raise InterruptedError()

            # 3. transcription ---------------------------------------------------
            if want_src:
                msg = (_t("Transcribing {language}").format(language=src_label) if src_lang
                       else _t("Detecting language & transcribing"))
                emit("transcribing", done / total_w, msg)
                cues = engine.transcribe(pcm, str(wav), duration, "transcribe",
                                         stage("transcribing", w_asr, msg), cancelled)
                done += w_asr
                if not src_lang:
                    src_lang = engine.detected_language or guess_from_text(" ".join(c.src for c in cues))
                    log.info("Detected language: %s", lang_name(src_lang) if src_lang else "unknown")
                cues = clean_cues(cues, s.filter_hallucinations, src_lang)
                cues = resegment(cues, s.max_cue_seconds, s.line_limit(src_lang) * s.max_lines,
                                 s.min_cue_seconds, src_lang)
                log.info("Transcribed %d subtitle lines", len(cues))
                if not whisper_mode:
                    # Checkpoint: if translation fails, "Run Again" resumes from here.
                    save_project(project_for(src, s), src, s, cues, [], stage="transcribed", src_lang=src_lang)

            if whisper_mode and want_tgt:
                tr_engine = engine
                if not can_whisper_translate(s.engine, s.asr_model):
                    # e.g. large-v3-turbo: it was never trained to translate and just writes the source
                    # language again. Do the English pass with large-v3 instead.
                    alt = whisper_translate_model(s.engine)
                    if not alt:
                        raise RuntimeError(f"{s.asr_model} can't do Whisper's built-in translation, and no "
                                           "large-v3 model is available for this engine. Pick an LLM or DeepL "
                                           "translator instead.")
                    log.info("%s can't translate, so the English pass uses %s (more accurate, slower; "
                             "downloaded once).", s.asr_model.split("/")[-1], alt.split("/")[-1])
                    s_tr = Settings()
                    s_tr.update(s.to_dict())
                    s_tr.asr_model = alt
                    cache.release()                       # one big model in memory at a time
                    from .model_fetch import ensure_model
                    ensure_model(s.engine, alt,
                                 lambda f, msg: emit("downloading", done / total_w, msg + " " + _t("(for translation)")),
                                 cancelled)
                    emit("loading", done / total_w, _t("Loading {model} for translation").format(model=alt.split('/')[-1]))
                    tr_engine = cache.get(s_tr)
                emit("translating", done / total_w, _t("Whisper → English"))
                tr_cues = tr_engine.transcribe(pcm, str(wav), duration, "translate",
                                               stage("translating", w_asr_tr, _t("Whisper → English")), cancelled)
                engine = tr_engine
                if not src_lang:
                    src_lang = engine.detected_language
                done += w_asr_tr
                tr_cues = [c for c in tr_cues if c.tgt.strip()]
                tr_cues = fix_timing(clean_cues(tr_cues, s.filter_hallucinations, "en", attr="tgt"),
                                     s.min_cue_seconds)
                if cues:
                    align_by_overlap(cues, tr_cues, "tgt")
                else:
                    cues = tr_cues

        # 4. LLM / DeepL translation ----------------------------------------------
        if not whisper_mode and want_tgt and cues:
            if _same_language(src_lang, s.target_lang):
                log.info("Source and target are both %s — no translation needed.", lang_name(src_lang))
                for c in cues:
                    c.tgt = c.src
            else:
                from .translators import get_translator
                if s.translator in ("ollama", "openai_compatible"):
                    # The local LLM needs the memory: drop the speech model first (it reloads in a few
                    # seconds for the next file). Avoids swapping on 8–16 GB machines.
                    cache.release()
                tr = get_translator(s.translator, s)
                tr.src_lang = src_lang or AUTO
                here = done
                tr.prepare(lambda m: emit("preparing", here / total_w, m), cancelled)
                todo = [i for i, c in enumerate(cues) if not c.tgt.strip() or c.tgt.startswith("[?]")]
                if todo:
                    pair = f"{lang_name(src_lang) if src_lang else _t('source')} → {lang_name(s.target_lang)}"
                    emit("translating", done / total_w, _t("Translating {pair}").format(pair=pair))
                    log.info("Translating %d lines %s with %s (%s)", len(todo), pair, s.translator, tr.model)
                    prog = stage("translating", w_tr, _t("Translating {pair}").format(pair=pair))
                    try:
                        if len(todo) == len(cues):
                            out = tr.translate([c.src for c in cues], glossary_terms, prog, cancelled,
                                               s.show_notes)
                            todo = list(range(len(cues)))
                        else:
                            out = _translate_subset(tr, cues, todo, glossary_terms, s, prog, cancelled)
                    except InterruptedError:
                        raise
                    except Exception as e:
                        raise RuntimeError(f"{e}\n\nThe transcript was saved — fix the issue and use "
                                           "“Run Again”: only the translation will be redone.") from e
                    for i, t in zip(todo, out):
                        cues[i].tgt = t
                tr.finish()
            done += w_tr

        # 5. check: never report success for "English" subtitles that are still Japanese -----
        if want_tgt and src_lang and s.translator != "echo":      # echo = test translator (copies text)
            got = tr_cues if (whisper_mode and tr_cues and not want_src) else cues
            script_share = untranslated_share([c.tgt for c in got], src_lang, s.target_lang)
            # Same script on both sides (Spanish → English…): look for lines copied unchanged instead.
            copy_share = (copied_share([(c.src, c.tgt) for c in cues if c.src and c.tgt])
                          if script_share == 0 and not _same_language(src_lang, s.target_lang) else 0.0)
            n_lines = sum(1 for c in got if c.tgt.strip())
            if script_share > 0.3 or copy_share > 0.5:
                if want_src and cues and not whisper_mode:
                    save_project(project_for(src, s), src, s, cues, [], stage="transcribed", src_lang=src_lang)
                elif want_src and cues and any(c.src for c in cues):
                    plan0 = plan_outputs(src, s, src_lang)
                    if plan0.original:
                        plan0.original.write_text(render_srt(cues, "src", src_lang=src_lang,
                                                             tgt_lang=s.target_lang, max_chars=s.max_line_chars,
                                                             max_chars_cjk=s.max_line_chars_cjk,
                                                             max_lines=s.max_lines), encoding="utf-8")
                tgt_name, src_name = lang_name(s.target_lang), lang_name(src_lang)
                what = (f"{round(script_share * n_lines)} of the {tgt_name} lines are still in {src_name} "
                        f"({script_share:.0%})" if script_share > 0.3 else
                        f"{copy_share:.0%} of the lines came back as the original {src_name} text, unchanged")
                raise RuntimeError(
                    f"Translation failed: {what}. No {tgt_name} subtitle file was written."
                    + (" Whisper's built-in translation is unreliable for this audio. Try an LLM translator "
                       "(Claude, Grok, OpenAI…) and use “Run Again”." if whisper_mode else
                       " The transcript was saved; use “Run Again” to retry just the translation."))
            if script_share > 0:
                log.warning("%.0f%% of the %s lines still look like %s. Check them in the editor.",
                            script_share * 100, lang_name(s.target_lang), lang_name(src_lang))
            elif copy_share > 0.15:
                log.warning("%.0f%% of the lines were left unchanged from the original. Check them in the editor.",
                            copy_share * 100)

        # 6. write ---------------------------------------------------------------
        emit("writing", done / total_w, _t("Writing subtitles"))
        plan = plan_outputs(src, s, src_lang)
        kw = dict(src_lang=src_lang, tgt_lang=s.target_lang, max_chars=s.max_line_chars,
                  max_chars_cjk=s.max_line_chars_cjk, max_lines=s.max_lines)
        outputs = []
        if plan.translation:
            # Whisper's direct translation keeps its own (better) timings when there is no transcript pass.
            use = tr_cues if (whisper_mode and tr_cues and not want_src) else cues
            plan.translation.write_text(render_srt(use, "tgt", **kw), encoding="utf-8")
            outputs.append(str(plan.translation))
        if plan.original and any(c.src for c in cues):
            plan.original.write_text(render_srt(cues, "src", **kw), encoding="utf-8")
            outputs.append(str(plan.original))
        if plan.bilingual:
            plan.bilingual.write_text(render_srt(cues, "bi", **kw), encoding="utf-8")
            outputs.append(str(plan.bilingual))
        if s.out_furigana:
            outputs += _write_furigana(plan, s, src_lang)
        save_project(plan.project, src, s, cues, outputs, src_lang=src_lang)
        elapsed = time.time() - t_start
        speed = f" · {duration / max(1, elapsed):.1f}× realtime" if duration else ""
        parts = breakdown()
        log.info("✓ Done %s — total %s%s%s", src.name, fmt_clock(elapsed), speed, f"  ({parts})" if parts else "")
        emit("done", 1.0, _t("Done in {time}").format(time=fmt_clock(elapsed)))
        return {"outputs": outputs, "project": str(plan.project), "lines": len(cues), "seconds": elapsed,
                "source_lang": src_lang, "breakdown": breakdown()}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _write_furigana(plan: OutputPlan, s: Settings, src_lang: str | None) -> list[str]:
    """Extra Japanese files with readings: name.ja.furigana.srt (inline) and/or name.ja.vtt (ruby)."""
    from . import furigana
    from .subtitles import furigana_srt, srt_to_vtt
    tracks = []
    if plan.original and plan.original.exists() and (src_lang or "").split("-")[0] == "ja":
        tracks.append(plan.original)
    if plan.translation and plan.translation.exists() and s.target_lang.split("-")[0] == "ja":
        tracks.append(plan.translation)
    if not tracks:
        log.info("Furigana skipped: no Japanese subtitle file in this job.")
        return []
    if not furigana.available():
        log.warning("Furigana skipped: the Japanese reading dictionary isn't installed. "
                    "Turn on Furigana in the Output section and click Install (about 80 MB).")
        return []
    out = []
    for p in tracks:
        text = p.read_text(encoding="utf-8")
        base = p.name[:-4]                      # strip ".srt"
        if s.furigana_style in ("inline", "both"):
            q = p.with_name(base + ".furigana.srt")
            q.write_text(furigana_srt(text), encoding="utf-8")
            out.append(str(q))
        if s.furigana_style in ("ruby", "both"):
            q = p.with_name(base + ".vtt")
            q.write_text(srt_to_vtt(text, ruby=True), encoding="utf-8")
            out.append(str(q))
    log.info("Furigana: wrote %s", ", ".join(Path(o).name for o in out))
    return out


# --------------------------------------------------------------------------- project files
def save_project(path: Path, src: Path, s: Settings, cues: list[Cue], outputs: list[str],
                 stage: str = "complete", src_lang: str | None = None) -> None:
    data = {
        "stage": stage,
        "app": "Yakusuru", "version": __version__, "source": str(src),
        "source_lang_setting": s.source_lang, "source_lang": src_lang or s.source_lang,
        "target_lang": s.target_lang,
        "engine": s.engine, "asr_model": s.asr_model, "translator": s.translator,
        "translator_model": s.model_for(), "content_type": s.content_type,
        "outputs": outputs, "cues": [c.to_dict() for c in cues],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_checkpoint(src: Path, s: Settings) -> tuple[list[Cue], str | None] | None:
    """(cues, source language) saved for this exact file + engine + model + language, or None."""
    p = project_for(src, s, existing=True)
    if not p.exists():
        return None
    try:
        data, cues = load_project(p)
    except Exception:
        return None
    # Files from before multilingual support were always Japanese → English.
    setting = data.get("source_lang_setting", data.get("source_lang", "ja"))
    if (data.get("source") != str(src) or data.get("engine") != s.engine
            or data.get("asr_model") != s.asr_model or setting != s.source_lang
            or not any(c.src for c in cues)):
        return None
    if (data.get("translator") != s.translator or data.get("translator_model") != s.model_for()
            or data.get("target_lang", "en") != s.target_lang):
        for c in cues:          # different translator or language now: re-translate everything
            c.tgt = ""
    src_lang = data.get("source_lang", "ja")
    return cues, (None if src_lang == AUTO else src_lang)


def _translate_subset(tr, cues, todo, terms, s, progress, cancelled):
    """Translate only the listed lines, still giving the model the full transcript as context."""
    want = set(todo)
    lines = [c.src if i in want else "" for i, c in enumerate(cues)]
    out = tr.translate(lines, terms, progress, cancelled, s.show_notes)
    return [out[i] for i in todo]


def load_project(path: Path) -> tuple[dict, list[Cue]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data, [Cue.from_dict(d) for d in data.get("cues", [])]


def project_for(src: Path, s: Settings, existing: bool = False) -> Path:
    """Project file for `src`. With existing=True, fall back to a file saved under the old suffix."""
    folder = Path(s.output_folder).expanduser() if (s.output_mode == "folder" and s.output_folder) else src.parent
    p = folder / f"{src.stem}{PROJECT_SUFFIX}"
    if existing and not p.exists():
        for suf in LEGACY_PROJECT_SUFFIXES:
            old = folder / f"{src.stem}{suf}"
            if old.exists():
                return old
    return p
