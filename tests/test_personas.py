"""预设人设与群聊设置（2026-11-11 重写：结构化人设、群里也有人设、群里怎么回）。"""
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import yaml

from xuyi.personas import (CHAT_STYLE, FACT_RULES, GROUP_STYLE, apply_persona, compose_group_prompt, compose_prompt,
                           list_personas, load_persona, persona_status)

SECTIONS = ("【你是谁】", "【性格和说话】", "【他不同状态时】", "【例子】")


def profile_with(config=None):
    folder = Path(tempfile.mkdtemp())
    (folder / "cordis.patch.yml").write_text(
        yaml.safe_dump([{"id": "im-qqbot", "config": dict(config or {})}], allow_unicode=True), encoding="utf-8")
    return folder


def qqbot(folder):
    entries = yaml.safe_load((folder / "cordis.patch.yml").read_text(encoding="utf-8"))
    return next(e for e in entries if e.get("id") == "im-qqbot")["config"]


class Presets(unittest.TestCase):
    def test_every_preset_is_a_full_card(self):
        names = [item["name"] for item in list_personas()]
        self.assertGreaterEqual(len(names), 11)
        for name in names:
            with self.subTest(name=name):
                text = load_persona(name)["systemPrompt"]
                for section in SECTIONS:
                    self.assertIn(section, text)
                # 够具体，但别写太复杂（用户要求；每轮都发给模型，写太细反而容易照着演）。
                self.assertGreaterEqual(len(text), 300)
                self.assertLessEqual(len(text), 700)
                self.assertNotIn("**", text)

    def test_presets_do_not_carry_real_names(self):
        # 隐私：用户给她起的名字不能出现在任何预设里（真名不写进测试，这里只防常见占位泄漏）。
        for item in list_personas():
            text = load_persona(item["name"])["systemPrompt"]
            self.assertNotIn("X」", text)

    def test_deepseek_preset_is_called_dafeiyu(self):
        # 用户定的：「DeepSeek本尊」这一项就叫「大肥鱼」，描述别玩网络梗。
        item = next(i for i in list_personas() if i["name"] == "deepseek")
        self.assertEqual(item["label"], "大肥鱼")
        self.assertNotIn("疯", item["description"])
        self.assertIn("大肥鱼", load_persona("deepseek")["systemPrompt"])

    def test_fact_rules_apply_to_private_and_group_prompts(self):
        self.assertIn("是不是 AI", CHAT_STYLE)
        for item in list_personas():
            with self.subTest(name=item["name"]):
                text = load_persona(item["name"])["systemPrompt"]
                self.assertIn(FACT_RULES, compose_group_prompt(text))
                self.assertIn(FACT_RULES, compose_prompt(text))
        self.assertIn("不加任何发言人前缀", GROUP_STYLE)


class GroupSettings(unittest.TestCase):
    def test_group_gets_the_same_persona_with_group_manners(self):
        folder = profile_with({"directPrompt": "旧的"})
        apply_persona("wenrou", folder, backup=False)
        config = qqbot(folder)
        persona = load_persona("wenrou")["systemPrompt"]
        self.assertTrue(config["groupPrompt"].startswith(persona))
        self.assertIn(GROUP_STYLE, config["groupPrompt"])
        self.assertIn("私聊里谁跟你说过的事，群里一个字都不提", config["groupPrompt"])
        self.assertNotIn(GROUP_STYLE, config["directPrompt"])
        self.assertEqual(config["groupPrompt"], compose_group_prompt(persona))

    def test_group_reply_sets_require_mention_only_when_asked(self):
        folder = profile_with({"requireMention": False})
        apply_persona("wenrou", folder, backup=False)
        self.assertIs(qqbot(folder)["requireMention"], False)          # 没选就不动
        self.assertEqual(persona_status(folder)["group_reply"], "all")
        apply_persona("wenrou", folder, backup=False, group_reply="mention")
        self.assertIs(qqbot(folder)["requireMention"], True)
        self.assertEqual(persona_status(folder)["group_reply"], "mention")
        apply_persona("wenrou", folder, backup=False, group_reply="all")
        self.assertIs(qqbot(folder)["requireMention"], False)

    def test_own_persona_also_reaches_the_group(self):
        folder = profile_with()
        apply_persona(None, folder, backup=False, text="你叫阿桃，是他的大学室友。")
        self.assertTrue(qqbot(folder)["groupPrompt"].startswith("你叫阿桃"))

    def test_bad_group_reply_is_refused_before_touching_the_file(self):
        from xuyi.store import MemoryError
        folder = profile_with({"directPrompt": "旧的"})
        with self.assertRaises(MemoryError):
            apply_persona("wenrou", folder, backup=False, group_reply="sometimes")
        self.assertEqual(qqbot(folder)["directPrompt"], "旧的")

    def test_status_defaults_to_host_default(self):
        self.assertEqual(persona_status(profile_with())["group_reply"], "mention")

    def test_old_preset_is_recognized_without_mistaking_an_edit_for_a_preset(self):
        body = "你是一个喜欢折纸的朋友。"
        suffix = "\n\n在 QQ 上发消息的样子：\n——旧版共用规则。"
        legacy = body + suffix
        fingerprint = sha256(legacy.encode("utf-8")).hexdigest()
        with patch.dict("xuyi.personas.LEGACY_PROMPT_HASHES", {"wenrou": fingerprint}):
            self.assertEqual(persona_status(profile_with({"directPrompt": legacy}))["name"], "wenrou")
            status = persona_status(profile_with({"directPrompt": body + "说话简短。" + suffix}))
        self.assertIsNone(status["name"])
        self.assertEqual(status["preview"], body + "说话简短。")


if __name__ == "__main__":
    unittest.main()
