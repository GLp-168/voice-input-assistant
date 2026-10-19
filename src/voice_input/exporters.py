"""Export helpers for transcript and document templates."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable
import zipfile
from xml.sax.saxutils import escape


REGULAR_BODY_FONT = "Microsoft YaHei UI"
REGULAR_TITLE_FONT = "Microsoft YaHei UI"

OFFICIAL_BODY_FONT = "仿宋_GB2312"
OFFICIAL_TITLE_FONT = "方正小标宋简体"
OFFICIAL_TITLE_FALLBACK_FONT = "黑体"

MINUTES_BODY_FONT = "仿宋_GB2312"
MINUTES_TITLE_FONT = "黑体"

A4_PAGE_WIDTH_TWIPS = 11906
A4_PAGE_HEIGHT_TWIPS = 16838


def export_transcript_docx(path: str | Path, text: str, *, title: str = "识别结果") -> None:
    """Write a regular Word document with paragraph-preserving transcript layout."""

    paragraphs = _split_paragraphs(text)
    _write_docx(
        path,
        title=title,
        document_xml=_document_xml(
            title,
            paragraphs,
            title_style="RegularTitle",
            body_style="RegularBody",
            section=_section_xml(top=1440, right=1440, bottom=1440, left=1440, header=720, footer=720),
        ),
        styles_xml=_regular_styles_xml(),
    )


def export_official_document_docx(
    path: str | Path,
    text: str,
    *,
    title: str = "识别结果",
    title_font: str = OFFICIAL_TITLE_FONT,
    body_font: str = OFFICIAL_BODY_FONT,
) -> None:
    """Write a Word document using a GB/T 9704-2012 inspired official layout."""

    paragraphs = _split_paragraphs(text)
    body = [
        _paragraph("语音转文字", style="OfficialRedHeader"),
        _paragraph(title, style="OfficialTitle"),
        _paragraph("发文字号：〔    〕  号", style="OfficialMeta"),
        _paragraph("签发人：", style="OfficialMeta"),
        _paragraph("主送：", style="OfficialRecipient"),
    ]
    body.extend(_paragraph(paragraph, style="OfficialBody") for paragraph in paragraphs)
    body.extend(
        [
            _paragraph("附件：", style="OfficialAttachment"),
            _paragraph("联系人：        联系电话：", style="OfficialMeta"),
            _paragraph("抄送：", style="OfficialMeta"),
        ]
    )
    body.append(
        _section_xml(
            top=2098,
            right=1474,
            bottom=1984,
            left=1587,
            header=720,
            footer=720,
            doc_grid_line_pitch=580,
        )
    )
    _write_docx(
        path,
        title=title,
        document_xml=_word_document_xml(body),
        styles_xml=_official_styles_xml(title_font=title_font, body_font=body_font),
    )


def export_meeting_minutes_docx(
    path: str | Path,
    text: str,
    *,
    title: str = "会议纪要",
    title_font: str = MINUTES_TITLE_FONT,
    body_font: str = MINUTES_BODY_FONT,
) -> None:
    """Write a meeting-minutes or plain-record template Word document."""

    paragraphs = _split_paragraphs(text)
    body = [
        _paragraph(title, style="MinutesTitle"),
        _paragraph("会议时间：", style="MinutesMeta"),
        _paragraph("会议地点：", style="MinutesMeta"),
        _paragraph("主持人：        记录人：", style="MinutesMeta"),
        _paragraph("参会人员：", style="MinutesMeta"),
        _paragraph("一、会议议题", style="MinutesHeading"),
        _paragraph(_first_nonempty(paragraphs, default="（请补充会议议题）"), style="MinutesBody"),
        _paragraph("二、会议内容", style="MinutesHeading"),
    ]
    for paragraph in paragraphs:
        if paragraph.strip():
            body.append(_paragraph(paragraph, style="MinutesBody"))
    body.extend(
        [
            _paragraph("三、决议事项", style="MinutesHeading"),
            _paragraph("1. ", style="MinutesAction"),
            _paragraph("四、待办跟进", style="MinutesHeading"),
            _paragraph("责任人：        截止时间：        事项：", style="MinutesAction"),
        ]
    )
    body.append(
        _section_xml(
            top=1701,
            right=1701,
            bottom=1701,
            left=1701,
            header=720,
            footer=720,
            doc_grid_line_pitch=360,
        )
    )
    _write_docx(
        path,
        title=title,
        document_xml=_word_document_xml(body),
        styles_xml=_minutes_styles_xml(title_font=title_font, body_font=body_font),
    )


def _write_docx(path: str | Path, *, title: str, document_xml: str, styles_xml: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = datetime.now().replace(microsecond=0).isoformat()

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _package_rels_xml())
        archive.writestr("docProps/app.xml", _app_props_xml())
        archive.writestr("docProps/core.xml", _core_props_xml(created, title))
        archive.writestr("word/styles.xml", styles_xml)
        archive.writestr("word/document.xml", document_xml)


def _split_paragraphs(text: str) -> list[str]:
    paragraphs = text.splitlines()
    return paragraphs if paragraphs else [""]


def _first_nonempty(paragraphs: list[str], *, default: str) -> str:
    for paragraph in paragraphs:
        if paragraph.strip():
            return paragraph.strip()
    return default


def _document_xml(title: str, paragraphs: list[str], *, title_style: str, body_style: str, section: str) -> str:
    body = [_paragraph(title, style=title_style)]
    for paragraph in paragraphs:
        body.append(_paragraph(paragraph, style=body_style))
    body.append(section)
    return _word_document_xml(body)


def _word_document_xml(body: Iterable[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}</w:body>"
        "</w:document>"
    )


def _section_xml(
    *,
    top: int,
    right: int,
    bottom: int,
    left: int,
    header: int,
    footer: int,
    doc_grid_line_pitch: int | None = None,
) -> str:
    doc_grid = ""
    if doc_grid_line_pitch is not None:
        doc_grid = f'<w:docGrid w:type="lines" w:linePitch="{doc_grid_line_pitch}"/>'
    return (
        "<w:sectPr>"
        f'<w:pgSz w:w="{A4_PAGE_WIDTH_TWIPS}" w:h="{A4_PAGE_HEIGHT_TWIPS}"/>'
        f'<w:pgMar w:top="{top}" w:right="{right}" w:bottom="{bottom}" w:left="{left}" '
        f'w:header="{header}" w:footer="{footer}" w:gutter="0"/>'
        f"{doc_grid}"
        "</w:sectPr>"
    )


def _paragraph(text: str, *, style: str | None = None) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    safe_text = escape(text)
    if not safe_text:
        return f"<w:p>{style_xml}</w:p>"
    return f'<w:p>{style_xml}<w:r><w:t xml:space="preserve">{safe_text}</w:t></w:r></w:p>'


def _content_types_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""


def _package_rels_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _app_props_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>Voice Input Assistant</Application>
</Properties>"""


