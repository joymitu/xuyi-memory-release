"""预设不分男女：不把对方写成「他」、不叫「兄弟」；旧版预设照样认得出，并提示可以更新。"""
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from xuyi.personas import CHAT_STYLE, GROUP_STYLE, LEGACY_PROMPT_HASHES, apply_persona, list_personas, \
    load_persona, persona_status

GENDERED = ("他", "兄弟", "老哥", "哥们")


class NeutralPresetTests(unittest.TestCase):
    def test_presets_do_not_assume_the_user_is_a_man(self):
        for item in list_personas():
            data = load_persona(item["name"])
            with self.subTest(name=item["name"]):
                for word in GENDERED:
                    self.assertNotIn(word, data["systemPrompt"])
                    self.assertNotIn(word, data["description"])

    def test_chat_rules_do_not_either(self):
        for word in GENDERED:   # 「其他人设」里的「他」不是指人
            self.assertNotIn(word, CHAT_STYLE.replace("其他", ""))
            self.assertNotIn(word, GROUP_STYLE.replace("其他", "").replace("「他」", ""))

    def test_replacing_he_did_not_break_other_words(self):
        # 2026-11-16：「其他人设」曾被误换成「其TA人设」。
        texts = [CHAT_STYLE, GROUP_STYLE] + [load_persona(item["name"])["systemPrompt"] for item in list_personas()]
        for text in texts:
            for broken in ("其TA", "TA们", "TA人"):
                self.assertNotIn(broken, text)

    def test_chat_rules_answer_memory_and_recommend_directly(self):
        self.assertIn("别说「这都能忘」", CHAT_STYLE)
        self.assertIn("直接说出一个具体的", CHAT_STYLE)
        self.assertIn("别说「这都能忘」", GROUP_STYLE)   # 群里也一样不怪人

    def test_group_rules_do_not_guess_members_gender(self):
        self.assertIn("看不出是男是女", GROUP_STYLE)
        self.assertIn("只轻轻带过", GROUP_STYLE)

    def test_every_preset_keeps_its_older_versions_recognisable(self):
        for item in list_personas():
            self.assertGreaterEqual(len(LEGACY_PROMPT_HASHES[item["name"]]), 3, item["name"])


class OutdatedPresetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name)
        (self.profile / "cordis.patch.yml").write_text("- id: im-qqbot\n  config: {}\n", encoding="utf-8")

    def test_current_preset_is_not_outdated(self):
        apply_persona("aojiao", self.profile, backup=False)
        status = persona_status(self.profile)
        self.assertEqual(status["name"], "aojiao")
        self.assertFalse(status["outdated"])

    def test_an_old_version_is_still_that_preset_but_outdated(self):
        old = "【你是谁】\n你是嘴硬心软的青梅。跟他说话熟络。"
        apply_persona(None, self.profile, text=old, backup=False)
        from xuyi.board_bridge import find_persona
        digest = sha256(find_persona(self.profile)["text"].encode("utf-8")).hexdigest()
        with patch.dict("xuyi.personas.LEGACY_PROMPT_HASHES", {"aojiao": (digest,)}):
            status = persona_status(self.profile)
        self.assertEqual(status["name"], "aojiao")
        self.assertTrue(status["outdated"])


if __name__ == "__main__":
    unittest.main()
