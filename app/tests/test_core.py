import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yakusuru import subtitles as st  # noqa: E402
from yakusuru.translators import parse_response  # noqa: E402


def test_timestamps_roundtrip():
    for t in (0, 1.234, 59.999, 3661.5):
        assert abs(st.parse_ts(st.fmt_ts(t)) - t) < 0.001


def test_wrap_english_two_lines():
    text = "I told you already, I'm not going to the festival with him tonight no matter what"
    out = st.wrap(text, "en", 42, 22, 2).split("\n")
    assert len(out) == 2
    assert all(len(l) <= 50 for l in out)
    assert " ".join(out) == text


def test_wrap_japanese_breaks_at_punctuation():
    text = "今日はとてもいい天気なので、みんなで公園に散歩に行きませんか"
    out = st.wrap(text, "ja", 42, 22, 2).split("\n")
    assert len(out) == 2
    assert "".join(out) == text
    assert out[0].endswith("、")


def test_clean_hallucinations_and_repeats():
    cues = [st.Cue(0, 2, src="こんにちは"), st.Cue(2, 4, src="ご視聴ありがとうございました"),
            st.Cue(4, 6, src="ああああああああああ"), st.Cue(6, 8, src="待って待って待って待って")]
    out = st.clean_cues(cues)
    texts = [c.src for c in out]
    assert "ご視聴ありがとうございました" not in texts
    assert "ああああ" in texts
    assert "待って" in texts


def test_resegment_long_cue_with_words():
    words = []
    t = 0.0
    for chunk in ["今日は", "学校で", "先生に", "怒られた。", "でも", "明日は", "きっと", "大丈夫だと", "思う。"]:
        words.append(st.Word(t, t + 1.4, chunk))
        t += 1.5
    c = st.Cue(0, t, src="".join(w.text for w in words), words=words)
    out = st.resegment([c], max_seconds=7.0, max_chars=44, lang="ja")
    assert len(out) >= 2
    assert out[0].src.endswith("。")
    assert all(x.duration <= 7.5 for x in out)


def test_render_and_read_srt(tmp_path):
    cues = [st.Cue(0.5, 2.0, src="こんにちは", tgt="Hello"), st.Cue(2.5, 4.0, src="さようなら", tgt="Goodbye")]
    p = tmp_path / "x.srt"
    st.write_srt(p, cues, "bi", src_lang="ja", tgt_lang="en")
    back = st.read_srt(p)
    assert back[0][2] == "こんにちは\nHello"
    assert abs(back[1][0] - 2.5) < 1e-6


def test_parse_response_variants():
    assert parse_response('{"t":[{"i":1,"en":"Hi"},{"i":2,"en":"Bye"}]}') == {1: "Hi", 2: "Bye"}
    assert parse_response('```json\n{"translations":[{"id":3,"text":"Yo"}]}\n```') == {3: "Yo"}
    assert parse_response('<think>hmm</think>{"1": "A", "2": "B"}') == {1: "A", 2: "B"}
    assert parse_response("Sure!\n1: First\n2. Second") == {1: "First", 2: "Second"}


def test_llm_batching_repairs_missing(monkeypatch):
    from yakusuru.config import Settings
    from yakusuru.translators import LLMTranslator

    calls = []

    class Flaky(LLMTranslator):
        key = "flaky"

        def complete(self, system, user):
            payload = json.loads(user.split("Translate these lines:\n")[1].split("\n\n")[0])
            ids = [l["i"] for l in payload["lines"]]
            calls.append(ids)
            # drop the 3rd line on the first attempt
            keep = ids if len(calls) > 1 else [i for i in ids if i != 3]
            return json.dumps({"t": [{"i": i, "en": f"line{i}"} for i in keep]})

    s = Settings()
    s.batch_size = 5
    out = Flaky(s, "m").translate([f"行{i}" for i in range(1, 8)], [])
    assert out == [f"line{i}" for i in range(1, 8)]
    assert len(calls) >= 3


@pytest.fixture
def media(tmp_path):
    from yakusuru.audio import find_ffmpeg
    ff = find_ffmpeg()
    if not ff:
        pytest.skip("no ffmpeg")
    p = tmp_path / "テスト clip.mp4"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=16",
                    "-f", "lavfi", "-i", "color=c=black:s=160x90:d=16", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(p)], check=True)
    return p


