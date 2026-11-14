"""「别记了」清单索引：结果必须和全量扫描一致，召回时不再全表模糊匹配（10 万句时 170 毫秒）。"""
import unittest

from xuyi import index
from xuyi.retrieval import Retriever
from xuyi.store import Store
from xuyi.strength import said_forget_ids

PEER = 'e' * 64
SCOPE = 'qq:c2c:' + PEER
LINES = ['我前任叫阿杰。', '刚才那句别记了。', '今天好累', '我家的狗叫旺财，算了这个别记了。',
         '你不会忘了吧', '我最喜欢的颜色是蓝色。', '忘掉吧', '明天上班', '当我没说']


def row(n, text, role='user'):
    return {'scope': SCOPE, 'event_id': f'dsh:f:{n}', 'sender_id': PEER if role == 'user' else 'bot',
            'role': role, 'origin': 'quoted' if role == 'user' else 'assistant',
            'recorded_at': f'2026-11-01T00:{n // 60:02d}:{n % 60:02d}+08:00', 'text': text}


def fill(store, lines=LINES):
    n = 0
    for text in lines:
        for event in (row(n, text), row(n + 1, '嗯嗯', 'assistant')):
            store.add(event)
            index.update(store, SCOPE, [event])
        n += 2


def full_scan(store):
    store.db.execute('DELETE FROM forget_stats')
    return said_forget_ids(store, SCOPE)


class ForgetIndexTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def queries(self, call):
        seen = []
        self.store.db.set_trace_callback(seen.append)
        try:
            result = call()
        finally:
            self.store.db.set_trace_callback(None)
        return result, seen

    def test_incremental_list_matches_the_full_scan(self):
        fill(self.store)
        self.assertIsNotNone(index.forget_seqs(self.store, SCOPE))
        indexed = said_forget_ids(self.store, SCOPE)
        self.assertEqual(indexed, full_scan(self.store))
        # 「刚才那句别记了」连带上一句；「你不会忘了吧」是反问，不算。
        self.assertIn('dsh:f:0', indexed)
        self.assertNotIn('dsh:f:8', indexed)

    def test_fresh_list_skips_the_table_wide_like_scan(self):
        fill(self.store)
        said_forget_ids(self.store, SCOPE)
        _, seen = self.queries(lambda: said_forget_ids(self.store, SCOPE))
        self.assertFalse([sql for sql in seen if 'LIKE' in sql], seen)

    def test_old_archive_heals_on_first_recall(self):
        fill(self.store)
        expected = said_forget_ids(self.store, SCOPE)
        self.store.db.execute('DROP TABLE forget_lines')
        self.store.db.execute('DROP TABLE forget_stats')   # 1.0.1 的档案没有这两张表
        self.assertIsNone(index.forget_seqs(self.store, SCOPE))
        first, seen = self.queries(lambda: said_forget_ids(self.store, SCOPE))
        self.assertTrue([sql for sql in seen if 'LIKE' in sql])
        self.assertEqual(first, expected)
        _, seen = self.queries(lambda: said_forget_ids(self.store, SCOPE))
        self.assertFalse([sql for sql in seen if 'LIKE' in sql])

    def test_gap_in_incremental_updates_stays_stale(self):
        fill(self.store)
        missed = row(100, '这句也别记了')
        self.store.add(missed)                       # 写进档案却没进索引
        later = row(101, '今天好热')
        self.store.add(later)
        index.update(self.store, SCOPE, [later])
        # 只追加会漏掉 100；写入时发现断档就整个重建，清单里必须有它。
        self.assertIsNotNone(index.forget_seqs(self.store, SCOPE))
        self.assertIn('dsh:f:100', said_forget_ids(self.store, SCOPE))
        self.assertEqual(said_forget_ids(self.store, SCOPE), full_scan(self.store))

    def test_deleting_the_request_brings_the_line_back(self):
        fill(self.store)
        self.assertIn('dsh:f:0', said_forget_ids(self.store, SCOPE))
        self.store.forget(SCOPE, 'dsh:f:2')          # 删掉「刚才那句别记了」
        index.forget(self.store, SCOPE, 'dsh:f:2')
        self.assertIsNone(index.forget_seqs(self.store, SCOPE))
        self.assertNotIn('dsh:f:0', said_forget_ids(self.store, SCOPE))

    def test_rebuild_refills_the_list(self):
        fill(self.store)
        expected = said_forget_ids(self.store, SCOPE)
        index.rebuild(self.store, SCOPE)
        self.assertIsNotNone(index.forget_seqs(self.store, SCOPE))
        self.assertEqual(said_forget_ids(self.store, SCOPE), expected)

    def test_forgotten_line_still_hidden_from_auto_recall(self):
        fill(self.store, ['我最喜欢的颜色是蓝色。', '刚才那句别记了。'])
        out = Retriever(self.store).recall(SCOPE, '我最喜欢什么颜色？', 900, highlight_only=True)
        self.assertNotIn('蓝色', out['context'])

    def test_latest_seq_uses_the_scope_index(self):
        plan = self.store.db.execute(
            'EXPLAIN QUERY PLAN SELECT seq FROM events WHERE scope=? ORDER BY seq DESC LIMIT 1',
            (SCOPE,)).fetchall()
        detail = ' '.join(str(tuple(step)) for step in plan)
        self.assertIn('events_by_scope_seq', detail)
        self.assertNotIn('TEMP B-TREE', detail)


if __name__ == '__main__':
    unittest.main()
