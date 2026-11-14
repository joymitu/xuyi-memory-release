"""1.0.2 两个记忆短板：单独一句的喜好（各种问法/说法）、不带「改成」的改口。"""
import unittest
from datetime import datetime, timedelta, timezone

from xuyi import index
from xuyi.retrieval import Retriever
from xuyi.store import Store

PEER = 'f' * 64
SCOPE = 'qq:c2c:' + PEER
START = datetime(2026, 11, 1, 20, 0, tzinfo=timezone(timedelta(hours=8)))
OTHERS = ['我最喜欢喝奶茶。', '我最爱的动物是猫。']   # 档案里还有别的喜好，问题必须分得清类别


class Archive:
    def __init__(self, store):
        self.store, self.n = store, 0

    def say(self, text, minutes=1, role='user'):
        self.n += 1
        at = START + timedelta(minutes=self.n * minutes) if isinstance(minutes, int) else minutes
        event = {'scope': SCOPE, 'event_id': f'dsh:p:{self.n}', 'role': role,
                 'sender_id': PEER if role == 'user' else 'bot',
                 'origin': 'quoted' if role == 'user' else 'assistant',
                 'recorded_at': at.isoformat(), 'text': text}
        self.store.add(event)
        index.update(self.store, SCOPE, [event])
        return self

    def at(self, text, when):
        return self.say(text, minutes=when)


class Base(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))
        self.archive = Archive(self.store)

    def auto(self, query):
        return Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True)['context']

    def current(self, query):
        found = [p for p in Retriever(self.store).recall(SCOPE, query, 900)['sources']
                 if 'unverified_current_highlight' in p]
        return found[0]['unverified_current_highlight']['值'] if found else None


class SingleStatementTests(Base):
    CASES = [
        ('我最爱的水果是芒果', '芒果', ['我最爱的水果是什么？', '我最爱吃什么水果', '你还记得我最爱的水果吗',
                                    '我最喜欢的水果是啥来着', '我最爱的水果是哪个']),
        ('我最喜欢的颜色是蓝色', '蓝色', ['我最喜欢什么颜色？', '我最喜欢的颜色是？', '你记得我最喜欢的颜色吗',
                                      '我喜欢什么颜色']),
        ('我最喜欢的歌手是周深', '周深', ['我最喜欢的歌手是谁？', '我最喜欢哪个歌手', '还记得我最喜欢的歌手吗']),
        ('对了，我最爱的水果就是芒果！', '芒果', ['我最爱的水果是什么？']),
        ('我最喜欢蓝色', '蓝色', ['我最喜欢什么颜色？']),
        ('我最爱吃的水果是芒果', '芒果', ['我最爱的水果是什么？', '我最爱吃什么水果']),
        ('我最喜欢的电影是千与千寻', '千与千寻', ['我最喜欢的电影是哪部？']),
        ('我最喜欢的季节是秋天', '秋天', ['我最喜欢哪个季节？', '我最喜欢的季节是什么']),
        ('芒果是我最爱的水果', '芒果', ['我最爱的水果是什么？']),
    ]

    def test_said_once_among_other_tastes_is_recalled(self):
        for said, value, questions in self.CASES:
            for question in questions:
                with self.subTest(said=said, question=question):
                    store = self.enterContext(Store(':memory:'))
                    archive = Archive(store)
                    for text in (OTHERS[0], said, OTHERS[1]):
                        archive.say(text)
                    context = Retriever(store).recall(SCOPE, question, 900, highlight_only=True)['context']
                    self.assertIn(value, context)

    def test_other_categories_are_not_confused(self):
        for text in (OTHERS[0], '我最爱的水果是芒果', OTHERS[1]):
            self.archive.say(text)
        self.assertEqual(self.current('我最喜欢的歌手是谁？'), None)   # 没说过就不猜
        self.assertEqual(self.current('我最喜欢喝什么？'), '奶茶')
        self.assertEqual(self.current('我最爱的动物是什么'), '猫')

    def test_questions_about_someone_else_stay_out(self):
        self.archive.say('我最爱的水果是芒果')
        self.assertEqual(self.auto('我朋友最爱的水果是什么？'), '')

    def test_plain_competing_values_still_abort(self):
        self.archive.say('我最喜欢的歌手是周深').say('我最喜欢的歌手是毛不易', minutes=60 * 24)
        self.assertIsNone(self.current('我最喜欢的歌手是谁？'))


