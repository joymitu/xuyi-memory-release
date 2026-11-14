"""问的是谁：问机器人自己（你最喜欢…）、问别人（我妈最喜欢…）时，不能把用户自己的记忆当答案塞进去。"""
import unittest

from test_preference_phrasing import Base


class QuestionOwnerTests(Base):
    def setUp(self):
        super().setUp()
        for text in ('我最喜欢的颜色是蓝色。', '我家的猫叫汤圆。', '我最喜欢喝奶茶。', '周六见小林。',
                     '以后叫我阿想。', '我对芒果过敏。'):
            self.archive.say(text, minutes=60 * 24)

    def test_questions_about_someone_else_get_nothing(self):
        for question in ('你最喜欢什么颜色？', '你最喜欢喝什么？', '你周六有什么安排？', '你还喝奶茶吗？',
                         '我妈最喜欢什么颜色？', '他最喜欢喝什么', '我男朋友周六有什么安排？', '你能吃芒果吗？'):
            with self.subTest(question=question):
                self.assertEqual(self.auto(question), '')

    def test_questions_about_me_still_work(self):
        cases = {'我最喜欢什么颜色？': '蓝色', '你猜我最喜欢什么颜色': '蓝色', '你还记得我最喜欢喝什么吗？': '奶茶',
                 '你知道我最喜欢的颜色吗': '蓝色', '周六有什么安排？': '小林', '我们周六有什么安排？': '小林',
                 '我能吃芒果吗？': '过敏', '我家猫叫啥？': '汤圆', '你应该叫我什么？': '阿想',
                 '你平时怎么称呼我？': '阿想', '妈妈问我最喜欢什么颜色': '蓝色'}
        for question, value in cases.items():
            with self.subTest(question=question):
                self.assertIn(value, self.auto(question))


if __name__ == '__main__':
    unittest.main()
