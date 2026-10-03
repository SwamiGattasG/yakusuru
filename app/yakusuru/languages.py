"""Language catalog: every language Whisper can transcribe, plus a few translation-only variants.

Each entry knows what the rest of the app needs:
  * how to show it ("Japanese — 日本語")
  * whether its script uses spaces between words (line wrapping, cleanup)
  * whether characters are full-width (subtitle line-length limits)
  * its DeepL code, if DeepL supports it
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str            # ISO 639-1 (Whisper code) or BCP-47 for variants: "ja", "zh-Hant", "pt-BR"
    name: str            # English name
    native: str = ""     # endonym
    nospace: bool = False   # no spaces between words (ja, zh, th…) → wrap by characters
    wide: bool = False      # full-width glyphs → shorter line limit
    rtl: bool = False
    deepl_src: str = ""
    deepl_tgt: str = ""
    asr: bool = True        # can be a transcription (source) language

    @property
    def label(self) -> str:
        from .i18n import lang_label          # "Japonés — 日本語" in a Spanish interface
        return lang_label(self.code, self.name, self.native)

    @property
    def whisper(self) -> str:
        """Code to pass to Whisper (variants map to their base language)."""
        return self.code.split("-")[0]


L = Language
_ALL: list[Language] = [
    # Most common first (shown at the top of the pickers)
    L("ja", "Japanese", "日本語", nospace=True, wide=True, deepl_src="JA", deepl_tgt="JA"),
    L("en", "English", "English", deepl_src="EN", deepl_tgt="EN-US"),
    L("zh", "Chinese", "中文", nospace=True, wide=True, deepl_src="ZH", deepl_tgt="ZH-HANS"),
    L("ko", "Korean", "한국어", wide=True, deepl_src="KO", deepl_tgt="KO"),
    L("es", "Spanish", "Español", deepl_src="ES", deepl_tgt="ES"),
    L("fr", "French", "Français", deepl_src="FR", deepl_tgt="FR"),
    L("de", "German", "Deutsch", deepl_src="DE", deepl_tgt="DE"),
    L("pt", "Portuguese", "Português", deepl_src="PT", deepl_tgt="PT-PT"),
    L("it", "Italian", "Italiano", deepl_src="IT", deepl_tgt="IT"),
    L("ru", "Russian", "Русский", deepl_src="RU", deepl_tgt="RU"),
    # Translation-only regional variants
    L("zh-Hant", "Chinese (Traditional)", "繁體中文", nospace=True, wide=True, deepl_tgt="ZH-HANT", asr=False),
    L("pt-BR", "Portuguese (Brazil)", "Português (Brasil)", deepl_tgt="PT-BR", asr=False),
    L("en-GB", "English (UK)", "English (UK)", deepl_tgt="EN-GB", asr=False),
    L("es-419", "Spanish (Latin America)", "Español (Latinoamérica)", deepl_tgt="ES-419", asr=False),
    # Everything else Whisper supports (alphabetical)
    L("af", "Afrikaans", "Afrikaans"), L("sq", "Albanian", "Shqip"), L("am", "Amharic", "አማርኛ"),
    L("ar", "Arabic", "العربية", rtl=True, deepl_src="AR", deepl_tgt="AR"),
    L("hy", "Armenian", "Հայերեն"), L("as", "Assamese", "অসমীয়া"), L("az", "Azerbaijani", "Azərbaycan"),
    L("ba", "Bashkir", "Башҡорт"), L("eu", "Basque", "Euskara"), L("be", "Belarusian", "Беларуская"),
    L("bn", "Bengali", "বাংলা"), L("bs", "Bosnian", "Bosanski"), L("br", "Breton", "Brezhoneg"),
    L("bg", "Bulgarian", "Български", deepl_src="BG", deepl_tgt="BG"),
    L("my", "Burmese", "မြန်မာ", nospace=True), L("yue", "Cantonese", "粵語", nospace=True, wide=True),
    L("ca", "Catalan", "Català"), L("hr", "Croatian", "Hrvatski"),
    L("cs", "Czech", "Čeština", deepl_src="CS", deepl_tgt="CS"),
    L("da", "Danish", "Dansk", deepl_src="DA", deepl_tgt="DA"),
    L("nl", "Dutch", "Nederlands", deepl_src="NL", deepl_tgt="NL"),
    L("et", "Estonian", "Eesti", deepl_src="ET", deepl_tgt="ET"), L("fo", "Faroese", "Føroyskt"),
    L("fi", "Finnish", "Suomi", deepl_src="FI", deepl_tgt="FI"), L("gl", "Galician", "Galego"),
    L("ka", "Georgian", "ქართული"), L("el", "Greek", "Ελληνικά", deepl_src="EL", deepl_tgt="EL"),
    L("gu", "Gujarati", "ગુજરાતી"), L("ht", "Haitian Creole", "Kreyòl ayisyen"), L("ha", "Hausa", "Hausa"),
    L("haw", "Hawaiian", "ʻŌlelo Hawaiʻi"), L("he", "Hebrew", "עברית", rtl=True, deepl_src="HE", deepl_tgt="HE"),
    L("hi", "Hindi", "हिन्दी"), L("hu", "Hungarian", "Magyar", deepl_src="HU", deepl_tgt="HU"),
    L("is", "Icelandic", "Íslenska"), L("id", "Indonesian", "Bahasa Indonesia", deepl_src="ID", deepl_tgt="ID"),
    L("jw", "Javanese", "Basa Jawa"), L("kn", "Kannada", "ಕನ್ನಡ"), L("kk", "Kazakh", "Қазақ"),
    L("km", "Khmer", "ខ្មែរ", nospace=True), L("lo", "Lao", "ລາວ", nospace=True), L("la", "Latin", "Latina"),
    L("lv", "Latvian", "Latviešu", deepl_src="LV", deepl_tgt="LV"), L("ln", "Lingala", "Lingála"),
    L("lt", "Lithuanian", "Lietuvių", deepl_src="LT", deepl_tgt="LT"), L("lb", "Luxembourgish", "Lëtzebuergesch"),
    L("mk", "Macedonian", "Македонски"), L("mg", "Malagasy", "Malagasy"), L("ms", "Malay", "Bahasa Melayu"),
    L("ml", "Malayalam", "മലയാളം"), L("mt", "Maltese", "Malti"), L("mi", "Maori", "Te Reo Māori"),
    L("mr", "Marathi", "मराठी"), L("mn", "Mongolian", "Монгол"), L("ne", "Nepali", "नेपाली"),
    L("no", "Norwegian", "Norsk", deepl_src="NB", deepl_tgt="NB"), L("nn", "Norwegian Nynorsk", "Nynorsk"),
    L("oc", "Occitan", "Occitan"), L("ps", "Pashto", "پښتو", rtl=True), L("fa", "Persian", "فارسی", rtl=True),
    L("pl", "Polish", "Polski", deepl_src="PL", deepl_tgt="PL"), L("pa", "Punjabi", "ਪੰਜਾਬੀ"),
    L("ro", "Romanian", "Română", deepl_src="RO", deepl_tgt="RO"), L("sa", "Sanskrit", "संस्कृतम्"),
    L("sr", "Serbian", "Српски"), L("sn", "Shona", "chiShona"), L("sd", "Sindhi", "سنڌي", rtl=True),
    L("si", "Sinhala", "සිංහල"), L("sk", "Slovak", "Slovenčina", deepl_src="SK", deepl_tgt="SK"),
    L("sl", "Slovenian", "Slovenščina", deepl_src="SL", deepl_tgt="SL"), L("so", "Somali", "Soomaali"),
    L("su", "Sundanese", "Basa Sunda"), L("sw", "Swahili", "Kiswahili"),
    L("sv", "Swedish", "Svenska", deepl_src="SV", deepl_tgt="SV"), L("tl", "Tagalog", "Tagalog"),
    L("tg", "Tajik", "Тоҷикӣ"), L("ta", "Tamil", "தமிழ்"), L("tt", "Tatar", "Татар"), L("te", "Telugu", "తెలుగు"),
    L("th", "Thai", "ไทย", nospace=True, deepl_src="TH", deepl_tgt="TH"), L("bo", "Tibetan", "བོད་ཡིག", nospace=True),
    L("tr", "Turkish", "Türkçe", deepl_src="TR", deepl_tgt="TR"), L("tk", "Turkmen", "Türkmen"),
    L("uk", "Ukrainian", "Українська", deepl_src="UK", deepl_tgt="UK"), L("ur", "Urdu", "اردو", rtl=True),
    L("uz", "Uzbek", "Oʻzbek"), L("vi", "Vietnamese", "Tiếng Việt", deepl_src="VI", deepl_tgt="VI"),
    L("cy", "Welsh", "Cymraeg"), L("yi", "Yiddish", "ייִדיש", rtl=True), L("yo", "Yoruba", "Yorùbá"),
]
del L

BY_CODE: dict[str, Language] = {lang.code: lang for lang in _ALL}
COMMON = ["ja", "en", "zh", "ko", "es", "fr", "de", "pt", "it", "ru"]
AUTO = "auto"

# Whisper's built-in translation can only produce English.
WHISPER_TRANSLATE_TARGETS = {"en", "en-GB"}


def get(code: str | None) -> Language:
    """Language for a code; unknown codes become a generic space-separated language."""
    if not code:
        return Language("und", "the original language")
    if code in BY_CODE:
        return BY_CODE[code]
    base = code.split("-")[0]
    if base in BY_CODE:
        return BY_CODE[base]
    return Language(code, code)


def name(code: str | None) -> str:
    """Language name in the interface language ("Japanese", "Japonés", "日本語")."""
    from .i18n import _
    if code == AUTO:
        return _("Auto-detect")
    return _(get(code).name)


def source_languages() -> list[Language]:
    return [lang for lang in _ALL if lang.asr]


def target_languages() -> list[Language]:
    return list(_ALL)


# --------------------------------------------------------------------------- script detection
_SCRIPTS = [
    ("ja", re.compile(r"[぀-ヿｦ-ﾟ]")),            # kana ⇒ Japanese
    ("ko", re.compile(r"[가-힯ᄀ-ᇿ]")),
    ("zh", re.compile(r"[一-鿿]")),                          # Han without kana
    ("th", re.compile(r"[฀-๿]")),
    ("ar", re.compile(r"[؀-ۿ]")),
    ("he", re.compile(r"[֐-׿]")),
    ("ru", re.compile(r"[Ѐ-ӿ]")),
    ("el", re.compile(r"[Ͱ-Ͽ]")),
    ("hi", re.compile(r"[ऀ-ॿ]")),
]


def guess_from_text(text: str) -> str | None:
    """Rough script-based guess, used only when an engine doesn't report the detected language."""
    sample = text[:4000]
    best, count = None, 0
    for code, rx in _SCRIPTS:
        n = len(rx.findall(sample))
        if n > count:
            best, count = code, n
    if best == "zh" and _SCRIPTS[0][1].search(sample):
        best = "ja"
    return best if count >= 5 else None