def test_pipeline_end_to_end(media, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))   # isolate settings/glossary
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from yakusuru.config import Settings
    from yakusuru.pipeline import EngineCache, load_project, process
    s = Settings()
    s.engine, s.asr_model, s.translator = "dummy", "dummy", "echo"
    s.out_translation = s.out_original = s.out_bilingual = True
    events = []
    res = process(media, s, EngineCache(), lambda *a: events.append(a), lambda: False)
    assert len(res["outputs"]) == 3
    en = Path(res["outputs"][0]).read_text(encoding="utf-8")
    assert "[EN] こんにちは" in en
    progs = [e[1] for e in events]
    assert progs == sorted(progs) and progs[-1] == 1.0
    meta, cues = load_project(Path(res["project"]))
    assert meta["engine"] == "dummy" and len(cues) == 5
    # second run with rename policy must not clobber
    res2 = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert res2["outputs"][0] != res["outputs"][0]


def test_provider_request_and_response_shapes(monkeypatch):
    """Each provider builds a sane request and extracts the reply text."""
    from yakusuru.config import Settings
    from yakusuru.translators import providers

    monkeypatch.setattr(providers.keystore, "get_key", lambda p: "test-key")
    seen = {}
    reply = '{"t":[{"i":1,"en":"Hello"}]}'

    def fake(method, url, *, headers=None, body=None, **kw):
        seen["url"], seen["headers"], seen["body"] = url, headers or {}, body or {}
        if "anthropic" in url:
            return {"content": [{"type": "text", "text": reply}]}
        if "googleapis" in url:
            return {"candidates": [{"content": {"parts": [{"text": "x", "thought": True}, {"text": reply}]}}]}
        if "deepl" in url:
            return {"translations": [{"text": "Hello"}]}
        if "/api/chat" in url:
            return {"message": {"content": "<think></think>" + reply}}
        return {"choices": [{"message": {"content": reply}}]}

    monkeypatch.setattr(providers, "http_json", fake)
    s = Settings()
    for key, check in [
        ("anthropic", lambda: seen["headers"]["x-api-key"] == "test-key" and seen["body"]["system"]),
        ("openai", lambda: seen["body"]["messages"][0]["role"] == "system"),
        ("gemini", lambda: seen["headers"]["x-goog-api-key"] == "test-key" and "systemInstruction" in seen["body"]),
        ("ollama", lambda: seen["body"]["format"] == "json" and seen["body"]["stream"] is False),
        ("deepl", lambda: seen["body"]["source_lang"] == "JA" and seen["url"].startswith("https://api.deepl")),
    ]:
        from yakusuru.translators import get_translator
        out = get_translator(key, s).translate(["こんにちは"], [])
        assert out == ["Hello"], (key, out)
        assert check(), key


def test_platform_matrix_rules(monkeypatch):
    from yakusuru import platform_matrix as pm
    cs = pm.component_support
    # Apple Silicon + 3.14: everything native incl. MLX
    monkeypatch.setattr(pm, "macos_major", lambda: 15)
    for k in ("mlx_whisper", "torch", "transformers", "faster_whisper"):
        assert cs(k, "macos", "arm64", (3, 14))[0], k
    assert not cs("mlx_whisper", "macos", "x86_64", (3, 12))[0]
    assert not cs("torch", "macos", "x86_64", (3, 13))[0]
    assert not cs("torch", "windows", "arm64", (3, 13))[0]
    assert cs("torch", "windows", "x86_64", (3, 14))[0]
    assert pm.profiles_for("macos", "arm64") == ["apple_mlx", "cpu"]
    assert "nvidia_cuda" not in pm.profiles_for("windows", "arm64")

    # Rosetta: x86_64 Python on arm64 hardware is rejected; native arm64 3.14 is accepted
    monkeypatch.setattr(pm, "os_key", lambda: "macos")
    monkeypatch.setattr(pm, "hardware_arch", lambda: "arm64")
    monkeypatch.setattr(pm, "python_arch", lambda: "x86_64")
    ok, why = pm.check_python((3, 14))
    assert not ok and "Rosetta" in why
    monkeypatch.setattr(pm, "python_arch", lambda: "arm64")
    assert pm.check_python((3, 14))[0]
    assert not pm.check_python((3, 15))[0]
    # Intel Mac caps at 3.12
    monkeypatch.setattr(pm, "hardware_arch", lambda: "x86_64")
    monkeypatch.setattr(pm, "python_arch", lambda: "x86_64")
    assert pm.check_python((3, 12))[0] and not pm.check_python((3, 13))[0]


