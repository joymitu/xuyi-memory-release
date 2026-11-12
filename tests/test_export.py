import io
import unittest
import zipfile
from datetime import datetime

from xuyi.export import TITLE, docx, markdown

BOOK = {
    "days": [
        {"date": "2026-11-09", "weekday": "周一", "count": 6, "notes": [],
         "entries": [{"time": "16:01", "text": "我家的猫叫年糕"}],
         "diary_text": "第一段。\n\n第二段，带个 <尖括号> & 和号。"},
        {"date": "2026-11-08", "weekday": "周日", "count": 2, "notes": [],
         "entries": [{"time": "10:00", "text": "在吗"}], "diary_text": None},
    ]
}
CENTER = {"days": 1, "messages": 50}
WHEN = datetime(2026, 11, 9, 19, 0)


class MarkdownTests(unittest.TestCase):
    def test_covers_title_every_day_and_the_original_lines(self):
        text = markdown(BOOK, CENTER, WHEN)
        self.assertIn(f"# {TITLE}", text)
        self.assertIn("## 11 月 9 日 · 周一", text)
        self.assertIn("## 11 月 8 日 · 周日", text)
        self.assertIn("第一段。", text)
        self.assertIn("第二段，带个 <尖括号> & 和号。", text)   # markdown 不转义原文
        self.assertIn("- 16:01 我家的猫叫年糕", text)
        self.assertIn("（这一页还没写。）", text)                # 没生成的那天只留原话
        self.assertIn("认识 1 天", text)
        self.assertIn("导出 2026-11-09 19:00", text)


class DocxTests(unittest.TestCase):
    def test_it_is_a_real_zip_with_a_word_document(self):
        data = docx(BOOK, CENTER, WHEN)
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        self.assertIn("[Content_Types].xml", names)
        self.assertIn("_rels/.rels", names)
        self.assertIn("word/document.xml", names)
        xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("<w:document", xml)
        self.assertIn("11 月 9 日 · 周一", xml)
        self.assertIn("第一段。", xml)
        self.assertIn("那天的原话", xml)

    def test_the_text_is_xml_escaped(self):
        xml = zipfile.ZipFile(io.BytesIO(docx(BOOK, CENTER, WHEN))).read("word/document.xml").decode("utf-8")
        self.assertIn("&lt;尖括号&gt; &amp; 和号", xml)
        self.assertNotIn("<尖括号>", xml)

    def test_days_are_separated_by_page_breaks(self):
        xml = zipfile.ZipFile(io.BytesIO(docx(BOOK, CENTER, WHEN))).read("word/document.xml").decode("utf-8")
        self.assertEqual(xml.count('w:type="page"'), 1)


if __name__ == "__main__":
    unittest.main()
