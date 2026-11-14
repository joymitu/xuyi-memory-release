"""问句两道关要对得上：插件层放行（Node 测试查），Python 家族接得住（这里查）。"""
import json
import unittest
from pathlib import Path

from xuyi.highlights import FAMILIES

FIXTURE = Path(__file__).parent / 'fixtures' / 'memory_questions.json'


class QuestionGateTests(unittest.TestCase):
    def test_every_shared_question_reaches_a_family(self):
        questions = json.loads(FIXTURE.read_text(encoding='utf-8'))['questions']
        self.assertGreater(len(questions), 10)
        for question in questions:
            with self.subTest(question=question):
                self.assertTrue(any(family.QUERY.search(question) for family in FAMILIES))


if __name__ == '__main__':
    unittest.main()