def test_components_follow_architecture(monkeypatch):
    from yakusuru import platform_matrix as pm
    from yakusuru.deps import components_for
    from yakusuru.hardware import HardwareInfo
    monkeypatch.setattr(pm, "macos_major", lambda: 15)
    hw = HardwareInfo(os="macos", os_version="15", arch="arm64", cpu="Apple M4", ram_gb=16, python="3.14.0",
                      python_arch="arm64")
    comps = {c.key: c for c in components_for("apple_mlx", hw)}
    assert comps["mlx_whisper"].available and comps["mlx_whisper"].recommended
    assert comps["torch"].commands[0][0] == "torch" and "--index-url" not in comps["torch"].commands[0]
    assert "MPS" in comps["torch"].description
    # Windows on ARM: no torch / faster-whisper, whisper.cpp recommended for vulkan
    hw2 = HardwareInfo(os="windows", os_version="11", arch="arm64", cpu="Snapdragon", ram_gb=16,
                       python="3.13.1", python_arch="arm64")
    comps2 = {c.key: c for c in components_for("vulkan", hw2)}
    assert not comps2["torch"].available and not comps2["faster_whisper"].available
    assert comps2["whispercpp"].recommended
    # Broken Python blocks every pip component
    hw.python_ok, hw.python_note = False, "x86_64 under Rosetta"
    assert not any(c.available for c in components_for("apple_mlx", hw) if not c.manual and c.key != "ffmpeg")


def test_model_fetch_resolution():
    from yakusuru.model_fetch import resolve
    assert resolve("faster_whisper", "large-v3") == ("Systran/faster-whisper-large-v3", None)
    assert resolve("faster_whisper", "medium") == ("Systran/faster-whisper-medium", None)
    assert resolve("faster_whisper", "kotoba-tech/kotoba-whisper-v2.0-faster") == \
        ("kotoba-tech/kotoba-whisper-v2.0-faster", None)
    assert resolve("mlx", "mlx-community/whisper-large-v3-turbo") == ("mlx-community/whisper-large-v3-turbo", None)
    assert resolve("whispercpp", "ggml-kotoba-whisper-v2.0.bin") == \
        ("kotoba-tech/kotoba-whisper-v2.0-ggml", "ggml-kotoba-whisper-v2.0.bin")
    assert resolve("dummy", "x") is None


def test_running_text_shows_elapsed_and_eta():
    import time
    from yakusuru.gui.queue_model import Job, running_text
    j = Job(Path("a.mp4"))
    j.state, j.stage, j.message, j.progress, j.started = "running", "transcribing", "Transcribing", 0.5, time.time() - 60
    t = running_text(j)
    assert "50%" in t and "1:00 elapsed" in t and "left" in t
    j.stage, j.message = "downloading", "Downloading model · 10 / 1,600 MB"
    assert "%" not in running_text(j).split("·")[1]


