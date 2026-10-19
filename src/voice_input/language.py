"""Language selection and transcript normalization helpers."""

from __future__ import annotations

from functools import lru_cache


LANGUAGE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("zh-Hans", "中文（简体）"),
    ("zh-Hant", "中文（繁体）"),
    ("en", "English"),
    ("auto", "自动检测"),
)


_ENGINE_LANGUAGE = {
    "zh-Hans": "zh",
    "zh-Hant": "zh",
    "en": "en",
    "auto": None,
}


_T2S_FALLBACK = str.maketrans(
    {
        "財": "财",
        "務": "务",
        "預": "预",
        "算": "算",
        "科": "科",
        "目": "目",
        "報": "报",
        "表": "表",
        "識": "识",
        "別": "别",
        "結": "结",
        "果": "果",
        "語": "语",
        "音": "音",
        "輸": "输",
        "入": "入",
        "助": "助",
        "手": "手",
        "開": "开",
        "關": "关",
        "機": "机",
        "啟": "启",
        "動": "动",
        "後": "后",
        "當": "当",
        "前": "前",
        "檔": "档",
        "選": "选",
        "擇": "择",
        "徑": "径",
        "設": "设",
        "置": "置",
        "體": "体",
        "轉": "转",
        "寫": "写",
        "內": "内",
        "離": "离",
        "線": "线",
        "運": "运",
        "行": "行",
    }
)
_S2T_FALLBACK = str.maketrans({v: k for k, v in _T2S_FALLBACK.items()})


def language_label(value: str | None) -> str:
    normalized = _normalize_language_setting(value)
    for option_value, label in LANGUAGE_OPTIONS:
        if option_value == normalized:
            return label
    return normalized


def engine_language_code(value: str | None) -> str | None:
    """Return the language code understood by whisper.cpp, or None for auto."""

    return _ENGINE_LANGUAGE.get(_normalize_language_setting(value), "zh")


def normalize_transcript_text(text: str, value: str | None) -> str:
    """Normalize recognized text for the selected output language."""

    setting = _normalize_language_setting(value)
    if setting == "zh-Hans":
        return _convert_chinese(text, "t2s")
    if setting == "zh-Hant":
        return _convert_chinese(text, "s2t")
    return text


def punctuate_transcript_text(text: str, *, close_sentence: bool = True) -> str:
    """Conservatively close ASR text without guessing semantic comma breaks."""

    paragraphs = text.splitlines()
    if not paragraphs:
        return ""
    return "\n".join(_punctuate_paragraph(paragraph.strip(), close_sentence=close_sentence) for paragraph in paragraphs)


def _normalize_language_setting(value: str | None) -> str:
    value = (value or "").strip()
    aliases = {
        "zh": "zh-Hans",
        "zh-cn": "zh-Hans",
        "zh_cn": "zh-Hans",
        "zh-hans": "zh-Hans",
        "zh-tw": "zh-Hant",
        "zh_tw": "zh-Hant",
        "zh-hant": "zh-Hant",
        "chinese": "zh-Hans",
        "english": "en",
        "": "zh-Hans",
    }
    return aliases.get(value.lower(), value if value in _ENGINE_LANGUAGE else "zh-Hans")


def _punctuate_paragraph(text: str, *, close_sentence: bool) -> str:
    if not text:
        return ""
    if _has_sentence_punctuation(text):
        return _ensure_sentence_close(text, close_sentence=close_sentence)
    # Mid-sentence punctuation needs model context. A length-based fallback can
    # split numbers, dates and terms incorrectly during live streaming.
    if _cjk_ratio(text) < 0.45:
        return text.rstrip()
    return _ensure_sentence_close(text, close_sentence=close_sentence)


def _ensure_sentence_close(text: str, *, close_sentence: bool, terminal: str = "。") -> str:
    stripped = text.rstrip()
    if not stripped or not close_sentence:
        return stripped
    if stripped[-1] in "。！？.!?；;：:":
        return stripped
    return stripped + terminal


def _has_sentence_punctuation(text: str) -> bool:
    return any(char in "，,。！？!?；;：:" for char in text)


def _cjk_ratio(text: str) -> float:
    visible = [char for char in text if not char.isspace()]
    if not visible:
        return 0.0
    cjk = sum(1 for char in visible if _is_cjk(char))
    return cjk / len(visible)


def _is_cjk(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
    )


def _convert_chinese(text: str, mode: str) -> str:
    try:
        converter = _opencc(mode)
        if converter is not None:
            return converter.convert(text)
    except Exception:
        pass
    table = _T2S_FALLBACK if mode == "t2s" else _S2T_FALLBACK
    return text.translate(table)


@lru_cache(maxsize=2)
def _opencc(mode: str) -> object | None:
    try:
        from opencc import OpenCC
    except Exception:
        return None
    return OpenCC(mode)
