"""拷问集第六批开发集：「不是X，是Y」改口、问上班地点、相关旧话回退带出新说法、回退时分清问的是谁。"""
import unittest
from datetime import timedelta

from test_preference_phrasing import Base, START
from xuyi.retrieval import Retriever, auto_remember

DAY = 60 * 24


class NotButTests(Base):
    def test_not_x_but_y_corrects_the_current_value(self):
        self.archive.say('我最喜欢的零食是薯片。').say('不是薯片，是辣条，说错了。')
        self.assertEqual(self.current('我最喜欢的零食是啥？'), '辣条')
        self.assertNotIn('我最喜欢的零食是薯片', self.auto('我最喜欢的零食是啥？'))

    def test_not_x_but_y_works_days_later_too(self):
        self.archive.say('我最喜欢的零食是薯片。').at('不对，不是薯片，应该是辣条。', START + timedelta(days=5))
        self.assertEqual(self.current('我最喜欢的零食是啥？'), '辣条')

    def test_not_x_but_y_needs_x_to_be_the_remembered_value(self):
        for later in ('不是周六，是周日，说错了。', '不是我，是我同事。', '不是蓝色，是绿色。'):
            with self.subTest(later=later):
                store = self.enterContext(type(self.store)(':memory:'))
                from test_preference_phrasing import Archive
                Archive(store).say('我最喜欢的零食是薯片。').say(later)
                found = [p for p in Retriever(store).recall(self.scope, '我最喜欢的零食是啥？', 900)['sources']
                         if 'unverified_current_highlight' in p]
                self.assertEqual(found[0]['unverified_current_highlight']['值'], '薯片')

    @property
    def scope(self):
        from test_preference_phrasing import SCOPE
        return SCOPE


class WorkplaceTests(Base):
    def test_where_do_i_work_finds_the_new_company(self):
        self.archive.say('我公司在望京SOHO。').say('今天好累', minutes=DAY).say('我换工作了，新公司在国贸。', minutes=DAY)
        from test_preference_phrasing import SCOPE
        context = Retriever(self.store).recall(SCOPE, '我现在在哪上班？', 1600, focus=True)['context']
        self.assertIn('国贸', context)
        old = [line for line in context.split('\n') if '望京' in line]
        self.assertTrue(all('以新的为准' in line for line in old), old)


class RelatedFallbackTests(Base):
    def related(self, query):
        from test_preference_phrasing import SCOPE
        return Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True, related=True)['context']

    def test_related_fallback_brings_the_newer_statement(self):
        self.archive.say('我现在住在杭州滨江。').say('今天好累', minutes=DAY).say('搬完家了，现在住上海徐汇。', minutes=DAY)
        context = self.related('我现在住在哪？')
        self.assertIn('上海', context)
        self.assertNotIn('杭州', context)   # 确定搬过家了：只给新的那句

    def test_unclear_follow_up_keeps_both_lines(self):
        self.archive.say('我养了只柯基叫豆豆。').say('今天好累', minutes=DAY).say('豆豆最近老是咳嗽。', minutes=DAY)
        context = self.related('柯基好可爱')
        self.assertIn('柯基', context)
        self.assertIn('咳嗽', context)
        self.assertIn('后来又提到过', context)

    def test_related_fallback_ignores_questions_about_her(self):
        self.archive.say('我最喜欢的零食是辣条。')
        self.assertEqual(self.related('你最喜欢什么零食？'), '')

    def test_plain_chat_still_triggers_the_fallback(self):
        from test_preference_phrasing import SCOPE
        self.archive.say('我养了只柯基叫豆豆。').say('今天好累', minutes=DAY)
        self.assertIsNotNone(auto_remember(self.store, SCOPE, '豆豆又拆家了'))


if __name__ == '__main__':
    unittest.main()
