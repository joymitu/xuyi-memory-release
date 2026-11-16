"""她主动查记忆时问的是她自己（「你最喜欢什么零食」）：用户说的「我最喜欢辣条」不能递给她当自己的。"""
import unittest

from test_round4_wording import WordingBase


class AsksHerTests(WordingBase):
    def test_users_own_favourite_is_not_handed_to_her(self):
        self.chat('我最喜欢的零食是辣条。')
        self.assertNotIn('辣条', self.look('你最喜欢什么零食？'))

    def test_her_own_words_still_come_back(self):
        self.chat('你最喜欢吃什么零食呀')
        self.archive.say('我最爱吃海苔，咔嚓咔嚓的', role='assistant')
        self.assertIn('海苔', self.look('你最喜欢什么零食？'))

    def test_asking_whether_she_remembers_mine_still_finds_mine(self):
        self.chat('我最喜欢的零食是辣条。')
        self.assertIn('辣条', self.look('你还记得我最喜欢什么零食吗'))


if __name__ == '__main__':
    unittest.main()