def _core_props_xml(created: str, title: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(title)}</dc:title>
  <dc:creator>Voice Input Assistant</dc:creator>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>
</cp:coreProperties>"""


def _font_xml(font: str) -> str:
    safe_font = escape(font, {'"': "&quot;"})
    return f'<w:rFonts w:ascii="{safe_font}" w:hAnsi="{safe_font}" w:eastAsia="{safe_font}"/>'


def _styles_xml(*, title_style_id: str, body_style_id: str, title_font: str, body_font: str, title_size: int, body_size: int, title_after: int, body_line: int, body_first_line_chars: int | None = None, body_alignment: str = "left", title_alignment: str = "center", body_bold: bool = False) -> str:
    body_ind = ""
    if body_first_line_chars is not None:
        body_ind = f'<w:ind w:firstLineChars="{body_first_line_chars}"/>'
    bold = "<w:b/>" if body_bold else ""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:spacing w:before="0" w:after="0" w:line="{body_line}" w:lineRule="exact"/>
    </w:pPr>
    <w:rPr>
      {_font_xml(body_font)}
      <w:sz w:val="{body_size}"/>
      <w:color w:val="000000"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="{title_style_id}">
    <w:name w:val="{title_style_id}"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:jc w:val="{title_alignment}"/>
      <w:spacing w:before="0" w:after="{title_after}" w:line="{body_line}" w:lineRule="exact"/>
    </w:pPr>
    <w:rPr>
      {_font_xml(title_font)}
      {bold}
      <w:sz w:val="{title_size}"/>
      <w:color w:val="000000"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="{body_style_id}">
    <w:name w:val="{body_style_id}"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      {body_ind}
      <w:jc w:val="{body_alignment}"/>
      <w:spacing w:before="0" w:after="0" w:line="{body_line}" w:lineRule="exact"/>
    </w:pPr>
    <w:rPr>
      {_font_xml(body_font)}
      <w:sz w:val="{body_size}"/>
      <w:color w:val="000000"/>
    </w:rPr>
  </w:style>
</w:styles>"""


