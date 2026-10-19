from __future__ import annotations

from voice_input.language import punctuate_transcript_text


def test_punctuate_transcript_text_does_not_guess_mid_sentence_breaks() -> None:
    text = (
        "\u4eca\u5929\u4f1a\u8bae\u8ba8\u8bba\u4e86"
        "\u91cd\u70b9\u9879\u76ee\u63a8\u8fdb\u60c5\u51b5"
        "\u540c\u65f6\u660e\u786e\u4e0b\u4e00\u6b65"
        "\u5de5\u4f5c\u5b89\u6392"
    )

    assert punctuate_transcript_text(text, close_sentence=False) == text
    assert punctuate_transcript_text(text) == text + "\u3002"


def test_punctuate_transcript_text_keeps_existing_punctuation() -> None:
    text = "\u4f1a\u8bae\u5f00\u59cb\uff0c\u8bf7\u5927\u5bb6\u53d1\u8a00\u3002"

    assert punctuate_transcript_text(text) == text


def test_punctuate_transcript_text_does_not_split_amounts() -> None:
    text = "\u5168\u5e74\u5b8c\u6210\u6295\u8d44\u4e94\u4e07\u4ebf\u5143\u540c\u6bd4\u589e\u957f\u767e\u5206\u4e4b\u4e09"

    assert punctuate_transcript_text(text, close_sentence=False) == text
    assert "\uff0c\u4e07\u4ebf" not in punctuate_transcript_text(text)


def test_punctuate_transcript_text_does_not_add_chinese_period_to_latin_text() -> None:
    assert punctuate_transcript_text("file transcript") == "file transcript"
