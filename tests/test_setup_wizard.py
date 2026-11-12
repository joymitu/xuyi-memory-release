"""首次引导（日记本里的「设置」）：选人设、看 QQ 插件、开聊。

QQ 插件刚装好、还没扫码时，profile 补丁里还没有 im-qqbot 条目；引导要能先选人设。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from xuyi.personas import apply_persona, load_persona, persona_status

ROOT = Path(__file__).resolve().parents[1]
OTHER = "- id: ui-chat\n  name: '@deepseek-ai/dsh-client-ui-chat'\n"


class PersonaSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name)
        (self.profile / "cordis.patch.yml").write_text(OTHER, encoding="utf-8")

    def entries(self):
        return yaml.safe_load((self.profile / "cordis.patch.yml").read_text(encoding="utf-8"))

    def test_without_a_qq_entry_it_refuses_unless_asked_to_create_one(self):
        with self.assertRaises(Exception):
            apply_persona("wenrou", self.profile)
        out = apply_persona("wenrou", self.profile, create=True)
        self.assertTrue(out["backup"])
        qq = [e for e in self.entries() if e.get("id") == "im-qqbot"]
        self.assertEqual(len(qq), 1)
        self.assertNotIn("name", qq[0])   # 只是覆盖腾讯插件那一行的配置，不新增插件行
        self.assertIn(load_persona("wenrou")["systemPrompt"].strip()[:20], qq[0]["config"]["directPrompt"])
        self.assertEqual(self.entries()[0]["id"], "ui-chat")   # 其他行原样
        # 已有条目时更新它，不再追加第二条。
        apply_persona("wenrou", self.profile, create=True)
        self.assertEqual(len([e for e in self.entries() if e.get("id") == "im-qqbot"]), 1)

    def test_status_names_the_preset_or_says_custom(self):
        self.assertFalse(persona_status(self.profile)["configured"])
        apply_persona("wenrou", self.profile, create=True)
        status = persona_status(self.profile)
        self.assertTrue(status["configured"])
        self.assertEqual(status["name"], "wenrou")
        self.assertEqual(status["label"], load_persona("wenrou")["label"])
        apply_persona(None, self.profile, text="你叫阿桃，是他的大学室友。", create=True)
        status = persona_status(self.profile)
        self.assertEqual(status["name"], None)
        self.assertEqual(status["label"], "自定义人设")
        self.assertIn("阿桃", status["preview"])

    def test_cli_setup_state_lists_presets_and_current_persona(self):
        env = {**os.environ, "PYTHONUTF8": "1"}
        db = str(self.profile / "m.sqlite")
        out = subprocess.run([sys.executable, "-m", "xuyi", "--db", db, "setup-state",
                              "--profile", str(self.profile)], capture_output=True, cwd=ROOT, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        state = json.loads(out.stdout.decode("utf-8"))
        self.assertFalse(state["persona"]["configured"])
        self.assertGreaterEqual(len(state["presets"]), 5)
        self.assertTrue(all({"name", "label", "description"} <= set(p) for p in state["presets"]))
        applied = subprocess.run([sys.executable, "-m", "xuyi", "--db", db, "persona", "wenrou", "--apply",
                                  "--create", "--profile", str(self.profile)],
                                 capture_output=True, cwd=ROOT, env=env, timeout=60)
        self.assertEqual(applied.returncode, 0, applied.stderr.decode("utf-8", "replace"))
        self.assertEqual(persona_status(self.profile)["name"], "wenrou")


if __name__ == "__main__":
    unittest.main()
