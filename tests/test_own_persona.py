"""自己写的人设要保存好：换成预设不丢、能换回来、删之前界面先确认；正在用的那份标出来。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from xuyi.personas import list_own, persona_body, persona_status, save_own
from xuyi.store import Store

ROOT = Path(__file__).resolve().parents[1]
MINE = "你叫阿桃，是我的大学室友，说话直来直去，爱吐槽但很护着我。"
OTHER = "- id: ui-chat\n  name: '@deepseek-ai/dsh-client-ui-chat'\n"


class OwnPersonaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name)
        (self.profile / "cordis.patch.yml").write_text(OTHER, encoding="utf-8")
        self.db = str(self.profile / "m.sqlite")

    def cli(self, *args):
        env = {**os.environ, "PYTHONUTF8": "1"}
        out = subprocess.run([sys.executable, "-m", "xuyi", "--db", self.db, *args],
                             capture_output=True, cwd=ROOT, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return json.loads(out.stdout.decode("utf-8"))

    def write_mine(self, text=MINE):
        file = self.profile / "mine.txt"
        file.write_text(text, encoding="utf-8")
        return self.cli("persona", "--file", str(file), "--apply", "--create", "--profile", str(self.profile))

    def state(self):
        return self.cli("setup-state", "--profile", str(self.profile))

    def test_my_persona_survives_switching_to_a_preset_and_comes_back(self):
        applied = self.write_mine()
        self.assertTrue(applied["own_id"])
        self.cli("persona", "wenrou", "--apply", "--create", "--profile", str(self.profile))
        self.assertEqual(persona_status(self.profile)["name"], "wenrou")
        own = self.state()["own"]
        self.assertEqual([o["text"] for o in own], [MINE])
        self.cli("persona", "--own", own[0]["id"], "--apply", "--profile", str(self.profile))
        state = self.state()
        self.assertIsNone(state["persona"]["name"])
        self.assertEqual(state["persona"]["own_id"], own[0]["id"])
        self.assertIn("阿桃", state["persona"]["preview"])

    def test_a_persona_written_by_hand_in_the_host_is_kept_before_it_is_replaced(self):
        # 用户不是在续忆里写的，而是直接在 DSH 里改了人设：换成预设之前也要先存下来。
        from xuyi.personas import apply_persona
        apply_persona(None, self.profile, text=MINE, create=True, backup=False)
        self.cli("persona", "wenrou", "--apply", "--create", "--profile", str(self.profile))
        self.assertEqual([o["text"] for o in self.state()["own"]], [MINE])

    def test_opening_settings_already_saves_the_persona_in_use(self):
        from xuyi.personas import apply_persona
        apply_persona(None, self.profile, text=MINE, create=True, backup=False)
        state = self.state()
        self.assertEqual(state["persona"]["own_id"], state["own"][0]["id"])

    def test_the_same_text_is_kept_once_and_forget_removes_only_that_one(self):
        self.write_mine()
        self.write_mine()
        self.write_mine("你叫小满，是我的同桌。")
        own = self.state()["own"]
        self.assertEqual(len(own), 2)
        self.assertEqual(own[0]["text"], "你叫小满，是我的同桌。")   # 最近用的排前面
        self.assertEqual(self.cli("persona", "--forget-own", own[1]["id"])["forgotten"], 1)
        self.assertEqual([o["text"] for o in self.state()["own"]], ["你叫小满，是我的同桌。"])

    def test_body_drops_the_chat_rules(self):
        from xuyi.personas import compose_prompt
        self.assertEqual(persona_body(compose_prompt(MINE)), MINE)

    def test_store_keeps_at_most_the_limit(self):
        from xuyi.personas import OWN_LIMIT
        with Store(":memory:") as store:
            for i in range(OWN_LIMIT + 5):
                save_own(store, f"人设{i}", used=True)
            self.assertEqual(len(list_own(store)), OWN_LIMIT)


if __name__ == "__main__":
    unittest.main()
