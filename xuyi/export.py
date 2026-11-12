"""把日记本导出成 Markdown 或 Word（.docx）。

导出只用已经生成好的正文；没有正文的那一天就只导出原话，不会为了导出偷偷调模型。
Word 文件是直接用 OOXML 拼的，不依赖 python-docx，装好本项目就能用。
"""
import zipfile
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

TITLE = "TA的日记本"


def _head(center, now=None):
    now = now or datetime.now()
    parts = []
    if center:
        parts.append(f"认识 {center.get('days', 0)} 天")
        parts.append(f"聊过 {center.get('messages', 0)} 句")
    parts.append("导出 " + now.strftime("%Y-%m-%d %H:%M"))
    return " · ".join(parts)


def _day_title(day):
    return f"{int(day['date'][5:7])} 月 {int(day['date'][8:10])} 日 · {day['weekday']}"


def markdown(book, center=None, now=None):
    out = [f"# {TITLE}", "", _head(center, now), ""]
    for day in book.get("days", []):
        out += ["---", "", f"## {_day_title(day)}", ""]
        text = (day.get("diary_text") or "").strip()
        if text:
            out += [part.strip() + "\n" for part in text.split("\n\n")]
        else:
            out += ["（这一页还没写。）", ""]
        if day.get("entries"):
            out += ["", "**那天的原话**", ""]
            for entry in day["entries"]:
                out.append(f"- {entry['time']} {entry['text']}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _para(text, *, size=28, bold=False, indent=0, align=None, east="KaiTi", after=120):
    props = ['<w:spacing w:before="0" w:after="%d" w:line="360" w:lineRule="auto"/>' % after]
    if indent:
        props.append(f'<w:ind w:firstLineChars="{indent}"/>')
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    run = ([f'<w:rFonts w:ascii="{east}" w:hAnsi="{east}" w:eastAsia="{east}"/>',
            f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>']
           + (['<w:b/>'] if bold else []))
    return ('<w:p><w:pPr>' + "".join(props) + '</w:pPr><w:r><w:rPr>'
            + "".join(run) + '</w:rPr><w:t xml:space="preserve">'
            + escape(text) + "</w:t></w:r></w:p>")


def _page_break():
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _document_xml(book, center, now=None):
    body = [_para(TITLE, size=40, bold=True, align="center", east="SimHei", after=80),
            _para(_head(center, now), size=20, align="center", east="SimSun", after=240)]
    days = book.get("days", [])
    for index, day in enumerate(days):
        body.append(_para(_day_title(day), size=30, bold=True, east="SimHei", after=120))
        text = (day.get("diary_text") or "").strip()
        if text:
            for part in text.split("\n\n"):
                body.append(_para(part.strip(), indent=200))
        else:
            body.append(_para("（这一页还没写。）", size=24, east="KaiTi", indent=200))
        if day.get("entries"):
            body.append(_para("那天的原话", size=22, bold=True, east="SimHei", after=80))
            for entry in day["entries"]:
                body.append(_para(f"{entry['time']} {entry['text']}", size=20, east="KaiTi", after=40))
        if index < len(days) - 1:
            body.append(_page_break())
    body.append('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>" + "".join(body) + "</w:body></w:document>")


CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.wordprocessingml.document.main+xml"/></Types>')

ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
             'relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def docx(book, center=None, now=None):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/document.xml", _document_xml(book, center, now))
    return buffer.getvalue()
