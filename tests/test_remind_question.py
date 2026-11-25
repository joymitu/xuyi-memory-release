"""问她还记不记得某件事（「还记得上次那家烤肉店吗」）：句子里没有「我」，也要自动带上相关原话；没共同词就什么都不给。"""
import unittest

from test_preference_phrasing import SCOPE
from test_round4_wording import WordingBase
from xuyi.retrieval import REMIND_QUESTION, Retriever


class RemindQuestionTests(WordingBase):
    def auto(self, query):
        return Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True, about_me=True)['context']

    def test_remind_questions_are_recognised(self):
        for text in ['还记得上次那家烤肉店吗', '你记得那部电影不', '记不记得那个烤肉店', '那本书你忘了没', '上次说的那家店叫啥']:
            self.assertTrue(REMIND_QUESTION.search(text), text)
        for text in ['今天好累', '记得带伞哦', '你忘了吗', '早']:
            self.assertFalse(REMIND_QUESTION.search(text), text)

    def test_the_old_line_comes_back_without_saying_i(self):
        self.chat('楼下新开的烤肉店叫炭火时光，好吃。')
        self.assertIn('炭火时光', self.auto('还记得上次那家烤肉店吗'))

    def test_nothing_shared_means_nothing_injected(self):
        self.chat('楼下新开的烤肉店叫炭火时光，好吃。')
        self.assertEqual(self.auto('你记得吃饭没'), '')
        self.assertEqual(self.auto('还记得那部电影吗'), '')


if __name__ == '__main__':
    unittest.main()
