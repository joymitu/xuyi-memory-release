"""群聊记忆：只查本群、「我」只查说话的人、写明是谁说的、别人的私事不当众说、
主人私聊的事绝不进群；她自己的喜好群聊私聊共用。"""
import unittest
from datetime import datetime, timedelta, timezone

from xuyi import index
from xuyi.retrieval import GROUP_LINES, Retriever
from xuyi.store import Store

GROUP = "qq:group:" + "g" * 32
OWNER = "qq:c2c:" + "o" * 64
WANG, LI, HUA = "AAAA0000AAAA0000AAAA", "BBBB1111BBBB1111BBBB", "CCCC2222CCCC2222CCCC"
NAMES = {WANG: "小王", LI: "小李", HUA: "阿花"}
START = datetime(2026, 11, 1, 20, 0, tzinfo=timezone(timedelta(hours=8)))


class GroupBase(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(":memory:"))
        self.n = 0

    def say(self, scope, who, text, role="user"):
        self.n += 1
        event = {"scope": scope, "event_id": f"dsh:g:{self.n}", "sender_id": who if role == "user" else "assistant",
                 "role": role, "origin": "quoted" if role == "user" else "assistant",
                 "recorded_at": (START + timedelta(hours=self.n)).isoformat(), "text": text}
        if scope == GROUP and role == "user" and who in NAMES:
            event["sender_name"] = NAMES[who]
        self.store.add(event)
        index.update(self.store, scope, [{k: v for k, v in event.items() if k != "sender_name"}])
        return self

    def chat(self):
        for i in range(25):   # 真实群里大多数话和问题无关
            self.say(GROUP, HUA, f"今天第{i}次刷到猫猫视频哈哈哈")
        return self

    def ask(self, speaker, query, her=False):
        return Retriever(self.store).recall(GROUP, query, 700, group=True, speaker=speaker, her=her)["context"]


class WhoSaidItTests(GroupBase):
    def test_i_means_the_person_asking(self):
        self.say(GROUP, WANG, "我养了只柯基叫豆豆").say(GROUP, LI, "我养了只布偶叫雪球").chat()
        context = self.ask(LI, "我养的猫叫啥来着？")
        self.assertIn("雪球", context)
        self.assertNotIn("豆豆", context)

    def test_naming_a_member_looks_at_that_member(self):
        self.say(GROUP, WANG, "我养了只柯基叫豆豆").say(GROUP, LI, "我养了只布偶叫雪球").chat()
        context = self.ask(HUA, "小王养的狗叫啥？")
        self.assertIn("豆豆", context)
        self.assertNotIn("雪球", context)

    def test_lines_say_who_said_them(self):
        self.say(GROUP, WANG, "我养了只柯基叫豆豆").chat()
        context = self.ask(WANG, "我养的狗叫啥？")
        self.assertIn("| 小王] 我养了只柯基叫豆豆", context)
        self.assertNotIn(WANG[:16], context)

    def test_at_most_two_lines(self):
        for i in range(6):
            self.say(GROUP, WANG, f"我的第{i}只柯基叫豆豆{i}号")
        self.chat()
        lines = [line for line in self.ask(WANG, "我的柯基都叫啥？").split("\n") if line.startswith("[dsh:")]
        self.assertEqual(len(lines), GROUP_LINES)

    def test_repeated_chit_chat_with_numbers_does_not_chain_in(self):
        # 真 DeepSeek 群聊模拟发现：「今天第13次刷到猫猫视频」只是数字不同，被「数字变了」串了进来。
        self.say(GROUP, LI, "我养了只布偶叫雪球")
        for i in range(10):
            self.say(GROUP, LI, f"今天第{i}次刷到猫猫视频哈哈哈")
        self.chat()
        context = self.ask(LI, "我养的猫叫啥来着？")
        self.assertIn("雪球", context)
        self.assertNotIn("猫猫视频", context)

    def test_own_sleep_question_finds_own_line(self):
        self.say(GROUP, WANG, "我最近焦虑得睡不着，在吃药").chat()
        self.assertIn("睡不着", self.ask(WANG, "我最近睡眠咋样来着"))

    def test_plain_chat_brings_nothing(self):
        self.say(GROUP, WANG, "我养了只柯基叫豆豆").chat()
        self.assertEqual(self.ask(LI, "我血型是什么？"), "")


