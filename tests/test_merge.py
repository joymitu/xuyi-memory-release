import unittest

from xuyi import index
from xuyi.merge import merge_bundle
from xuyi.retrieval import Retriever
from xuyi.store import Store


TARGET = 'qq:c2c:' + 'c' * 64
SOURCE = 'qq:c2c:' + 'd' * 64


def event(scope, mid, text, **extra):
    fields = dict(scope=scope, event_id=mid, sender_id=scope.split(':')[-1], role='user',
                  origin='quoted', recorded_at='2026-11-07T12:00:00+08:00', text=text)
    return dict(fields, **extra)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))
        self.source = self.enterContext(Store(':memory:'))

    def bundle(self):
        return self.source.export_bundle(SOURCE)

    def test_merge_rescopes_events_and_stays_idempotent(self):
        self.source.add(event(SOURCE, 'dsh:s1:m1', '请叫我小渔。'))
        self.source.add(event(SOURCE, 'dsh:s1:m2', '周六见小林。'))
        first = merge_bundle(self.store, self.bundle(), TARGET, provenance='双设备合并')
        self.assertEqual(first['counts']['inserted'], 2)
        self.assertEqual(first['source_scope'], SOURCE)
        out = Retriever(self.store).recall(TARGET, '周六', 900)
        self.assertIn('周六见小林', out['context'])
        self.assertTrue(index.fresh(self.store, TARGET))
        second = merge_bundle(self.store, self.bundle(), TARGET)
        self.assertEqual(second['counts']['inserted'], 0)
        self.assertEqual(second['counts']['duplicate'], 2)
        self.assertEqual(len(self.store.events(TARGET)), 2)

    def test_tombstone_union_widens_deletions(self):
        self.source.add(event(SOURCE, 'dsh:s1:m1', '待删除消息。'))
        stale = self.bundle()
        merge_bundle(self.store, self.bundle(), TARGET)
        self.source.forget(SOURCE, 'dsh:s1:m1')
        merge_bundle(self.store, self.bundle(), TARGET)
        self.assertIsNone(self.store.get(TARGET, 'dsh:s1:m1'))
        # A stale bundle captured before the deletion must not resurrect it.
        report = merge_bundle(self.store, stale, TARGET)
        self.assertEqual(report['counts']['skipped_deleted'], 1)
        self.assertIsNone(self.store.get(TARGET, 'dsh:s1:m1'))

    def test_conflicting_event_keeps_target_and_reports(self):
        self.store.add(event(TARGET, 'dsh:s1:m1', '目标版本的原话。'))
        self.source.add(event(SOURCE, 'dsh:s1:m1', '来源版本的原话。'))
        out = merge_bundle(self.store, self.bundle(), TARGET)
        self.assertEqual(out['counts']['conflict'], 1)
        self.assertEqual(out['conflicts'][0]['source_text'], '来源版本的原话。')
        self.assertEqual(self.store.get(TARGET, 'dsh:s1:m1')['text'], '目标版本的原话。')
        self.assertEqual(len(self.store.events(TARGET)), 1)

    def test_same_scope_refused_and_state_head_untouched(self):
        self.source.add(event(SOURCE, 'dsh:s1:m1', '请叫我小渔。'))
        self.store.add(event(TARGET, 'dsh:t1:m1', '已有消息。'))
        with self.assertRaises(Exception):
            merge_bundle(self.store, self.bundle(), SOURCE)
        self.assertEqual(len(self.store.events(TARGET)), 1)
        report = merge_bundle(self.store, self.bundle(), TARGET)
        self.assertEqual(report['source_state_versions'], 0)
        self.assertIn('状态头未改动', report['note'])


if __name__ == '__main__':
    unittest.main()
