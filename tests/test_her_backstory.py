"""她自己的身世（老家、年纪、工作、学校）前后一致：问「你老家哪的」时，把她以前的回答递给她；
问的是用户自己的，不递给她。"""
import unittest

from test_preference_phrasing import SCOPE
from test_round4_wording import WordingBase
from xuyi.highlights import asks_her
from xuyi.personas import CHAT_STYLE, GROUP_STYLE
from xuyi.retrieval import Retriever


class AsksHerBackstoryTests(unittest.TestCase):
    def test_questions_about_her_own_background_are_recognised(self):
        for text in ['你老家哪的', '你老家是哪里的呀', '你是哪里人', '你多大了', '你几岁啦', '你在哪上班',
                     '你是做什么工作的', '你在哪儿读书', '你住哪', '你有兄弟姐妹吗', '你什么专业的']:
            self.assertTrue(asks_her(text), text)

    def test_questions_about_mine_or_time_bound_ones_are_not(self):
        for text in ['我老家哪的来着', '你知道我多大了吗', '我在哪上班来着', '你昨天干嘛了', '你吃饭了吗',
                     '我妈多大了', '你还记得我是哪里人吗']:
            self.assertFalse(asks_her(text), text)


class HerBackstoryTests(WordingBase):
    def auto(self, query, her=True):
        return Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True, about_me=True, her=her)['context']

    def asked_before(self, question, answer):
        self.chat('今天好累')
        self.archive.say(question, minutes=60).say(answer, role='assistant')
        self.chat('周末去爬山了')

    def test_her_earlier_hometown_comes_back_under_a_different_wording(self):
        self.asked_before('你是哪里人呀', '成都的，小时候在那边长大')
        context = self.auto('你老家哪的')
        self.assertIn('成都', context)
        self.assertIn('说法要和它一致', context)

    def test_her_earlier_age_and_job_come_back(self):
        self.asked_before('你今年几岁了', '二十八，不小了')
        self.asked_before('你是做什么工作的', '做设计的')
        self.assertIn('二十八', self.auto('你多大了'))
        self.assertIn('做设计', self.auto('你在哪上班'))

    def test_the_users_own_hometown_is_not_handed_to_her(self):
        self.chat('我老家是哈尔滨的。')
        self.asked_before('你老家哪的呀', '成都的')
        context = self.auto('你老家哪的')
        self.assertIn('成都', context)
        self.assertNotIn('哈尔滨', context)

    def test_another_topic_does_not_match(self):
        self.asked_before('你老家哪的呀', '成都的')
        self.assertEqual(self.auto('你多大了'), '')

    def test_the_chat_rules_ask_for_a_steady_backstory(self):
        for style in (CHAT_STYLE, GROUP_STYLE):
            self.assertIn('身世', style)
            self.assertIn('老家', style)


if __name__ == '__main__':
    unittest.main()
