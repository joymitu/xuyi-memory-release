import unittest
from datetime import datetime, timedelta

from xuyi.store import Store
from xuyi.retrieval import Retriever

from test_store import event

SCOPE = 'qq:c2c:' + 'e' * 64


def days_ago(days, hour=16):
    moment = datetime.now().astimezone() - timedelta(days=days)
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


class DecayRankingTests(unittest.TestCase):
    """排序里的时间/家族权重：只改"先想起谁"，档案一条不动。

    不能"被召回就强化"（会形成寡头），所以这里只按时间与家族给有界权重。
    """

    def build(self, rows):
        store = Store(':memory:')
        for n, (stamp, role, text) in enumerate(rows):
            e = event(f'dsh:d:{n}', text, scope=SCOPE)
            e['recorded_at'] = stamp
            e['role'], e['origin'] = role, ('quoted' if role == 'user' else 'assistant')
            if role == 'assistant':
                e['sender_id'] = 'assistant'
            store.add(e)
        from xuyi import index as xuyi_index
        xuyi_index.rebuild(store, SCOPE)
        return store

    def order(self, store, query, budget=1600):
        out = Retriever(store).recall(SCOPE, query, budget)
        return [piece['text'] for piece in out['sources']]

    def test_recent_memory_wins_when_equally_relevant(self):
        store = self.build([
            (days_ago(300), 'user', '去年去爬山，累得半死。'),
            (days_ago(2), 'user', '昨天去爬山，腿现在还疼。'),
        ])
        try:
            texts = self.order(store, '爬山')
            self.assertEqual(texts[0], '昨天去爬山，腿现在还疼。')
        finally:
            store.close()

    def test_long_lived_facts_are_not_buried_by_chatter(self):
        store = self.build([
            (days_ago(300), 'user', '我对芒果过敏，别点芒果的。'),
            *[(days_ago(n), 'user', f'今天第{n}次刷手机，眼睛疼。') for n in range(1, 6)],
        ])
        try:
            texts = self.order(store, '能点芒果吗')
            self.assertIn('我对芒果过敏，别点芒果的。', texts)
            self.assertEqual(texts[0], '我对芒果过敏，别点芒果的。')
        finally:
            store.close()

    def test_an_old_but_only_match_is_still_found(self):
        store = self.build([
            (days_ago(1500), 'user', '我小时候住的那个院子有棵石榴树。'),
            *[(days_ago(n), 'user', f'今天第{n}次刷手机，眼睛疼。') for n in range(1, 6)],
        ])
        try:
            texts = self.order(store, '石榴树')
            self.assertIn('我小时候住的那个院子有棵石榴树。', texts)
        finally:
            store.close()

    def test_two_very_old_lines_break_the_cap_by_recency(self):
        # 都撞到上限（+4）时，仍然更近的在前——否则会按入库先后、旧的反而排前面。
        store = self.build([
            (days_ago(2000), 'user', '很多年前去爬山，累得半死。'),
            (days_ago(1200), 'user', '后来又一次去爬山，腿疼。'),
        ])
        try:
            texts = self.order(store, '爬山')
            self.assertEqual(texts[0], '后来又一次去爬山，腿疼。')
        finally:
            store.close()

    def test_named_fact_sinks_slower_than_chatter(self):
        store = self.build([
            (days_ago(400), 'user', '我叫小明，你记着。'),
            *[(days_ago(n), 'user', f'今天第{n}次刷手机，眼睛疼。') for n in range(1, 6)],
        ])
        try:
            texts = self.order(store, '我叫什么')
            self.assertIn('我叫小明，你记着。', texts)
        finally:
            store.close()

    def test_recall_does_not_reinforce_the_order(self):
        # 被召回就强化 → 寡头。这里重复查三次，顺序必须一模一样。
        store = self.build([
            (days_ago(300), 'user', '去年去爬山，累得半死。'),
            (days_ago(2), 'user', '昨天去爬山，腿现在还疼。'),
        ])
        try:
            first = self.order(store, '爬山')
            for _ in range(3):
                self.assertEqual(self.order(store, '爬山'), first)
        finally:
            store.close()


class RankPenaltyCurveTests(unittest.TestCase):
    """曲线本身：直接测 rank_penalty 的取值，比只测排序更容易发现参数跑偏。"""

    def piece(self, days, text):
        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        return {'recorded_at': (now - timedelta(days=days)).isoformat(), 'text': text}

    def test_curve_values(self):
        from xuyi.retrieval import rank_penalty
        for text, expected in [
            ('我今天很难过。', [0, 0, 3, 3]),          # 情绪：60 天宽限，之后每 120 天 +1，上限 3
            ('我叫小明。', [0, 0, 1, 2]),              # 长期事实：180 天宽限，之后每 180 天 +1，上限 2
            ('我对芒果过敏。', [0, 0, 1, 2]),
            ('今天下雨了。', [0, 2, 4, 4]),            # 闲聊：14 天宽限，之后每 30 天 +1，上限 4
        ]:
            with self.subTest(text=text):
                got = [rank_penalty(self.piece(days, text)) for days in (10, 60, 300, 2000)]
                self.assertEqual(got, expected)

    def test_broken_timestamp_does_not_penalize(self):
        from xuyi.retrieval import rank_penalty
        self.assertEqual(rank_penalty({'recorded_at': '不是时间', 'text': '今天下雨了。'}), 0)
        self.assertEqual(rank_penalty({'text': '今天下雨了。'}), 0)

    def test_future_timestamp_does_not_penalize(self):
        from xuyi.retrieval import rank_penalty
        from datetime import datetime, timedelta
        future = (datetime.now().astimezone() + timedelta(days=30)).isoformat()
        self.assertEqual(rank_penalty({'recorded_at': future, 'text': '今天下雨了。'}), 0)

    def test_parameters_are_sane(self):
        from xuyi import retrieval
        for grace, step, cap in [
            (retrieval.CHAT_GRACE_DAYS, retrieval.CHAT_PENALTY_DAYS, retrieval.CHAT_MAX_PENALTY),
            (retrieval.FACT_GRACE_DAYS, retrieval.FACT_PENALTY_DAYS, retrieval.FACT_MAX_PENALTY),
            (retrieval.EMOTION_GRACE_DAYS, retrieval.EMOTION_PENALTY_DAYS, retrieval.EMOTION_MAX_PENALTY),
        ]:
            self.assertGreaterEqual(grace, 0)
            self.assertGreater(step, 0)
            self.assertGreaterEqual(cap, 0)
        # 长期事实一定比闲聊沉得慢
        self.assertGreaterEqual(retrieval.FACT_GRACE_DAYS, retrieval.CHAT_GRACE_DAYS)
        self.assertLessEqual(retrieval.FACT_MAX_PENALTY, retrieval.CHAT_MAX_PENALTY)

    def test_penalty_is_cheap(self):
        import time
        from xuyi.retrieval import rank_penalty
        pieces = [self.piece(days, '今天第几次刷手机，眼睛疼。') for days in range(0, 2000, 10)]
        start = time.perf_counter()
        for _ in range(10):
            for piece in pieces:
                rank_penalty(piece)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f'2000 次排序惩罚用了 {elapsed:.2f}s')


if __name__ == '__main__':
    unittest.main()
