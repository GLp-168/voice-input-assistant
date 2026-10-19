from __future__ import annotations

from pathlib import Path
import zipfile

from voice_input.exporters import (
    export_meeting_minutes_docx,
    export_official_document_docx,
    export_transcript_docx,
)


def _read_docx_xml(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(member).decode("utf-8")


def _count_docx_paragraph_text(document_xml: str) -> int:
    return document_xml.count("<w:t xml:space=\"preserve\">")


def test_export_transcript_docx_keeps_regular_word_export(tmp_path: Path) -> None:
    output = tmp_path / "regular.docx"

    export_transcript_docx(output, "第一段\n第二段 & <测试>\n\n第四段", title="识别结果 & 存档")

    assert output.exists()
    document_xml = _read_docx_xml(output, "word/document.xml")
    styles_xml = _read_docx_xml(output, "word/styles.xml")
    core_xml = _read_docx_xml(output, "docProps/core.xml")
    assert '<w:pStyle w:val="RegularTitle"/>' in document_xml
    assert '<w:pStyle w:val="RegularBody"/>' in document_xml
    assert _count_docx_paragraph_text(document_xml) == 4
    assert "第一段" in document_xml
    assert "第二段 &amp; &lt;测试&gt;" in document_xml
    assert '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"' in document_xml
    assert '<w:spacing w:before="0" w:after="0" w:line="300" w:lineRule="exact"/>' in styles_xml
    assert 'w:eastAsia="Microsoft YaHei UI"' in styles_xml
    assert "识别结果 &amp; 存档" in core_xml


def test_export_official_document_docx_uses_common_gbt_9704_layout(tmp_path: Path) -> None:
    output = tmp_path / "official.docx"

    export_official_document_docx(output, "第一段内容\n第二段内容", title="会议纪要")

    document_xml = _read_docx_xml(output, "word/document.xml")
    styles_xml = _read_docx_xml(output, "word/styles.xml")
    assert '<w:pStyle w:val="OfficialTitle"/>' in document_xml
    assert '<w:pStyle w:val="OfficialRedHeader"/>' in document_xml
    assert '<w:pStyle w:val="OfficialMeta"/>' in document_xml
    assert '<w:pStyle w:val="OfficialRecipient"/>' in document_xml
    assert '<w:pStyle w:val="OfficialAttachment"/>' in document_xml
    assert document_xml.count('<w:pStyle w:val="OfficialBody"/>') == 2
    assert "发文字号" in document_xml
    assert "主送" in document_xml
    assert "附件" in document_xml
    assert '<w:pgSz w:w="11906" w:h="16838"/>' in document_xml
    assert '<w:pgMar w:top="2098" w:right="1474" w:bottom="1984" w:left="1587"' in document_xml
    assert '<w:docGrid w:type="lines" w:linePitch="580"/>' in document_xml
    assert '<w:jc w:val="center"/>' in styles_xml
    assert '<w:ind w:firstLineChars="200"/>' in styles_xml
    assert '<w:spacing w:before="0" w:after="0" w:line="580" w:lineRule="exact"/>' in styles_xml
    assert '<w:sz w:val="32"/>' in styles_xml
    assert '<w:sz w:val="44"/>' in styles_xml
    assert '<w:color w:val="C00000"/>' in styles_xml
    assert 'w:eastAsia="仿宋_GB2312"' in styles_xml
    assert 'w:eastAsia="方正小标宋简体"' in styles_xml


def test_export_official_document_docx_can_use_title_fallback_font(tmp_path: Path) -> None:
    output = tmp_path / "fallback.docx"

    export_official_document_docx(output, "正文", title="通知", title_font="黑体")

    styles_xml = _read_docx_xml(output, "word/styles.xml")
    assert 'w:eastAsia="黑体"' in styles_xml


def test_export_meeting_minutes_docx_has_distinct_template(tmp_path: Path) -> None:
    output = tmp_path / "minutes.docx"

    export_meeting_minutes_docx(output, "议题一\n讨论内容\n结论", title="会议纪要")

    document_xml = _read_docx_xml(output, "word/document.xml")
    styles_xml = _read_docx_xml(output, "word/styles.xml")
    assert '<w:pStyle w:val="MinutesTitle"/>' in document_xml
    assert '<w:pStyle w:val="MinutesMeta"/>' in document_xml
    assert '<w:pStyle w:val="MinutesHeading"/>' in document_xml
    assert '<w:pStyle w:val="MinutesAction"/>' in document_xml
    assert document_xml.count('<w:pStyle w:val="MinutesBody"/>') == 4
    assert "会议时间" in document_xml
    assert "决议事项" in document_xml
    assert "待办跟进" in document_xml
    assert '<w:pgMar w:top="1701" w:right="1701" w:bottom="1701" w:left="1701"' in document_xml
    assert '<w:docGrid w:type="lines" w:linePitch="360"/>' in document_xml
    assert '<w:ind w:firstLineChars="200"/>' in styles_xml
    assert '<w:sz w:val="32"/>' in styles_xml
    assert '<w:sz w:val="28"/>' in styles_xml
    assert 'w:styleId="MinutesHeading"' in styles_xml