def test_failed_translation_resumes_without_retranscribing(media, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from yakusuru.config import Settings
    from yakusuru.engines.dummy_engine import DummyEngine
    from yakusuru.pipeline import EngineCache, process
    from yakusuru.translators import providers

    calls = {"asr": 0}
    orig = DummyEngine.transcribe

    def counting(self, *a, **k):
        calls["asr"] += 1
        return orig(self, *a, **k)

    monkeypatch.setattr(DummyEngine, "transcribe", counting)
    s = Settings()
    s.engine, s.asr_model, s.translator = "dummy", "dummy", "echo"
    s.out_translation, s.out_original = True, False

    def boom(self, *a, **k):
        raise providers.TranslationError("Cannot reach Ollama")

    monkeypatch.setattr(providers.EchoTranslator, "translate", boom)
    with pytest.raises(RuntimeError, match="transcript was saved"):
        process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert calls["asr"] == 1

    monkeypatch.undo()  # restore echo translator (and env) …
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(DummyEngine, "transcribe", counting)
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert calls["asr"] == 1, "second run must reuse the saved transcript"
    assert "[EN] こんにちは" in Path(res["outputs"][0]).read_text(encoding="utf-8")


def test_legacy_name_migration(monkeypatch, tmp_path):
    """Data saved under the old app name is adopted, not reinstalled."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    # Where this OS keeps app data (mirrors paths.data_dir)
    if sys.platform == "darwin":
        base = tmp_path / "Library" / "Application Support"
    elif os.name == "nt":
        base = tmp_path / "local"
    else:
        base = tmp_path / "data"
    old = base / "LanguageInterpreter" / "venv"
    old.mkdir(parents=True)
    (old / ".core-installed").write_text("x")
    from yakusuru import paths
    d = paths.data_dir()
    assert d == base / "Yakusuru" and (d / "venv" / ".core-installed").exists()
    assert not (base / "LanguageInterpreter").exists()


def test_legacy_project_file_is_found(tmp_path):
    from yakusuru.config import Settings
    from yakusuru.pipeline import is_project_file, project_for
    src = tmp_path / "ep1.mp4"
    src.write_bytes(b"")
    (tmp_path / "ep1.langinterp.json").write_text("{}")
    s = Settings()
    assert project_for(src, s).name == "ep1.yakusuru.json"
    assert project_for(src, s, existing=True).name == "ep1.langinterp.json"
    assert is_project_file(tmp_path / "ep1.langinterp.json")


# ----------------------------------------------------------------------------- multilingual
def test_wrap_by_script():
    # Spanish wraps on words, Chinese and Thai by characters, Korean uses the full-width limit
    es = st.wrap("No puedo creer que hayas venido hasta aquí solo para decirme eso", "es", 42, 22, 2)
    assert len(es.split("\n")) == 2 and all(" " not in l[:1] for l in es.split("\n"))
    zh = st.wrap("今天天气很好，我们一起去公园散步吧，好不好呀朋友们", "zh", 42, 22, 2).split("\n")
    assert len(zh) == 2 and "".join(zh) == "今天天气很好，我们一起去公园散步吧，好不好呀朋友们"
    ko = st.wrap("오늘 날씨가 정말 좋네요 우리 같이 공원에 산책하러 갈까요", "ko", 42, 22, 2).split("\n")
    assert len(ko) == 2


def test_normalize_only_strips_spaces_for_nospace_scripts():
    assert st.normalize_text("今日 は いい 天気", "ja") == "今日はいい天気"
    assert st.normalize_text("hola  mundo", "es") == "hola mundo"


def test_hallucinations_other_languages():
    cues = [st.Cue(0, 1, src="Subtítulos realizados por la comunidad de Amara.org"),
            st.Cue(1, 2, src="Untertitel im Auftrag des ZDF, 2021"),
            st.Cue(2, 3, src="시청해 주셔서 감사합니다."),
            st.Cue(3, 4, src="¿Dónde está la estación?")]
    assert [c.src for c in st.clean_cues(cues)] == ["¿Dónde está la estación?"]


def test_project_files_from_ja_en_era_still_load():
    c = st.Cue.from_dict({"start": 1, "end": 2, "ja": "こんにちは", "en": "Hello"})
    assert (c.src, c.tgt) == ("こんにちは", "Hello")


def test_prompt_mentions_language_pair():
    from yakusuru.translators import prompts
    p = prompts.system_prompt("film", "keep", "es", "fr")
    assert "Spanish-to-French" in p and "keigo" not in p and "honorific" not in p.lower()
    j = prompts.system_prompt("anime", "keep", "ja", "en")
    assert "Japanese-to-English" in j and "keigo" in j and "Tanaka-san" in j
    a = prompts.system_prompt("anime", "keep", "auto", "de")
    assert "source-language-to-German" in a


def test_deepl_codes_and_unsupported(monkeypatch):
    from yakusuru.config import Settings
    from yakusuru.translators import TranslationError, get_translator
    s = Settings()
    s.source_lang, s.target_lang = "ja", "pt-BR"
    assert get_translator("deepl", s)._codes() == ("JA", "PT-BR")
    s.source_lang, s.target_lang = "auto", "en"
    assert get_translator("deepl", s)._codes() == (None, "EN-US")
    s.source_lang, s.target_lang = "sw", "en"
    with pytest.raises(TranslationError, match="Swahili"):
        get_translator("deepl", s)._codes()


def test_output_names_follow_languages(tmp_path):
    from yakusuru.config import Settings
    from yakusuru.pipeline import plan_outputs
    src = tmp_path / "clip.mp4"
    s = Settings()
    s.source_lang, s.target_lang = "ko", "es"
    s.out_translation = s.out_original = s.out_bilingual = True
    p = plan_outputs(src, s)
    assert (p.translation.name, p.original.name, p.bilingual.name) == ("clip.es.srt", "clip.ko.srt", "clip.ko-es.srt")
    s.source_lang = "auto"
    assert plan_outputs(src, s, "fr").original.name == "clip.fr.srt"


def test_pipeline_any_pair_and_same_language(media, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from yakusuru.config import Settings
    from yakusuru.pipeline import EngineCache, process
    s = Settings()
    s.engine, s.asr_model, s.translator = "dummy", "dummy", "echo"
    s.source_lang, s.target_lang = "auto", "de"
    s.out_translation, s.out_original, s.out_bilingual = True, True, False
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert res["source_lang"] == "ja"                           # detected from the text
    assert sorted(Path(o).name.split(".", 1)[1] for o in res["outputs"]) == ["de.srt", "ja.srt"]
    # Same language in and out → no translator call, transcript written once
    s.source_lang, s.target_lang, s.overwrite = "ja", "ja", "overwrite"
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert [Path(o).name.split(".", 1)[1] for o in res["outputs"]] == ["ja.srt"]


def test_whisper_translate_rejects_non_english(media, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from yakusuru.config import Settings
    from yakusuru.pipeline import EngineCache, process
    s = Settings()
    s.engine, s.asr_model, s.translator, s.target_lang = "dummy", "dummy", "whisper", "fr"
    with pytest.raises(RuntimeError, match="only produces English"):
        process(media, s, EngineCache(), lambda *a: None, lambda: False)


def test_old_settings_keys_migrate():
    from yakusuru.config import Settings
    s = Settings()
    s.update({"out_english": False, "out_japanese": True, "max_line_chars_en": 38})
    assert (s.out_translation, s.out_original, s.max_line_chars) == (False, True, 38)
    assert (s.source_lang, s.target_lang) == ("ja", "en")


# ----------------------------------------------------------------------------- OS colors
def test_os_accent_readers(monkeypatch, tmp_path):
    from yakusuru.gui import system_colors as sc
    monkeypatch.setattr(sc, "_run", lambda cmd: "5")                 # macOS purple
    assert sc._macos(False) == "#953D96" and sc._macos(True) == "#A550A7"
    monkeypatch.setattr(sc, "_run", lambda cmd: None)                # key missing = blue/multicolor
    assert sc._macos(True) == "#0A84FF"
    monkeypatch.setattr(sc, "_run", lambda cmd: "'teal'")            # GNOME 47+
    assert sc._gnome() == "#2190A4"
    (tmp_path / "kdeglobals").write_text("[General]\nAccentColor=61,174,233\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert sc._kde() == "#3DAEE9"


def test_accent_derivation_keeps_contrast():
    pytest.importorskip("PySide6")
    from yakusuru.gui import theme
    light = theme.derive(theme.TOKENS["light"], "#FFC600", dark=False)     # yellow on white
    assert light["accent"].lower() != "#ffc600"                            # darkened to stay readable
    dark = theme.derive(theme.TOKENS["dark"], "#007AFF", dark=True)
    assert dark["accent"] == "#007aff" and dark["accent_text"] == "#ffffff"
    assert theme.derive(theme.TOKENS["dark"], "not-a-color", True)["accent"] == theme.TOKENS["dark"]["accent"]


# ----------------------------------------------------------------------------- Ollama installer
def _fake_ollama_archive(tmp_path, ext):
    import io, tarfile
    raw = tmp_path / "payload"
    (raw / "bin").mkdir(parents=True)
    (raw / "lib" / "ollama").mkdir(parents=True)
    exe = raw / "bin" / "ollama"
    exe.write_text("#!/bin/sh\necho fake\n")
    out = tmp_path / f"src{ext}"
    if ext == ".tgz":
        with tarfile.open(out, "w:gz") as tf:
            tf.add(raw / "bin", arcname="bin")
            tf.add(raw / "lib", arcname="lib")
    else:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(raw / "bin", arcname="bin")
            tf.add(raw / "lib", arcname="lib")
        zstandard = pytest.importorskip("zstandard")
        out.write_bytes(zstandard.ZstdCompressor().compress(buf.getvalue()))
    return out


@pytest.mark.parametrize("ext", [".tgz", ".tar.zst"])
def test_ollama_linux_install_without_root(monkeypatch, tmp_path, ext):
    import shutil as _sh
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from yakusuru import ollama_install as oi
    import requests
    archive = _fake_ollama_archive(tmp_path, ext)
    urls = []

    def fake_download(url, dest, progress, cancelled, label):
        urls.append(url)
        if not url.endswith(ext):
            raise requests.HTTPError("404")
        _sh.copy(archive, dest)
        progress(1.0, "done")
        return dest

    monkeypatch.setattr(oi, "_download", fake_download)
    monkeypatch.setattr(oi.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(oi.shutil, "which", lambda name: _sh.which(name) if name == "zstd" else None)
    where = oi._install_linux(tmp_path, lambda f, m: None, lambda: False)
    assert oi.find_binary() == where and oi.is_installed()
    where = where.replace("\\", "/")          # Windows paths use backslashes
    assert where.endswith("bin/ollama") and "Yakusuru/tools/ollama" in where
    assert urls[0].startswith("https://ollama.com/download/ollama-linux-amd64")


def test_mlx_engine_hooks_the_real_module():
    """Regression: `from mlx_whisper import transcribe` is the function, not the module."""
    pytest.importorskip("mlx_whisper")
    import importlib
    from yakusuru.config import Settings
    from yakusuru.engines import mlx_engine
    tm = importlib.import_module("mlx_whisper.transcribe")
    e = mlx_engine.MlxEngine("/nonexistent-model", Settings())
    try:
        e.load()
    except Exception:
        pass        # loading weights fails here, but the progress hook must already be installed
    assert tm.tqdm.tqdm is mlx_engine._ProgressShim


def test_macos_exact_accent_parsing(monkeypatch):
    from yakusuru.gui import system_colors as sc
    sc._mac_cache.clear()
    monkeypatch.setattr(sc, "_run", lambda cmd: "10,132,255" if cmd[0] == "osascript" else "4")
    assert sc._macos(True) == "#0A84FF"
    sc._mac_cache.clear()
    monkeypatch.setattr(sc, "_run", lambda cmd: "garbage" if cmd[0] == "osascript" else "-1")
    assert sc._macos(False) == "#8C8C8C"      # falls back to the table


# ----------------------------------------------------------------------------- translation optional / Grok / memory
def test_transcript_only_mode(media, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from yakusuru.config import Settings
    from yakusuru.pipeline import EngineCache, process
    from yakusuru.translators import providers

    def boom(*a, **k):
        raise AssertionError("translator must not run when translation is off")

    monkeypatch.setattr(providers.EchoTranslator, "translate", boom)
    s = Settings()
    s.engine, s.asr_model, s.translator, s.translate = "dummy", "dummy", "echo", False
    s.out_translation, s.out_original, s.out_bilingual = True, False, True   # ignored while off
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert [Path(o).name.split(".", 1)[1] for o in res["outputs"]] == ["ja.srt"]
    # Turning translation on later reuses the transcript ("Translate Now")
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path))
    s.translate, s.overwrite = True, "overwrite"
    from yakusuru.engines.dummy_engine import DummyEngine
    monkeypatch.setattr(DummyEngine, "transcribe", boom)
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert any(o.endswith(".en.srt") for o in res["outputs"])


def test_xai_grok_request(monkeypatch):
    from yakusuru.config import Settings
    from yakusuru.translators import get_translator, providers
    monkeypatch.setattr(providers.keystore, "get_key", lambda p: "xai-test")
    seen = {}

    def fake(method, url, *, headers=None, body=None, **kw):
        seen.update(url=url, headers=headers, body=body)
        return {"choices": [{"message": {"content": '{"t":[{"i":1,"out":"Hello"}]}'}}]}

    monkeypatch.setattr(providers, "http_json", fake)
    tr = get_translator("xai", Settings())
    assert tr.model == "grok-4.7"
    assert tr.translate(["こんにちは"], []) == ["Hello"]
    assert seen["url"] == "https://api.x.ai/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer xai-test"
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_speech_model_released_before_local_llm():
    from yakusuru.pipeline import EngineCache
    calls = []

    class E:
        def unload(self):
            calls.append("unload")

    c = EngineCache()
    c.engine, c.key = E(), ("x",)
    c.release()
    assert calls == ["unload"] and c.engine is None and c.key is None


def test_ollama_low_memory_behaviour(monkeypatch):
    from yakusuru.config import Settings
    from yakusuru.models import recommended_ollama_model
    from yakusuru.translators import providers
    sent = []
    monkeypatch.setattr(providers, "http_json", lambda m, u, **kw: sent.append((u, kw.get("body"))) or {})
    tr = providers.OllamaTranslator(Settings(), "qwen3:14b")
    monkeypatch.setattr(providers.OllamaTranslator, "_ram_gb", staticmethod(lambda: 16.0))
    assert tr._num_ctx() == 4096
    tr.finish()
    assert sent and sent[-1][1] == {"model": "qwen3:14b", "keep_alive": 0}
    monkeypatch.setattr(providers.OllamaTranslator, "_ram_gb", staticmethod(lambda: 64.0))
    sent.clear()
    tr.finish()
    assert tr._num_ctx() == 8192 and not sent
    assert (recommended_ollama_model(16), recommended_ollama_model(8), recommended_ollama_model(32)) == \
        ("qwen3:8b", "qwen3:4b", "qwen3:14b")


def test_diagnostics_classify():
    from yakusuru.diagnostics import classify
    assert classify(["x.py:f"], 0, 2e6).startswith("downloading")
    assert "file lock" in classify(["filelock/_api.py:acquire"], 0, 0)
    assert classify(["urllib3/connection.py:getresponse", "socket.py:readinto"], 0, 0).startswith("waiting on the network")
    assert classify(["safetensors/numpy.py:load"], 40, 0) == "reading model weights from disk"
    assert classify(["foo.py:bar"], 95, 0) == "computing"
    assert classify(["foo.py:bar"], 0, 0).startswith("idle")


def test_diagnostics_where_reports_this_thread():
    import threading
    from yakusuru.diagnostics import where
    loc, frames = where(threading.get_ident())
    assert "test_diagnostics_where_reports_this_thread" in loc


def test_quiet_note():
    from pathlib import Path
    from yakusuru.gui.queue_model import Job, quiet_note, running_text
    j = Job(Path("a.mp4"), state="running", message="Loading model", quiet=200, activity="waiting on the network",
            stalled=True)
    assert quiet_note(j).startswith("⚠ Looks stuck for 3:20")
    assert running_text(j).startswith("⚠ Looks stuck")
    j.quiet = 5
    assert quiet_note(j) == ""


def _fake_whisper(monkeypatch, japanese_for_translate):
    """Dummy engine whose 'translate' task returns Japanese for models in `japanese_for_translate`
    (like large-v3-turbo does) and English otherwise."""
    from yakusuru import models
    from yakusuru.engines.dummy_engine import SAMPLE, DummyEngine
    from yakusuru.subtitles import Cue
    monkeypatch.setitem(models.ASR_MODELS, "dummy", [
        models.AsrModel("dummy-turbo", "turbo", 0.1, can_translate=False),
        models.AsrModel("dummy-large-v3", "large", 0.1)])
    used = []

    def transcribe(self, audio, wav_path, duration, task, progress, cancelled):
        used.append((self.model_id, task))
        out = []
        for i, line in enumerate(SAMPLE[:5]):
            c = Cue(i * 3 + 0.2, i * 3 + 2.8)
            if task == "translate":
                c.tgt = line if self.model_id in japanese_for_translate else f"English line {i + 1}."
            else:
                c.src = line
            out.append(c)
        progress(1.0)
        return out
    monkeypatch.setattr(DummyEngine, "transcribe", transcribe)
    return used


def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


def test_whisper_translate_falls_back_to_large_v3(media, monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    used = _fake_whisper(monkeypatch, {"dummy-turbo"})
    from yakusuru.config import Settings
    from yakusuru.pipeline import EngineCache, process
    s = Settings()
    s.engine, s.asr_model, s.translator, s.source_lang, s.target_lang = "dummy", "dummy-turbo", "whisper", "ja", "en"
    # English only: turbo is skipped entirely
    s.out_translation, s.out_original, s.out_bilingual = True, False, False
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    en = Path(res["outputs"][0]).read_text(encoding="utf-8")
    assert "English line 1." in en and "こんにちは" not in en
    assert used == [("dummy-large-v3", "translate")]
    # Japanese + English: turbo transcribes, large-v3 translates
    used.clear()
    s.out_original = True
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert used == [("dummy-turbo", "transcribe"), ("dummy-large-v3", "translate")]
    assert any(o.endswith(".en.srt") for o in res["outputs"])


def test_untranslated_output_is_an_error_not_success(media, monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    _fake_whisper(monkeypatch, {"dummy-turbo", "dummy-large-v3"})     # even large-v3 "fails"
    from yakusuru.config import Settings
    from yakusuru.pipeline import EngineCache, process
    s = Settings()
    s.engine, s.asr_model, s.translator, s.source_lang, s.target_lang = "dummy", "dummy-large-v3", "whisper", "ja", "en"
    s.out_translation, s.out_original, s.out_bilingual = True, True, False
    with pytest.raises(RuntimeError, match="still in Japanese"):
        process(media, s, EngineCache(), lambda *a: None, lambda: False)
    assert not list(media.parent.glob("*.en.srt"))           # no fake "English" file
    assert list(media.parent.glob("*.ja.srt"))                # the transcript is kept


def test_untranslated_share():
    from yakusuru.languages import untranslated_share as u
    assert u(["こんにちは", "今日はいい天気ですね", "OK"], "ja", "en") > 0.6
    assert u(["Hello, Tanaka-san.", "Nice weather."], "ja", "en") == 0.0
    assert u(["Hola", "Hello"], "es", "en") == 0.0          # same script: not judged
    assert u(["Привет", "Hello"], "ru", "en") == 0.5


def _fake_net(monkeypatch, v4_ok, v6_ok):
    import socket
    from yakusuru import netcheck
    v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))
    v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0))
    monkeypatch.setattr(netcheck, "_orig_getaddrinfo", lambda *a, **k: [v6, v4])
    monkeypatch.setattr(netcheck, "_try", lambda addr, t: (v4_ok, "v4") if addr[0] == socket.AF_INET else (v6_ok, "v6"))
    monkeypatch.setattr(netcheck, "_ipv4_only", False)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)    # restored after the test
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    return netcheck


def test_netcheck_broken_ipv6_switches_to_ipv4(monkeypatch):
    nc = _fake_net(monkeypatch, v4_ok=True, v6_ok=False)
    ok, _ = nc.check("https://huggingface.co")
    assert ok and nc._ipv4_only


def test_netcheck_offline_explains(monkeypatch):
    nc = _fake_net(monkeypatch, v4_ok=False, v6_ok=False)
    ok, why = nc.check("https://huggingface.co")
    assert not ok and "huggingface.co" in why and "firewall" in why


def test_monitor_names_connect_target():
    import threading
    import time
    from yakusuru.diagnostics import net_target
    got = []

    def create_connection(address, timeout=None):      # stands in for socket.create_connection
        time.sleep(0.5)
    t = threading.Thread(target=create_connection, args=(("huggingface.co", 443),))
    t.start()
    time.sleep(0.1)
    got.append(net_target(t.ident))
    t.join()
    assert got == ["huggingface.co:443"]


def test_furigana_readings_and_okurigana():
    from yakusuru import furigana
    if not furigana.available():
        pytest.skip("SudachiPy not installed")
    assert furigana.inline("今日は東京へ行きました") == "今日（きょう）は東京（とうきょう）へ行（い）きました"
    assert furigana.inline("取り扱い") == "取（と）り扱（あつか）い"
    assert furigana.inline("私は明日") == "私（わたし）は明日（あした）"        # spoken readings
    assert furigana.inline("ちょっと待って、100円") == "ちょっと待（ま）って、100円（えん）"
    assert furigana.ruby("田中さん") == "<ruby>田中<rt>たなか</rt></ruby>さん"
    assert furigana.inline("こんにちは") == "こんにちは"


def test_srt_to_vtt_and_furigana_srt():
    from yakusuru.subtitles import Cue, furigana_srt, render_srt, srt_to_vtt
    srt = render_srt([Cue(0.5, 2.4, "a < b"), Cue(3, 4, "second")], "src", src_lang="en")
    vtt = srt_to_vtt(srt)
    assert vtt.startswith("WEBVTT\n\n00:00:00.500 --> 00:00:02.400\na &lt; b\n")
    from yakusuru import furigana
    if furigana.available():
        out = furigana_srt(render_srt([Cue(0, 1, "東京")], "src", src_lang="ja"))
        assert "東京（とうきょう）" in out and "00:00:00,000 --> 00:00:01,000" in out


def test_pipeline_writes_furigana_and_timing(media, monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    from yakusuru import furigana
    if not furigana.available():
        pytest.skip("SudachiPy not installed")
    from yakusuru.config import Settings
    from yakusuru.pipeline import EngineCache, process
    s = Settings()
    s.engine, s.asr_model, s.translator, s.source_lang = "dummy", "dummy", "echo", "ja"
    s.translate, s.out_original, s.out_furigana, s.furigana_style = False, True, True, "both"
    res = process(media, s, EngineCache(), lambda *a: None, lambda: False)
    names = [Path(o).name for o in res["outputs"]]
    assert any(n.endswith(".ja.furigana.srt") for n in names) and any(n.endswith(".ja.vtt") for n in names)
    vtt = next(Path(o) for o in res["outputs"] if o.endswith(".vtt")).read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT") and "<ruby>" in vtt
    assert "seconds" in res and isinstance(res["breakdown"], str)


def test_time_column():
    import time
    from pathlib import Path
    from yakusuru.gui.queue_model import Job, time_text
    j = Job(Path("a.mp4"))
    assert time_text(j) == ""
    j.state, j.started = "running", time.time() - 65
    assert time_text(j) == "1:05"
    j.state, j.seconds = "done", 185
    assert time_text(j) == "3:05"
