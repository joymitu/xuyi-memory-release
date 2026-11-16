"""自动提醒第二步：问的是用户自己的事、六类重点都不管时，自动带上和问题有实质共同词的原话。"""
import unittest

from test_preference_phrasing import SCOPE
from test_round4_wording import WordingBase
from xuyi.retrieval import ABOUT_ME_BUDGET, Retriever


class AboutMeTests(WordingBase):
    def auto(self, query, about_me=True):
        return Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True, about_me=about_me)['context']

    def test_a_plain_fact_about_me_comes_back_without_the_tool(self):
        self.chat('我老公叫王磊，是程序员。', '我身高165。')
        self.assertIn('王磊', self.auto('我老公叫啥？'))
        self.assertIn('165', self.auto('我身高多少？'))

    def test_off_without_the_flag(self):
        self.chat('我老公叫王磊，是程序员。')
        self.assertEqual(self.auto('我老公叫啥？', about_me=False), '')

    def test_nothing_shared_means_nothing_injected(self):
        self.chat('我老公叫王磊，是程序员。')
        self.assertEqual(self.auto('我血型是什么？'), '')   # 没说过：一条猜的都不给

    def test_questions_about_her_or_others_inject_nothing(self):
        self.chat('我最喜欢的零食是辣条。', '我女朋友叫小雨。')
        self.assertEqual(self.auto('你最喜欢什么零食？'), '')

    def test_it_stays_small(self):
        self.chat(*[f'我的第{i}只猫叫团子{i}号，超级粘人，每天晚上都要睡在我枕头边上。' for i in range(20)])
        context = self.auto('我的猫都叫什么？')
        self.assertTrue(context)
        self.assertLessEqual(len(context.encode('utf-8')), ABOUT_ME_BUDGET)
        self.assertNotIn('别顺着改口', context)   # 工具那句规矩不进自动提醒

    def test_a_family_highlight_still_wins(self):
        self.archive.say('我最爱的水果是芒果。')
        self.assertIn('unverified_current_highlight', self.auto('我最爱的水果是什么？'))


if __name__ == '__main__':
    unittest.main()
