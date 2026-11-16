"""拷问集第七批：问法和原话用的词不一样（发型/头发、打算/决定、视力/近视、周末/周六），
单字实词（「我的猫都叫什么」→「我有两只猫」），一字不差的重复闲聊只留一条。"""
import unittest

from test_preference_phrasing import Base, SCOPE
from xuyi.retrieval import Retriever, same_thing

FILLER = [('下班路上看到一只橘猫在晒太阳。', '橘猫都好胖哈哈'), ('我新买的耳机音质还不错。', '什么牌子的'),
          ('今天地铁好挤，差点没挤上去。', '辛苦啦'), ('楼下新开了一家奶茶店，排好长的队。', '下次去试试')]


class WordingBase(Base):
    def chat(self, *facts):
        for fact in facts:
            for _ in range(3):
                for say, reply in FILLER:
                    self.archive.say(say, minutes=60).say(reply, role='assistant')
            self.archive.say(fact, minutes=60).say('嗯嗯', role='assistant')
        for i in range(30):   # 真实档案里绝大多数话和问题无关
            self.archive.say(f'今天第{i}次去超市买菜，人好多。', minutes=60)
        return self

    def look(self, query):
        return Retriever(self.store).recall(SCOPE, query, 1600, focus=True)['context']


class SameThingTests(WordingBase):
    CASES = [
        ('我把头发剪短了。', '我最近换发型了吗？', '剪短'),
        ('我想养只猫，还没决定。', '我最近有什么打算？', '养只猫'),
        ('我戴隐形眼镜，近视六百度。', '我视力怎么样？', '六百度'),
        ('周六要带朵朵去打疫苗。', '周末我要干嘛？', '疫苗'),
        ('我不喝咖啡，喝了心慌。', '给我推荐点提神的饮料呗', '不喝咖啡'),
        ('我膝盖受过伤，不能跑步。', '推荐个适合我的运动吧', '膝盖'),
        ('我在追《漫长的季节》，太好看了。', '我最近在看什么电视剧？', '漫长的季节'),
        ('我女儿叫朵朵，今年上幼儿园。', '我闺女叫什么？', '朵朵'),
    ]

    def test_a_different_word_for_the_same_thing_still_finds_the_line(self):
        for fact, query, want in self.CASES:
            with self.subTest(query=query):
                self.setUp()
                self.chat(fact)
                self.assertIn(want, self.look(query))

    def test_groups_only_add_words_the_question_does_not_have(self):
        self.assertIn('头发', same_thing('我换发型了吗'))
        self.assertNotIn('发型', same_thing('我换发型了吗'))
        self.assertEqual(same_thing('今天好累'), [])


class SingleCharacterTests(WordingBase):
    def test_my_cats_names_come_back_from_a_line_without_a_shared_word(self):
        self.chat('我有两只猫，一只叫可乐一只叫雪碧。', '我的猫可乐绝育了。')
        context = self.look('我的猫都叫什么？')
        self.assertIn('雪碧', context)

    def test_a_single_character_only_counts_in_my_own_statements(self):
        self.chat('我有两只猫，一只叫可乐一只叫雪碧。')
        self.assertNotIn('橘猫', self.look('我有几只猫？'))   # 路上看到的猫不是「我的猫」

    def test_question_words_do_not_pull_in_chit_chat(self):
        self.chat('我车是白色的特斯拉。')
        lines = self.look('我开的什么车？').split('\n')[3:]
        self.assertIn('特斯拉', lines[0])
        self.assertFalse(any('什么牌子的' in line for line in lines), lines)


class RepeatTests(WordingBase):
    def test_the_same_chit_chat_said_many_times_shows_once(self):
        self.chat('我把头发剪短了。')
        context = self.look('奶茶店排队吗？')
        self.assertEqual(context.count('楼下新开了一家奶茶店'), 1, context)


if __name__ == '__main__':
    unittest.main()
