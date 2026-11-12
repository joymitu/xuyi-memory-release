import unittest

from xuyi.audit import audit
from xuyi.store import Store


SCOPE = 'qq:c2c:' + 'a' * 64


def event(mid, text, **extra):
    fields = dict(scope=SCOPE, event_id=mid, sender_id='a' * 64, role='user', origin='quoted',
                  recorded_at='2026-11-07T12:00:00+08:00', text=text)
    return dict(fields, **extra)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_reports_only_structurally_skipped_statements(self):
        self.store.add_many([
            event('m1', '请叫我小渔。'),
            event('m2', '[Quoted message begins]\n请叫我小渔。\n[Current message]\n在吗'),
            event('m3', '更正一下，我最喜欢的饮料是气泡水，昨天说成茶了，抱歉哈，今天天气不错。' * 3),
            event('m4', '今天天气怎么样？'),
            event('m5', '我喜欢养绿萝。', role='assistant', origin='assistant'),
        ])
        out = audit(self.store, SCOPE)
        ids = [row['event_id'] for row in out['skipped']]
        self.assertEqual(ids, ['m2', 'm3'])
        self.assertIn('复合', out['skipped'][0]['reason'])
        self.assertIn('超长', out['skipped'][1]['reason'])
        self.assertEqual(out['scanned'], 5)

    def test_scope_without_triggers_is_quiet(self):
        self.store.add(event('m1', '今晚吃什么好呢'))
        out = audit(self.store, SCOPE)
        self.assertEqual((out['scanned'], out['triggered']), (1, 0))


if __name__ == '__main__':
    unittest.main()