def uses_nospace_text(text: str) -> bool:
    """True when text is mostly in a script written without spaces (CJK, Thai…)."""
    g = guess_from_text(text)
    return bool(g and get(g).nospace)


# --------------------------------------------------------------------------- output check
# Which writing systems a language uses. Anything not listed is Latin script.
_LANG_SCRIPTS = {
    "ja": {"kana", "han"}, "zh": {"han"}, "yue": {"han"}, "ko": {"hangul"}, "th": {"thai"},
    "ar": {"arabic"}, "fa": {"arabic"}, "ur": {"arabic"}, "ps": {"arabic"}, "he": {"hebrew"}, "yi": {"hebrew"},
    "el": {"greek"}, "hi": {"devanagari"}, "mr": {"devanagari"}, "ne": {"devanagari"}, "sa": {"devanagari"},
    **{c: {"cyrillic"} for c in ("ru", "uk", "be", "bg", "mk", "sr", "kk", "ba", "tt", "mn", "tg")},
}


def _char_script(ch: str) -> str | None:
    o = ord(ch)
    if 0x3040 <= o <= 0x30FF or 0xFF66 <= o <= 0xFF9F:
        return "kana"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
        return "han"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
        return "hangul"
    if 0x0E00 <= o <= 0x0E7F:
        return "thai"
    if 0x0600 <= o <= 0x06FF:
        return "arabic"
    if 0x0590 <= o <= 0x05FF:
        return "hebrew"
    if 0x0400 <= o <= 0x04FF:
        return "cyrillic"
    if 0x0370 <= o <= 0x03FF:
        return "greek"
    if 0x0900 <= o <= 0x097F:
        return "devanagari"
    if ch.isalpha() and o < 0x0250:
        return "latin"
    return None


def scripts_of(code: str | None) -> set[str]:
    if not code:
        return set()
    return _LANG_SCRIPTS.get(code.split("-")[0].lower(), {"latin"})


def untranslated_share(texts: list[str], src_lang: str | None, tgt_lang: str) -> float:
    """Fraction (0–1) of non-empty lines still written in the source language's script.

    Catches a translator that just echoed the source (e.g. Whisper's translate task with a model
    that wasn't trained for it returns Japanese for "English"). Only meaningful when the two
    languages use different scripts; returns 0.0 otherwise."""
    src_s, tgt_s = scripts_of(src_lang), scripts_of(tgt_lang)
    if not src_s or src_s & tgt_s:
        return 0.0
    lines = [t for t in texts if t and t.strip()]
    if not lines:
        return 0.0
    bad = 0
    for line in lines:
        counts: dict[str, int] = {}
        for ch in line:
            sc = _char_script(ch)
            if sc:
                counts[sc] = counts.get(sc, 0) + 1
        if not counts:
            continue
        in_src = sum(n for sc, n in counts.items() if sc in src_s)
        in_tgt = sum(n for sc, n in counts.items() if sc in tgt_s)
        if in_src > in_tgt:
            bad += 1
    return bad / len(lines)