def _append_paragraph_styles(styles_xml: str, styles: Iterable[str]) -> str:
    return styles_xml.replace("</w:styles>", "".join(styles) + "</w:styles>")


def _paragraph_style_xml(
    style_id: str,
    *,
    font: str,
    size: int,
    line: int,
    alignment: str = "left",
    after: int = 0,
    before: int = 0,
    color: str = "000000",
    bold: bool = False,
    first_line_chars: int | None = None,
) -> str:
    bold_xml = "<w:b/>" if bold else ""
    ind_xml = "" if first_line_chars is None else f'<w:ind w:firstLineChars="{first_line_chars}"/>'
    return f"""
  <w:style w:type="paragraph" w:styleId="{style_id}">
    <w:name w:val="{style_id}"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      {ind_xml}
      <w:jc w:val="{alignment}"/>
      <w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="exact"/>
    </w:pPr>
    <w:rPr>
      {_font_xml(font)}
      {bold_xml}
      <w:sz w:val="{size}"/>
      <w:color w:val="{color}"/>
    </w:rPr>
  </w:style>"""


def _regular_styles_xml() -> str:
    return _styles_xml(
        title_style_id="RegularTitle",
        body_style_id="RegularBody",
        title_font=REGULAR_TITLE_FONT,
        body_font=REGULAR_BODY_FONT,
        title_size=36,
        body_size=22,
        title_after=360,
        body_line=300,
        body_alignment="both",
    )


def _official_styles_xml(*, title_font: str, body_font: str) -> str:
    base = _styles_xml(
        title_style_id="OfficialTitle",
        body_style_id="OfficialBody",
        title_font=title_font or OFFICIAL_TITLE_FALLBACK_FONT,
        body_font=body_font or OFFICIAL_BODY_FONT,
        title_size=44,
        body_size=32,
        title_after=580,
        body_line=580,
        body_first_line_chars=200,
        body_alignment="both",
        body_bold=False,
    )
    return _append_paragraph_styles(
        base,
        (
            _paragraph_style_xml("OfficialRedHeader", font="黑体", size=36, line=420, alignment="center", after=240, color="C00000", bold=True),
            _paragraph_style_xml("OfficialMeta", font=body_font or OFFICIAL_BODY_FONT, size=28, line=420, alignment="center", after=120),
            _paragraph_style_xml("OfficialRecipient", font=body_font or OFFICIAL_BODY_FONT, size=32, line=580, alignment="left", after=160),
            _paragraph_style_xml("OfficialAttachment", font=body_font or OFFICIAL_BODY_FONT, size=32, line=580, alignment="left", before=240),
        ),
    )


def _minutes_styles_xml(*, title_font: str, body_font: str) -> str:
    base = _styles_xml(
        title_style_id="MinutesTitle",
        body_style_id="MinutesBody",
        title_font=title_font or MINUTES_TITLE_FONT,
        body_font=body_font or MINUTES_BODY_FONT,
        title_size=32,
        body_size=28,
        title_after=360,
        body_line=420,
        body_first_line_chars=200,
        body_alignment="both",
        body_bold=False,
    )
    return _append_paragraph_styles(
        base,
        (
            _paragraph_style_xml("MinutesMeta", font=body_font or MINUTES_BODY_FONT, size=26, line=360, alignment="left"),
            _paragraph_style_xml("MinutesHeading", font="黑体", size=28, line=420, alignment="left", before=200, after=80, bold=True),
            _paragraph_style_xml("MinutesAction", font=body_font or MINUTES_BODY_FONT, size=26, line=360, alignment="left", first_line_chars=0),
        ),
    )
