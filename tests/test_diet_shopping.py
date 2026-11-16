"""模拟对话发现的：「买点水果吧，你说买啥好」没说「推荐」，也要带出「芒果过敏」。"""
import unittest

from test_preference_phrasing import Base


class ShoppingForFoodTests(Base):
    def test_buying_fruit_brings_the_allergy(self):
        self.archive.say('我对芒果过敏，一吃嘴就肿。')
        self.assertIn('芒果', self.auto('买点水果吧，你说买啥好'))

    def test_ordering_milk_tea_brings_what_i_cannot_drink(self):
        self.archive.say('我乳糖不耐受，喝牛奶会拉肚子。')
        self.assertIn('乳糖', self.auto('点杯奶茶吧'))

    def test_buying_a_phone_is_not_about_food(self):
        self.archive.say('我对芒果过敏，一吃嘴就肿。')
        self.assertNotIn('芒果', self.auto('今天买了个新手机'))


if __name__ == '__main__':
    unittest.main()
