"""拷问集第八批开发集：没有语义模型时也认「数字变了」（存了2万→存到5万了；十二点睡→改成十一点睡），
单位字（点/万）不能把两句连起来，「我一」这种跨词碎片不当证据，自动提醒不递她的回应。"""
import unittest

from test_preference_phrasing import SCOPE
from test_round4_wording import WordingBase
from xuyi.retrieval import Retriever, same_subject


class NumberChangeTests(WordingBase):
    def auto(self, query):
        return Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True, about_me=True)['context']

    def test_savings_without_a_subject_is_the_newer_amount(self):
        self.chat('我在存钱，现在存了2万。', '存到5万了。')
        for context in (self.auto('我存了多少钱了？'), self.look('我存了多少钱了？')):
            self.assertIn('5万', context)
            old = [line for line in context.split('\n') if '2万' in line]
            self.assertTrue(all('以新的为准' in line for line in old), old)

    def test_bedtime_change_marks_the_old_time(self):
        self.chat('我每天晚上十二点才睡。', '我最近改成十一点睡了。')
        for context in (self.auto('我一般几点睡？'), self.look('我一般几点睡？')):
            self.assertIn('十一点', context)
            old = [line for line in context.split('\n') if '十二点' in line]
            self.assertTrue(all('以新的为准' in line for line in old), old)

    def test_a_unit_character_does_not_link_two_lines(self):
        self.assertFalse(same_subject('我最近改成十一点睡了。', '今天加班到九点，好累。', '我一般几点睡？'))
        self.assertTrue(same_subject('我在存钱，现在存了2万。', '存到5万了。', '我存了多少钱了？'))

    def test_a_cross_word_fragment_does_not_bring_a_sad_line(self):
        self.archive.say('我前段时间查出来有点抑郁。').say('我一直在', role='assistant')
        self.chat('我每天晚上十二点才睡。')
        self.assertNotIn('抑郁', self.auto('我一般几点睡？'))
        self.assertNotIn('我一直在', self.look('我一般几点睡？'))   # 她的回应不靠「我一」被猜中

    def test_new_hobby_finds_got_into(self):
        self.chat('我最近迷上了做饭，昨天做了红烧肉。')
        self.assertIn('做饭', self.look('我最近有什么新爱好？'))


if __name__ == '__main__':
    unittest.main()
