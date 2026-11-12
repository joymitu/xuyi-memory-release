"""「TA的记忆」页：记错了 / 恢复、备份、导入（换电脑带走）、自检。全部虚构数据。"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from xuyi.board import board
from xuyi.carry import backup, restore_backup, self_check
from xuyi.consolidate import refresh_corrections
from xuyi.retrieval import Retriever
from xuyi.store import MemoryError, Store
from xuyi.strength import rebuild, set_override

PEER = "c" * 64
SCOPE = "qq:c2c:" + PEER
NOW = datetime.now().astimezone()


def fill(store, rows):
    for n, (days, text) in enumerate(rows):
        stamp = (NOW - timedelta(days=days)).replace(hour=15, minute=0, second=0, microsecond=0)
        store.add({"scope": SCOPE, "event_id": f"dsh:c:{n}", "role": "user", "origin": "quoted",
                   "sender_id": PEER, "recorded_at": stamp.isoformat(), "text": text})
    refresh_corrections(store, SCOPE)
    rebuild(store, [SCOPE])


ROWS = [(30, "我家的猫叫布丁。"), (20, "我最喜欢喝乌龙茶。"), (10, "我想学做蛋糕。"), (5, "今天加班到九点。")]


class MemoryPage(unittest.TestCase):
    def test_groups_carry_clarity_and_switches(self):
        with Store(":memory:") as store:
            fill(store, ROWS)
            page = board(store, SCOPE)
            labels = {item["label"]: item for group in page["memory"] for item in group["items"]}
            self.assertIn("猫的名字", labels)
            self.assertIn(labels["猫的名字"]["clarity"], ("清楚", "有点模糊", "快忘了", "一直记着"))
            self.assertFalse(labels["猫的名字"]["pinned"])

    def test_allergies_are_sensitive_and_plans_read_as_appointments(self):
        with Store(":memory:") as store:
            fill(store, ROWS + [(4, "我对芒果过敏。"), (3, "周六和小林去看电影。")])
            page = board(store, SCOPE)
            groups = {g["name"]: g["items"] for g in page["memory"]}
            allergy = next(i for items in groups.values() for i in items if "芒果" in i["value"])
            self.assertTrue(allergy["sensitive"])                 # 和心情一样：只在你提到时才说
            self.assertIn("约定", groups)
            self.assertNotIn("朋友", groups)
            self.assertEqual(groups["约定"][0]["label"], "和小林")

    def test_marking_wrong_hides_it_everywhere_and_restore_brings_it_back(self):
        with Store(":memory:") as store:
            fill(store, ROWS)
            cat = next(i for g in board(store, SCOPE)["memory"] for i in g["items"] if i["label"] == "猫的名字")
            set_override(store, SCOPE, cat["event_id"], wrong=True)
            page = board(store, SCOPE)
            self.assertFalse(any(i["label"] == "猫的名字" for g in page["memory"] for i in g["items"]))
            self.assertTrue(any(i["label"] == "猫的名字" for i in page["forgotten"]))
            self.assertNotIn("布丁", Retriever(store).recall(SCOPE, "我家猫叫啥？", 1600, focus=True)["context"])
            set_override(store, SCOPE, cat["event_id"], wrong=False)
            self.assertIn("布丁", Retriever(store).recall(SCOPE, "我家猫叫啥？", 1600, focus=True)["context"])

    def test_things_he_asked_to_forget_never_show_up(self):
        with Store(":memory:") as store:
            fill(store, ROWS + [(2, "我家的狗叫旺财，算了这个别记了。")])
            page = board(store, SCOPE)
            text = json.dumps(page["memory"], ensure_ascii=False) + json.dumps(page["forgotten"], ensure_ascii=False)
            self.assertNotIn("旺财", text)


class BackupAndRestore(unittest.TestCase):
    def test_backup_carries_every_contact_and_the_switches_to_a_new_computer(self):
        with Store(":memory:") as old:
            fill(old, ROWS)
            cat = next(i for g in board(old, SCOPE)["memory"] for i in g["items"] if i["label"] == "猫的名字")
            set_override(old, SCOPE, cat["event_id"], pinned=True)
            other = "qq:group:" + "d" * 64
            old.add({"scope": other, "event_id": "dsh:g:1", "role": "user", "origin": "quoted",
                     "sender_id": "e" * 64, "recorded_at": NOW.isoformat(), "text": "群里说的一句"})
            packed = backup(old)
        self.assertEqual(packed["format"], "xuyi-backup/1")
        with Store(":memory:") as new:
            result = restore_backup(new, json.loads(json.dumps(packed, ensure_ascii=False)))
            self.assertEqual(result["contacts"], 2)
            self.assertIn("布丁", Retriever(new).recall(SCOPE, "我家猫叫啥？", 1600, focus=True)["context"])
            cat = next(i for g in board(new, SCOPE)["memory"] for i in g["items"] if i["label"] == "猫的名字")
            self.assertTrue(cat["pinned"])
            again = restore_backup(new, packed)              # 再导一次：都是重复，不出错
            self.assertEqual(again["inserted"], 0)

    def test_tampered_backup_is_refused(self):
        with Store(":memory:") as old:
            fill(old, ROWS)
            packed = backup(old)
        packed["contacts"][0]["payload"]["events"][0]["text"] = "被改过的话"
        with Store(":memory:") as new, self.assertRaises(MemoryError):
            restore_backup(new, packed)

    def test_wrong_format_is_refused(self):
        with Store(":memory:") as new, self.assertRaises(MemoryError):
            restore_backup(new, {"format": "something-else"})


class SelfCheck(unittest.TestCase):
    def test_reports_each_memory_as_carried_over_or_not(self):
        with Store(":memory:") as store:
            fill(store, ROWS)
            report = self_check(store, SCOPE)
            self.assertGreaterEqual(report["checked"], 2)
            self.assertEqual(report["ok"], report["checked"])
            self.assertEqual(report["missed"], [])

    def test_a_memory_that_cannot_be_found_is_reported(self):
        with Store(":memory:") as store:
            fill(store, ROWS)
            from xuyi import carry
            original = carry._probe
            carry._probe = lambda retriever, scope, item: False      # 模拟换了之后找不回来
            try:
                report = self_check(store, SCOPE)
            finally:
                carry._probe = original
            self.assertEqual(report["ok"], 0)
            self.assertEqual(len(report["missed"]), report["checked"])


class Health(unittest.TestCase):
    """装好以后续忆到底有没有真用上：收到了 QQ 私聊、存进了档案、拿这句话能想起来。"""

    def test_nothing_received_yet(self):
        from xuyi.carry import health
        with Store(":memory:") as store:
            report = health(store)
        self.assertFalse(report["recording"])
        self.assertFalse(report["recall"])
        self.assertFalse(report["ok"])

    def test_received_and_recallable(self):
        from xuyi.carry import health
        with Store(":memory:") as store:
            fill(store, ROWS)
            report = health(store)
        self.assertTrue(report["recording"])
        self.assertTrue(report["recall"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["messages"], len(ROWS))

    def test_group_messages_alone_do_not_count(self):
        # 群里收到的不算「私聊用上了」：引导问的是你自己和TA聊没聊通。
        from xuyi.carry import health
        with Store(":memory:") as store:
            store.add({"scope": "qq:group:" + "d" * 64, "event_id": "dsh:g:1", "role": "user", "origin": "quoted",
                       "sender_id": "e" * 64, "recorded_at": NOW.isoformat(), "text": "群里说的一句话而已"})
            self.assertFalse(health(store)["recording"])

    def test_the_message_being_answered_right_now_still_counts(self):
        # 刚收到的那句（半小时内）在模型查询里会被当成「正在回答的问题」排除；体检不能因此判失败。
        from xuyi.carry import health
        with Store(":memory:") as store:
            store.add({"scope": SCOPE, "event_id": "dsh:n:1", "role": "user", "origin": "quoted",
                       "sender_id": PEER, "recorded_at": NOW.isoformat(), "text": "在吗，我第一次找你聊天"})
            self.assertTrue(health(store)["recall"])


if __name__ == "__main__":
    unittest.main()