class PrivacyTests(GroupBase):
    def test_owners_private_chat_never_reaches_the_group(self):
        self.say(OWNER, "o" * 64, "我养了只柯基叫团子，别告诉别人")
        self.say(GROUP, WANG, "今天天气不错").chat()
        for speaker in (WANG, LI):
            context = self.ask(speaker, "我养的狗叫啥？")
            self.assertNotIn("团子", context)

    def test_someone_elses_private_matters_stay_out(self):
        self.say(GROUP, WANG, "我最近焦虑得睡不着，在吃药").chat()
        self.assertNotIn("焦虑", self.ask(LI, "小王最近怎么样了"))

    def test_asking_about_yourself_is_fine_but_kept_light(self):
        self.say(GROUP, WANG, "我对芒果过敏，一吃嘴就肿").chat()
        context = self.ask(WANG, "我对啥过敏来着？")
        self.assertIn("芒果", context)
        self.assertIn("只轻轻带过", context)   # 用户定：本人问自己的私事，群里说得含糊点
        self.assertNotIn("芒果", self.ask(LI, "小王对啥过敏？"))

    def test_a_private_chat_lookup_never_sees_the_group(self):
        self.say(GROUP, WANG, "我养了只柯基叫豆豆").chat()
        self.say(OWNER, "o" * 64, "今天好累")
        context = Retriever(self.store).recall(OWNER, "我养的狗叫啥？", 900, highlight_only=True, about_me=True)["context"]
        self.assertNotIn("豆豆", context)


class HerOwnLookupInGroupTests(GroupBase):
    """她在群里主动翻记忆（工具）：同样写昵称、同样不翻别人的私事。"""

    def look(self, speaker, query):
        return Retriever(self.store).recall(GROUP, query, 1600, focus=True, speaker=speaker)["context"]

    def test_names_instead_of_numbers(self):
        self.say(GROUP, WANG, "我养了只柯基叫豆豆").chat()
        context = self.look(LI, "小王的狗叫啥")
        self.assertIn("小王] 我养了只柯基叫豆豆", context)

    def test_someone_elses_private_matters_stay_out(self):
        self.say(GROUP, WANG, "我最近焦虑得睡不着，在吃药").chat()
        self.assertNotIn("焦虑", self.look(LI, "小王最近睡得怎么样"))
        own = self.look(WANG, "我最近睡得怎么样")
        self.assertIn("焦虑", own)
        self.assertIn("只轻轻带过", own)

    def test_ordinary_lines_carry_no_privacy_note(self):
        self.say(GROUP, WANG, "我养了只柯基叫豆豆").chat()
        self.assertNotIn("只轻轻带过", self.ask(WANG, "我养的狗叫啥？"))


class HerTasteEverywhereTests(GroupBase):
    def test_her_private_answer_keeps_her_consistent_in_the_group(self):
        self.say(OWNER, "o" * 64, "你最喜欢吃什么零食呀").say(OWNER, None, "海苔吧，咔嚓咔嚓的", role="assistant")
        self.say(GROUP, WANG, "今天天气不错").chat()
        context = self.ask(LI, "你最喜欢什么零食？", her=True)
        self.assertIn("海苔", context)
        self.assertIn("别处说过的自己的喜好", context)
        self.assertNotIn("你最喜欢吃什么零食呀", context)   # 私聊那句问话不带进群

    def test_her_group_answer_keeps_her_consistent_in_private(self):
        self.say(GROUP, WANG, "你最喜欢吃什么零食呀").say(GROUP, None, "海苔吧", role="assistant").chat()
        self.say(OWNER, "o" * 64, "今天好累")
        context = Retriever(self.store).recall(OWNER, "你最喜欢什么零食？", 900, highlight_only=True, her=True)["context"]
        self.assertIn("海苔", context)

    def test_members_tastes_are_not_hers(self):
        self.say(GROUP, WANG, "我最喜欢的零食是辣条").chat()
        self.assertNotIn("辣条", self.ask(LI, "你最喜欢什么零食？", her=True))


class MemberNameTests(GroupBase):
    def test_newest_nickname_wins(self):
        self.say(GROUP, WANG, "大家好")
        NAMES[WANG] = "王大锤"
        try:
            self.say(GROUP, WANG, "我改名了")
        finally:
            NAMES[WANG] = "小王"
        self.assertEqual(self.store.member_names(GROUP)[WANG], "王大锤")

    def test_private_chat_events_keep_no_names(self):
        event = {"scope": OWNER, "event_id": "x1", "sender_id": "o" * 64, "role": "user", "origin": "quoted",
                 "recorded_at": START.isoformat(), "text": "hi", "sender_name": "主人"}
        self.store.add(event)
        self.assertEqual(self.store.member_names(OWNER), {})
        self.assertNotIn("sender_name", self.store.get(OWNER, "x1"))


if __name__ == "__main__":
    unittest.main()