class UnmarkedChangeTests(Base):
    def test_now_is_right_after_a_taste_is_a_change(self):
        self.archive.say('我最爱吃芒果').say('现在是草莓了')
        self.assertEqual(self.current('我最爱吃什么？'), '草莓')
        self.assertNotIn('我最爱吃芒果', self.auto('我最爱吃什么？'))

    def test_variants_of_now(self):
        for later in ('不过现在是草莓了', '现在换成草莓了', '现在变成草莓了'):
            with self.subTest(later=later):
                store = self.enterContext(Store(':memory:'))
                Archive(store).say('我最爱的水果是芒果').say(later)
                found = [p for p in Retriever(store).recall(SCOPE, '我最爱的水果是什么？', 900)['sources']
                         if 'unverified_current_highlight' in p]
                self.assertEqual(found[0]['unverified_current_highlight']['值'], '草莓')

    def test_then_and_now_in_one_sentence(self):
        for said, question in (('我以前最爱吃芒果，现在是草莓了', '我最爱吃什么？'),
                               ('我以前最爱的水果是芒果，现在是草莓了', '我最爱的水果是什么？'),
                               ('以前最喜欢喝奶茶，现在改喝美式了', '我最喜欢喝什么？')):
            with self.subTest(said=said):
                store = self.enterContext(Store(':memory:'))
                Archive(store).say(said)
                found = [p for p in Retriever(store).recall(SCOPE, question, 900)['sources']
                         if 'unverified_current_highlight' in p]
                self.assertEqual(found[0]['unverified_current_highlight']['值'],
                                 '美式' if '美式' in said else '草莓')

    def test_then_and_now_replaces_an_earlier_statement(self):
        self.archive.say('我最爱的水果是芒果').say('我以前最爱的水果是芒果，现在是草莓了', minutes=60 * 24)
        self.assertEqual(self.current('我最爱的水果是什么？'), '草莓')

    def test_times_seasons_and_roles_are_not_tastes(self):
        for later in ('现在是下午三点了', '现在是冬天了', '现在是周末了', '现在是夜班了', '现在是大学生了',
                      '现在是不是草莓了', '我妈现在是草莓了'):
            with self.subTest(later=later):
                store = self.enterContext(Store(':memory:'))
                Archive(store).say('我最爱的水果是芒果').say(later)
                found = [p for p in Retriever(store).recall(SCOPE, '我最爱的水果是什么？', 900)['sources']
                         if 'unverified_current_highlight' in p]
                self.assertEqual(found[0]['unverified_current_highlight']['值'], '芒果')

    def test_now_is_long_after_is_not_a_change(self):
        self.archive.say('我最爱的水果是芒果').at('现在是草莓了', START + timedelta(days=3))
        self.assertEqual(self.current('我最爱的水果是什么？'), '芒果')

    def test_now_is_without_any_taste_is_ignored(self):
        self.archive.say('今天好累').say('现在是草莓了')
        self.assertEqual(self.auto('我最爱的水果是什么？'), '')

    def test_now_is_follows_the_most_recent_category(self):
        self.archive.say('我最喜欢的颜色是蓝色', minutes=60).say('我最爱的水果是芒果').say('现在是草莓了')
        self.assertEqual(self.current('我最爱的水果是什么？'), '草莓')
        self.assertEqual(self.current('我最喜欢什么颜色？'), '蓝色')


class IndexParityTests(Base):
    def test_new_phrasings_reach_the_highlight_index(self):
        lines = ['芒果是我最爱的水果', '我最喜欢蓝色', '我最爱吃的水果是芒果', '现在是草莓了',
                 '我以前最爱吃芒果，现在是草莓了']
        for text in lines:
            self.archive.say(text)
        self.assertTrue(index.highlight_fresh(self.store, SCOPE))
        indexed = {event['text'] for event in index.highlight_events(self.store, SCOPE)}
        self.assertEqual(indexed, set(lines))

    def test_archives_indexed_by_older_rules_rebuild_on_next_write(self):
        self.archive.say('芒果是我最爱的水果')
        # 模拟 1.0.1 建的索引：新句式不在里面，且没有规则版本号。
        self.store.db.execute('DELETE FROM highlight_lines')
        self.store.db.execute('DELETE FROM highlight_rules')
        self.assertFalse(index.highlight_fresh(self.store, SCOPE))
        self.assertIn('芒果', self.auto('我最爱的水果是什么？'))   # 过期时走全量，照样找得到
        self.archive.say('今天好累')                                  # 下一条消息写入时重建
        self.assertTrue(index.highlight_fresh(self.store, SCOPE))
        self.assertIn('芒果', self.auto('我最爱的水果是什么？'))


if __name__ == '__main__':
    unittest.main()
