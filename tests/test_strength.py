import unittest
from datetime import datetime, timedelta

from xuyi.store import Store
from xuyi.strength import (HALF_LIFE_DAYS, apply_events, family_of, move_for_strength,
                           rebuild, set_override, strengths)

from test_store import event

SCOPE = "qq:c2c:" + "e" * 64
PEER = "e" * 64


def at(days_ago, hour=16):
    moment = datetime.now().astimezone() - timedelta(days=days_ago)
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


class StrengthTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def add(self, seq, days, text, role="user"):
        e = event(f"dsh:s:{seq}", text, scope=SCOPE)
        e["recorded_at"] = at(days)
        e["role"] = role
        e["origin"] = "quoted" if role == "user" else "assistant"
        e["sender_id"] = PEER if role == "user" else "assistant"
        self.store.add(e)
        return e

    def build(self):
        rebuild(self.store, [SCOPE])
        return {row["event_id"]: row for row in strengths(self.store, SCOPE)}

    def test_importance_by_family(self):
        self.add(1, 0, "我家的猫叫年糕。")
        self.add(2, 0, "我对芒果过敏。")
        self.add(3, 0, "我最爱喝乌龙茶。")
        self.add(4, 0, "今天下雨了。")
        rows = self.build()
        self.assertEqual(rows["dsh:s:1"]["importance"], 1.0)
        self.assertEqual(rows["dsh:s:2"]["importance"], 1.0)
        self.assertEqual(rows["dsh:s:3"]["importance"], 0.8)
        self.assertEqual(rows["dsh:s:4"]["importance"], 0.3)

    def test_half_life_grows_with_distinct_days(self):
        self.add(1, 40, "我家的猫叫年糕。")
        self.add(2, 20, "我家的猫叫年糕。")
        self.add(3, 2, "我家的猫叫年糕。")
        rows = self.build()
        self.assertEqual(rows["dsh:s:1"]["half_life"], HALF_LIFE_DAYS[2])   # 提过 3 天 → 180
        self.assertEqual(rows["dsh:s:1"]["days"], 3)
        self.assertGreater(rows["dsh:s:1"]["retention"], 0.9)               # 长期事实有 0.5 底

    def test_same_day_ten_times_counts_once(self):
        for n in range(10):
            self.add(n, 1, "我家的猫叫年糕。")
        rows = self.build()
        self.assertEqual(rows["dsh:s:0"]["days"], 1)
        self.assertEqual(rows["dsh:s:0"]["half_life"], HALF_LIFE_DAYS[0])

    def test_corrected_old_statement_has_zero_strength(self):
        self.add(1, 200, "我最爱喝乌龙茶。")
        self.add(2, 1, "更正一下，我最爱喝美式。")
        self.store.db.execute("CREATE TABLE IF NOT EXISTS supersedes_map("
                              "scope TEXT, supersedes_id TEXT, event_id TEXT, PRIMARY KEY(scope, supersedes_id))")
        self.store.db.execute("INSERT INTO supersedes_map(scope,supersedes_id,event_id) VALUES(?,?,?)",
                              (SCOPE, "dsh:s:1", "dsh:s:2"))
        self.store.db.commit()
        rows = self.build()
        self.assertEqual(rows["dsh:s:1"]["strength"], 0.0)
        self.assertGreater(rows["dsh:s:2"]["strength"], 0.5)

    def test_pinned_never_decays(self):
        self.add(1, 365, "我家的猫叫年糕。")
        set_override(self.store, SCOPE, "dsh:s:1", pinned=True)
        rows = self.build()
        self.assertEqual(rows["dsh:s:1"]["strength"], 1.0)
        self.assertEqual(rows["dsh:s:1"]["retention"], 1.0)

    def test_muted_is_recorded(self):
        self.add(1, 3, "我最近在学吉他。")
        set_override(self.store, SCOPE, "dsh:s:1", muted=True)
        rows = self.build()
        self.assertTrue(rows["dsh:s:1"]["muted"])
        set_override(self.store, SCOPE, "dsh:s:1", muted=False)
        rows = self.build()
        self.assertFalse(rows["dsh:s:1"]["muted"])

    def test_incremental_matches_full_rebuild(self):
        self.add(1, 40, "我家的猫叫年糕。")
        self.add(2, 20, "我家的猫叫年糕。")
        self.add(3, 3, "我最爱喝乌龙茶。")
        rebuild(self.store, [SCOPE])
        full = sorted((row["event_id"], row["strength"]) for row in strengths(self.store, SCOPE))
        self.store.db.execute("DELETE FROM memory_strength")
        self.store.db.execute("DELETE FROM strength_group")
        self.store.db.execute("DELETE FROM strength_days")
        self.store.db.commit()
        apply_events(self.store, SCOPE, self.store.events(SCOPE))
        incremental = sorted((row["event_id"], row["strength"]) for row in strengths(self.store, SCOPE))
        self.assertEqual(full, incremental)

    def test_move_mapping(self):
        self.assertEqual(move_for_strength(0.9), 0)
        self.assertEqual(move_for_strength(0.6), 0)
        self.assertEqual(move_for_strength(0.5), 1)
        self.assertEqual(move_for_strength(0.3), 2)
        self.assertEqual(move_for_strength(0.2), 3)
        self.assertEqual(move_for_strength(0.05), 4)

    def test_family_detection_uses_highlight_families(self):
        self.assertEqual(family_of("我家的猫叫年糕。"), "name")
        self.assertEqual(family_of("我对芒果过敏。"), "diet")
        self.assertEqual(family_of("今天下雨了。"), "other")


if __name__ == "__main__":
    unittest.main()
