import random
import unittest

from test_store import event
from xuyi import index
from xuyi.retrieval import Retriever
from xuyi.store import Store


SCOPE = 'private:U1'


class IndexParityTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))
        self.brute = self.enterContext(Store(':memory:'))

    def add_both(self, texts):
        for seq, text in enumerate(texts, 1):
            row = event(f'm{seq}', text)
            self.store.add(row)
            self.brute.add(row)

    def recall(self, query, **options):
        return Retriever(self.store).recall(SCOPE, query, **options)

    def test_ranking_and_context_match_brute_force(self):
        rng = random.Random(3)
        texts = ['我的猫叫甲号。', '周六一起去看电影，可能要迟到。', 'Remember the coffee order, please!',
                 '我家的狗叫乙号，上次说错了。', '周五晚上一起吃饭，带上吉他。', '项目下周三截稿，别忘了。',
                 '更正一下，我的猫叫丙号。', 'bike ride along the river tomorrow morning']
        texts += ['杂项消息%d，%s。' % (i, rng.choice(['音乐', '咖啡', '高铁', '公园'])) for i in range(40)]
        self.add_both(texts)
        index.rebuild(self.store, SCOPE)
        self.assertTrue(index.fresh(self.store, SCOPE))
        for query in ('我的猫叫什么？', 'coffee order', '周六做什么', '高铁', '记得什么', '乙号'):
            indexed = self.recall(query, budget=1600)
            scan = Retriever(self.brute).recall(SCOPE, query, 1600)
            self.assertEqual(indexed['context'], scan['context'], query)
            self.assertEqual([p['event_id'] for p in indexed['sources']],
                             [p['event_id'] for p in scan['sources']], query)
            self.assertEqual(indexed['candidate_chunks'], scan['candidate_chunks'])

    def test_fallback_after_delete_and_rebuild(self):
        self.add_both(['我的猫叫甲号。', '周六一起吃饭。', '我家的狗叫乙号。'])
        index.rebuild(self.store, SCOPE)
        self.assertTrue(index.fresh(self.store, SCOPE))
        self.store.forget(SCOPE, 'm2')
        self.assertFalse(index.fresh(self.store, SCOPE))
        out = self.recall('周六一起吃饭')
        self.assertIn('倒排索引不完整', out['warnings'][0])
        index.rebuild(self.store, SCOPE)
        self.assertTrue(index.fresh(self.store, SCOPE))
        self.assertNotIn('倒排索引不完整', self.recall('周六一起吃饭')['warnings'][0])
        self.assertNotIn('周六一起吃饭', self.recall('周六一起吃饭')['context'])

    def test_access_log_records_interactions(self):
        self.add_both(['第一条消息，关于音乐。', '第二条消息，关于咖啡。'])
        index.rebuild(self.store, SCOPE)
        self.recall('音乐')
        rows = self.store.db.execute(
            "SELECT query,event_ids FROM access_log WHERE scope=?", (SCOPE,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], '音乐')
        self.assertIn('m1', rows[0][1])
        self.recall('咖啡')
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM access_log WHERE scope=?", (SCOPE,)).fetchone()[0], 2)

    def test_incremental_ingest_stays_in_order(self):
        self.add_both(['第一条消息。'])
        index.update(self.store, SCOPE, [event('m1', '第一条消息。')])
        self.store.add(event('m2', '第二条新消息，内容不同。'))
        index.update(self.store, SCOPE, [event('m2', '第二条新消息，内容不同。')])
        self.assertTrue(index.fresh(self.store, SCOPE))
        out = self.recall('第二条')
        self.assertEqual(out['sources'][0]['event_id'], 'm2')
        self.assertNotIn('倒排索引不完整', out['warnings'][0])


if __name__ == '__main__':
    unittest.main()


