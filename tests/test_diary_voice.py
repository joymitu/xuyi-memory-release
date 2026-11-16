"""日记用人设的视角写：交给模型的是人设本身（不带 QQ 发消息的规矩），不把对方写死成「他」。"""
import tempfile
import unittest
from pathlib import Path

from xuyi.board_bridge import diary_prompt, paths, read, request_diary, writing_voice
from xuyi.personas import CHAT_STYLE_HEADING, compose_prompt, load_persona

MESSAGES = [{"time": "20:01", "role": "user", "text": "今天面试过了！"},
            {"time": "20:02", "role": "assistant", "text": "哇恭喜！"}]


class DiaryVoiceTests(unittest.TestCase):
    def test_diary_gets_the_persona_without_the_chat_rules(self):
        persona = load_persona("aojiao")["systemPrompt"]
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "m.sqlite")
            _, pending = request_diary(db, "qq:c2c:" + "a" * 64, "2026-11-15", MESSAGES,
                                       compose_prompt(persona), "2026-11-16T00:00:00+00:00")
            self.assertTrue(pending)
            asked = read(paths(db, "qq:c2c:" + "a" * 64, "diary-2026-11-15")[0])
        self.assertTrue(asked["persona"].startswith(persona.strip()))
        self.assertNotIn(CHAT_STYLE_HEADING, asked["persona"])   # 「每条要短、最多分三条」不进日记
        self.assertIn("你自己的视角", asked["persona"])

    def test_the_prompt_does_not_decide_the_users_gender(self):
        prompt = diary_prompt("2026-11-15", "周日", MESSAGES)
        self.assertIn("对方：今天面试过了", prompt)
        self.assertNotIn("他：", prompt)
        self.assertIn("第一人称", prompt)

    def test_no_persona_means_no_diary_request(self):
        self.assertEqual(writing_voice(""), "")
        self.assertEqual(writing_voice(None), "")


if __name__ == "__main__":
    unittest.main()
