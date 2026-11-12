import unittest
from datetime import date

from xuyi.board import _note_seeds
from xuyi.diary import diary
from xuyi.store import Store

from test_store import event


class NoteTopicTests(unittest.TestCase):
    """当天没有"重点事实"时，便利贴也要贴上今天真正聊过的事；且只贴今天。

    2026-11-11：用户反馈「跟她说了事情但没记到便利贴上」——问句不该当成一件事贴上去，
    过了零点那一天的话题也不该消失（见 PastDayTopicTests）。
    """

    TODAY = date(2026, 11, 10)

    def setUp(self):
        self.store = Store(':memory:')
        self.scope = "qq:c2c:" + "f" * 64
        self.peer = "f" * 64
        rows = [
            ("2026-11-09T16:00:00+08:00", "我家的猫叫年糕。"),
            ("2026-11-10T16:20:00+08:00", "下周要去杭州出差三天，好烦"),
            ("2026-11-10T16:24:00+08:00", "昨天你推荐我喝的那家奶茶到底叫什么名字来着"),
            ("2026-11-10T16:25:00+08:00", "周末想去看个展，还在纠结看哪个"),
            ("2026-11-10T16:30:00+08:00", "嗯"),
        ]
        for n, (stamp, text) in enumerate(rows):
            e = event(f"dsh:s:{n}", text, scope=self.scope)
            e["recorded_at"] = stamp
            e["sender_id"] = self.peer
            self.store.add(e)

    def tearDown(self):
        self.store.close()

    def seeds(self):
        return _note_seeds(self.store, self.scope, self.peer,
                           self.store.events(self.scope), set(), self.TODAY, 6, [])

    def test_topic_notes_cover_today_and_only_today(self):
        topics = [seed for seed in self.seeds() if seed["kind"] == "topic"]
        self.assertEqual(len(topics), 2)
        texts = " ".join(seed["text"] for seed in topics)
        self.assertIn("出差", texts)
        self.assertIn("看个展", texts)
        self.assertNotIn("年糕", texts)          # 昨天的事不进今天的便利贴
        ids = {seed["event_id"] for seed in topics}
        self.assertNotIn("dsh:s:0", ids)
        self.assertNotIn("dsh:s:4", ids)          # 「嗯」这种不算

    def test_typoed_questions_to_her_are_not_topics(self):
        # 真实档案里「……来着」打成了「……来自」，句尾规则就漏了；问她的话（有「你」又有疑问词）一律不贴。
        from xuyi.board import topic_text
        self.assertIsNone(topic_text("上次你说的那部电影叫什么名字来自"))
        self.assertIsNone(topic_text("你怎么不按常理出牌啊"))
        self.assertEqual(topic_text("周末想去看个展，还在纠结看哪个"), "周末想去看个展，还在纠结看哪个")

    def test_questions_are_not_topics(self):
        ids = {seed["event_id"] for seed in self.seeds() if seed["kind"] == "topic"}
        self.assertNotIn("dsh:s:2", ids)          # 「……来着」是他问她的，不是他告诉她的事


class PastDayTopicTests(unittest.TestCase):
    """过了零点，前一天聊过的事还要留在那一页的便利贴上（以前第二天就没了）。"""

    def test_each_day_page_keeps_its_own_topics(self):
        scope, peer = "qq:c2c:" + "e" * 64, "e" * 64
        with Store(':memory:') as store:
            rows = [
                ("2026-11-08T20:00:00+08:00", "user", "今天终于把房子退了，押金也拿回来了"),
                ("2026-11-08T20:01:00+08:00", "assistant", "恭喜呀"),
                ("2026-11-08T20:02:00+08:00", "user", "你还记得我上次说的那家店叫啥不"),
                ("2026-11-09T21:00:00+08:00", "user", "明天要早起去体检，不能吃早饭"),
            ]
            for n, (stamp, role, text) in enumerate(rows):
                e = event(f"dsh:p:{n}", text, scope=scope)
                e["recorded_at"], e["role"] = stamp, role
                e["sender_id"] = peer if role == "user" else "assistant"
                e["origin"] = "quoted" if role == "user" else "assistant"
                store.add(e)
            book = diary(store, scope, notes=[])
            by_day = {d["date"]: [n for n in d["notes"] if n.get("kind") == "topic"] for d in book["days"]}
            self.assertTrue(any("押金" in n["text"] for n in by_day["2026-11-08"]), by_day)
            self.assertFalse(any("那家店" in n["text"] for n in by_day["2026-11-08"]), by_day)   # 问句不贴
            self.assertTrue(any("体检" in n["text"] for n in by_day["2026-11-09"]), by_day)

    def test_model_written_topics_are_not_duplicated(self):
        scope = "qq:c2c:" + "e" * 64
        with Store(':memory:') as store:
            e = event("dsh:q:0", "明天要早起去体检，不能吃早饭", scope=scope)
            e["recorded_at"] = "2026-11-09T21:00:00+08:00"
            store.add(e)
            written = [{"kind": "topic", "text": "体检别空太久肚子哦", "event_id": "dsh:q:0"}]
            day = diary(store, scope, notes=written)["days"][0]
            self.assertEqual([n["text"] for n in day["notes"] if n.get("kind") == "topic"], ["体检别空太久肚子哦"])


if __name__ == "__main__":
    unittest.main()