class AutoRememberTests(unittest.TestCase):
    def setUp(self):
        self.scope = 'qq:c2c:' + 'f' * 64
        self.store = self.enterContext(Store(':memory:'))
        for seq, text in enumerate(['周六和小林去爬山。', '完全无关的天气闲聊。'], 1):
            self.store.add(event(f'm{seq}', text, scope=self.scope))
        index.rebuild(self.store, self.scope)

    def test_rare_entity_triggers_topic_recall(self):
        from xuyi.retrieval import auto_remember
        out = auto_remember(self.store, self.scope, '我碰到小林了')
        self.assertIsNotNone(out)
        self.assertIn('爬山', out['context'])
        self.assertIn('unverified_related', str(list(out['sources'][0].keys())))
        self.assertLessEqual(out['used'], 300)

    def test_common_word_only_stays_silent(self):
        from xuyi.retrieval import auto_remember
        self.assertIsNone(auto_remember(self.store, self.scope, '今天天气不错呀'))
        self.assertIsNone(auto_remember(self.store, self.scope, ''))
        self.assertIsNone(auto_remember(self.store, self.scope, '以前和谁爬山来着'))
        self.assertIsNone(auto_remember(self.store, 'private:other', '我碰到小林了'))


class EchoTests(unittest.TestCase):
    """刚说的这句话在召回前已经入档；它不能被当成"想起来的旧事"送回去。"""
    def setUp(self):
        self.scope = 'qq:c2c:' + 'e' * 64
        self.store = self.enterContext(Store(':memory:'))
        for seq, text in enumerate(['周六和小林去爬山。', '完全无关的天气闲聊。', '我碰到小林了'], 1):
            self.store.add(event(f'm{seq}', text, scope=self.scope))
        index.rebuild(self.store, self.scope)

    def test_topic_recall_skips_the_current_message(self):
        from xuyi.retrieval import auto_remember
        out = auto_remember(self.store, self.scope, '我碰到小林了')
        self.assertIsNotNone(out)
        self.assertIn('爬山', out['context'])
        self.assertNotIn('m3', [source['event_id'] for source in out['sources']])

    def test_topic_recall_stays_silent_when_only_the_echo_matches(self):
        from xuyi.retrieval import auto_remember
        self.store.add(event('m4', '我养了一只龙猫', scope=self.scope))
        index.update(self.store, self.scope, [self.store.get(self.scope, 'm4')])
        self.assertIsNone(auto_remember(self.store, self.scope, '我养了一只龙猫'))

    def test_recall_does_not_spend_budget_on_the_question_itself(self):
        from xuyi.retrieval import Retriever
        out = Retriever(self.store).recall(self.scope, '我碰到小林了', 2000)
        self.assertNotIn('m3', [source['event_id'] for source in out['sources']])
        self.assertIn('爬山', out['context'])


