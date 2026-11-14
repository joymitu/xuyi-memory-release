"""拷问集第五批开发集暴露的两处：「最讨厌」和「最爱」抢同一个答案；「别再叫我X，叫我Y」不算改口。"""
import unittest

from test_preference_phrasing import Base


class HateTests(Base):
    def test_hated_food_does_not_compete_with_favourite_food(self):
        self.archive.say('我最爱吃火锅。').say('不对，现在是烤肉了。').say('我最讨厌吃香菜。', minutes=60 * 24)
        self.assertEqual(self.current('我最爱吃啥？'), '烤肉')
        self.assertEqual(self.current('我最讨厌吃什么？'), '香菜')
        self.assertNotIn('香菜', self.auto('我最爱吃啥？'))

    def test_hated_category_named_form(self):
        self.archive.say('我最喜欢的颜色是蓝色').say('我最讨厌的颜色是粉色')
        self.assertEqual(self.current('我最喜欢什么颜色？'), '蓝色')
        self.assertEqual(self.current('我最讨厌什么颜色？'), '粉色')

    def test_now_is_follows_the_liked_category_not_the_hated_one(self):
        self.archive.say('我最爱吃火锅').say('我最讨厌吃香菜').say('现在是烤肉了')
        self.assertEqual(self.current('我最爱吃啥？'), '烤肉')
        self.assertEqual(self.current('我最讨厌吃什么？'), '香菜')


class NicknameCorrectionTests(Base):
    def test_dont_call_me_x_call_me_y_replaces_the_old_nickname(self):
        self.archive.say('我叫李想，叫我小李就行。').say('别再叫我小李了，叫我阿想。', minutes=60 * 24)
        for question in ('你应该叫我什么？', '你平时怎么称呼我？'):
            with self.subTest(question=question):
                self.assertEqual(self.current(question), '阿想')
                self.assertNotIn('小李就行', self.auto(question))

    def test_plain_second_nickname_still_aborts(self):
        self.archive.say('叫我小李就行。').say('叫我阿想吧。', minutes=60 * 24)
        self.assertIsNone(self.current('你应该叫我什么？'))


if __name__ == '__main__':
    unittest.main()
