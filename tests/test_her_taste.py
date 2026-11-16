"""她自己的喜好前后一致：问「你最喜欢什么零食」时，把她以前对同一问题的回答递给她。"""
import unittest

from test_preference_phrasing import SCOPE
from test_round4_wording import WordingBase
from xuyi.personas import CHAT_STYLE, GROUP_STYLE
from xuyi.retrieval import Retriever


class HerTasteTests(WordingBase):
    def auto(self, query, her=True):
        return Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True, about_me=True, her=her)['context']

    def asked_before(self, question, answer):
        self.chat('今天好累')
        self.archive.say(question, minutes=60).say(answer, role='assistant')
        self.chat('周末去爬山了')

    def test_her_earlier_answer_comes_back_even_without_shared_words(self):
        self.asked_before('你最喜欢吃什么零食呀', '薯片吧')
        context = self.auto('你最喜欢什么零食？')
        self.assertIn('薯片吧', context)
        self.assertIn('说法要和它一致', context)

    def test_the_users_own_favourite_is_not_handed_to_her(self):
        self.chat('我最喜欢的零食是辣条。')
        self.asked_before('你最喜欢吃什么零食呀', '薯片吧')
        context = self.auto('你最喜欢什么零食？')
        self.assertIn('薯片', context)
        self.assertNotIn('辣条', context)

    def test_a_different_taste_question_does_not_match(self):
        self.asked_before('你最喜欢吃什么零食呀', '薯片吧')
        self.assertEqual(self.auto('你最喜欢听谁的歌？'), '')

    def test_newest_answer_first(self):
        self.asked_before('你最喜欢什么零食', '薯片吧')
        self.asked_before('你最爱吃的零食是啥', '还是薯片，番茄味的')
        context = self.auto('你最喜欢什么零食？')
        self.assertLess(context.index('番茄味'), context.index('薯片吧'))

    def test_off_without_the_flag(self):
        self.asked_before('你最喜欢吃什么零食呀', '薯片吧')
        self.assertEqual(self.auto('你最喜欢什么零食？', her=False), '')

    def test_her_own_lookup_also_brings_it(self):
        self.asked_before('你最喜欢吃什么零食呀', '薯片吧')
        self.assertIn('薯片吧', self.look('你最喜欢什么零食？'))

    def test_chat_rules_ask_for_consistency(self):
        self.assertIn("前后要一致", CHAT_STYLE)
        self.assertIn("前后要一致", GROUP_STYLE)


if __name__ == '__main__':
    unittest.main()
