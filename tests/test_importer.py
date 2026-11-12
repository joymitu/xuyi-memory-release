import unittest

from xuyi import index
from xuyi.importer import import_events, ledger
from xuyi.retrieval import Retriever
from xuyi.store import Store


SCOPE = 'qq:c2c:' + 'a' * 64


def event(mid, text, **extra):
    fields = dict(scope=SCOPE, event_id=mid, sender_id='a' * 64, role='user', origin='quoted',
                  recorded_at='2026-11-07T12:00:00+08:00', text=text)
    return dict(fields, **extra)


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_reimport_is_idempotent_and_keeps_index_fresh(self):
        rows = [event('m1', '请叫我小渔。'), event('m2', '周六见小林。'), event('m3', '我最爱喝茶。')]
        first = import_events(self.store, 'dsh-sessions', rows)
        self.assertEqual(first['totals'], {'inserted': 3, 'duplicate': 0, 'deleted': 0, 'failed': 0})
        self.assertTrue(index.fresh(self.store, SCOPE))
        second = import_events(self.store, 'dsh-sessions', rows)
        self.assertEqual(second['totals']['inserted'], 0)
        self.assertEqual(second['totals']['duplicate'], 3)
        self.assertTrue(index.fresh(self.store, SCOPE))
        self.assertIn('m2', {source['event_id']
                             for source in Retriever(self.store).recall(SCOPE, '小林', 900)['sources']})
        mark = ledger(self.store, 'dsh-sessions')[0]
        self.assertEqual(mark['scope'], SCOPE)
        self.assertEqual(mark['imported'], 3)
        self.assertEqual(mark['duplicates'], 3)

    def test_one_bad_record_never_blocks_the_batch(self):
        rows = [event('m1', '第一条正常消息。'),
                event('m2', ''),
                event('m3', '第三条正常消息。')]
        out = import_events(self.store, 'dsh-sessions', rows)
        self.assertEqual(out['totals']['inserted'], 2)
        self.assertEqual(out['totals']['failed'], 1)
        self.assertEqual(out['failures'][0]['event_id'], 'm2')
        self.assertIsNotNone(self.store.get(SCOPE, 'm3'))
        self.assertTrue(index.fresh(self.store, SCOPE))

    def test_watermark_advances_only_with_commits(self):
        rows = [event(f'm{i:03d}', f'第{i}条消息。') for i in range(7)]
        out = import_events(self.store, 'dsh-sessions', rows, batch=3)
        mark = ledger(self.store, 'dsh-sessions')[0]
        self.assertEqual(mark['watermark'], 'm006')
        self.assertEqual(mark['imported'], 7)
        self.assertEqual(out['totals']['inserted'], 7)

    def test_deleted_events_reported_not_resurrected(self):
        self.store.add(event('m1', '稍后删除的消息。'))
        self.store.forget(SCOPE, 'm1')
        out = import_events(self.store, 'dsh-sessions', [event('m1', '稍后删除的消息。')])
        self.assertEqual(out['totals']['deleted'], 1)
        self.assertIsNone(self.store.get(SCOPE, 'm1'))


if __name__ == '__main__':
    unittest.main()