class HighlightIndexTests(unittest.TestCase):
    """重点候选索引只是加速：结果必须与全量扫描逐位一致，过期时退回全量扫描。"""
    QUERIES = ['我家猫叫啥来着？', '你叫我什么？', '我最喜欢喝什么？', '我最爱吃什么？', '我周六有什么安排来着？',
               '我和阿珍的约定还作数吗？', '我最喜欢什么颜色？', '我还喝咖啡吗？', '最近有什么打算？']

    def archive(self, turns, filler=0):
        from datetime import datetime, timedelta, timezone
        store = self.enterContext(Store(':memory:'))
        peer = 'c' * 64
        scope = 'qq:c2c:' + peer
        start = datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=8)))
        lines = list(turns)
        for i in range(filler):
            lines.insert(len(lines) // 2, f'今天第{i}次去楼下买东西，挺累的。')
        for n, text in enumerate(lines):
            row = {'scope': scope, 'event_id': f'dsh:s:{n}', 'sender_id': peer, 'role': 'user',
                   'origin': 'quoted', 'recorded_at': (start + timedelta(minutes=n)).isoformat(), 'text': text}
            store.add(row)
            index.update(store, scope, [row])  # 与 CLI ingest 相同的逐条增量路径
        return store, scope

    def assert_parity(self, store, scope):
        from xuyi.highlights import current_highlight
        self.assertTrue(index.highlight_fresh(store, scope))
        full, lines = store.events(scope), index.highlight_events(store, scope)
        self.assertLess(len(lines), len(full) + 1)
        for query in self.QUERIES:
            self.assertEqual(current_highlight(scope, query, lines), current_highlight(scope, query, full), query)

    def test_only_family_lines_are_kept(self):
        store, scope = self.archive(['我家的猫叫年糕。', '数学考试下周考', '数学考完了'], filler=50)
        kept = [event['text'] for event in index.highlight_events(store, scope)]
        self.assertIn('我家的猫叫年糕。', kept)
        self.assertIn('数学考完了', kept)  # 开放环的收尾句也要在
        self.assertFalse(any('买东西' in text for text in kept))
        self.assert_parity(store, scope)

    def test_older_archives_and_deletions_fall_back_to_the_full_scan(self):
        from xuyi.retrieval import Retriever
        store, scope = self.archive(['我家的猫叫年糕。', '更正一下，我家的猫叫汤圆。'])
        store.db.execute('DROP TABLE highlight_lines')
        store.db.execute('DROP TABLE highlight_stats')  # 升级前的档案没有这两张表
        self.assertFalse(index.highlight_fresh(store, scope))
        self.assertIsNone(index.highlight_events(store, scope))
        self.assertIn('汤圆', Retriever(store).recall(scope, '我家猫叫啥', 900, highlight_only=True)['context'])
        row = {'scope': scope, 'event_id': 'dsh:s:9', 'sender_id': 'c' * 64, 'role': 'user', 'origin': 'quoted',
               'recorded_at': '2026-11-02T00:00:00+08:00', 'text': '我最喜欢喝奶茶。'}
        store.add(row)
        index.update(store, scope, [row])
        self.assertFalse(index.highlight_fresh(store, scope))  # 增量不能把缺了历史的索引标成新鲜
        index.rebuild(store, scope)
        self.assert_parity(store, scope)
        store.forget(scope, 'dsh:s:1')
        index.forget(store, scope, 'dsh:s:1')
        self.assertFalse(index.highlight_fresh(store, scope))
        self.assertIn('年糕', Retriever(store).recall(scope, '我家猫叫啥', 900, highlight_only=True)['context'])


class SnapshotScaleTests(unittest.TestCase):
    """开场快照走候选索引 + 尾部窗口后，结果必须与全量扫描完全相同。"""
    def build(self, store, long_current=0):
        from datetime import datetime, timedelta, timezone
        peer = 'd' * 64
        scope = 'qq:c2c:' + peer
        start = datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=8)))
        script = [('s1', '我家的猫叫年糕，超级粘人。'), ('s1', '周六我和小林约好去爬山。'), ('s1', '数学考试下周考'),
                  ('s2', '更正一下，我家的猫不叫年糕，叫汤圆，之前说错了。'), ('s2', '今天去面试了，有点紧张')]
        script += [('s3', f'今天第{i}次去楼下买东西') for i in range(long_current)] + [('s3', '在吗')]
        for n, (session, text) in enumerate(script):
            for role in ('user', 'assistant'):
                row = {'scope': scope, 'event_id': f'dsh:{session}:{n}{role[0]}', 'role': role,
                       'sender_id': peer if role == 'user' else 'assistant',
                       'origin': 'quoted' if role == 'user' else 'assistant',
                       'recorded_at': (start + timedelta(minutes=n)).isoformat(),
                       'text': text if role == 'user' else '嗯嗯'}
                store.add(row)
                index.update(store, scope, [row])
        return scope

    def test_snapshot_matches_the_full_scan(self):
        from datetime import date
        from xuyi.consolidate import snapshot
        for long_current in (0, 700):
            with self.subTest(long_current=long_current):
                indexed = self.enterContext(Store(':memory:'))
                full = self.enterContext(Store(':memory:'))
                scope = self.build(indexed, long_current)
                self.build(full, long_current)
                full.db.execute('DROP TABLE highlight_stats')  # 强制全量扫描
                self.assertTrue(index.highlight_fresh(indexed, scope))
                a = snapshot(indexed, scope, today=date(2026, 11, 6))
                b = snapshot(full, scope, today=date(2026, 11, 6))
                self.assertEqual(a['context'], b['context'])
                self.assertIn('汤圆', a['context'])
                self.assertIn('面试', a['context'])  # 上一个会话的话题，即使当前会话很长
