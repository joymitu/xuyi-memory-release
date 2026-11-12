"""记忆检索回归测试。"""
import unittest
from datetime import datetime, timedelta

from xuyi.consolidate import refresh_corrections
from xuyi.retrieval import Retriever
from xuyi.store import Store
from xuyi.strength import rebuild

NOW = datetime.now().astimezone()
SCOPE = "qq:c2c:" + "f" * 64


class Archive:
    def __init__(self):
        self.store = Store(":memory:")
        self.seq = 0

    def say(self, days, text, role="user", hour=16):
        self.seq += 1
        stamp = (NOW - timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if days == 0 and stamp > NOW:
            stamp = NOW - timedelta(minutes=30 - self.seq % 30)
        self.store.add(dict(scope=SCOPE, event_id=f"dsh:t:{self.seq}", role=role,
                            origin="quoted" if role == "user" else "assistant",
                            recorded_at=stamp.isoformat(), text=text,
                            sender_id="f" * 64 if role == "user" else "assistant"))
        return self

    def recall(self, query, budget=1600):
        refresh_corrections(self.store, SCOPE)
        rebuild(self.store, [SCOPE])
        return Retriever(self.store).recall(SCOPE, query, budget, focus=True)["context"]


def evidence(context):
    return [line for line in context.splitlines() if line.startswith("[")]


class AskedQuestionsAreNotEvidence(unittest.TestCase):
    """问题 2：用户今天问过的问句（回答只有「嗯嗯」）排在最前，把真正的证据挤掉。"""

    def test_earlier_questions_do_not_crowd_out_the_fact(self):
        a = Archive()
        a.say(3, "今天去看了莫奈的展，人超多。").say(3, "莫奈的睡莲好美", "assistant")
        a.say(3, "周五要去看牙，智齿疼。").say(3, "早点处理好", "assistant")
        a.say(0, "我什么时候去看牙？").say(0, "嗯嗯", "assistant")
        a.say(0, "我喜欢的那个英国乐队叫啥？").say(0, "嗯嗯", "assistant")
        a.say(0, "我前几天去看的什么展？")
        lines = evidence(a.recall("我前几天去看的什么展？"))
        self.assertTrue(lines and "莫奈" in lines[0], lines)
        self.assertFalse(any("我什么时候去看牙？" in line for line in lines), lines)

    def test_question_with_a_real_answer_still_comes_back(self):
        # 0.9.2 的真机例子：答案在她的回答里，问句和回答要一起回来。
        a = Archive()
        a.say(2, "什么品牌的奶茶好喝？").say(2, "云朵茶铺的桂花乌龙，清爽不腻。", "assistant")
        a.say(0, "你上次说哪个牌子的奶茶好喝？")
        self.assertIn("桂花乌龙", a.recall("你上次说哪个牌子的奶茶好喝？"))


class ForgetRequests(unittest.TestCase):
    """问题 5：用户说「别记了，忘掉吧」，照样查得到，还在无关查询里冒出来。"""

    def test_same_message_forget_hides_the_line(self):
        a = Archive()
        a.say(5, "我前任叫阿杰。算了，这个别记了，忘掉吧。").say(5, "好，不提了", "assistant")
        a.say(3, "今天去看了莫奈的展，人超多。")
        self.assertNotIn("阿杰", a.recall("我前任叫什么？"))
        self.assertNotIn("阿杰", a.recall("我前几天去看的什么展？"))

    def test_separate_forget_message_hides_the_previous_line(self):
        a = Archive()
        a.say(5, "我前任叫阿杰。", hour=15).say(5, "嗯嗯", "assistant", hour=15)
        a.say(5, "刚才那句别记了。", hour=16)
        self.assertNotIn("阿杰", a.recall("我前任叫什么？"))

    def test_snapshot_and_proactive_skip_forgotten(self):
        from xuyi.consolidate import snapshot
        from xuyi.strength import proactive_allowed
        a = Archive()
        a.say(1, "我前任叫阿杰。算了，这个别记了。")
        a.recall("随便")
        self.assertFalse(proactive_allowed(a.store, SCOPE, "dsh:t:1"))
        self.assertNotIn("阿杰", snapshot(a.store, SCOPE, 800, current_session="probe")["context"])

    def test_accusing_her_of_forgetting_is_not_a_request(self):
        a = Archive()
        a.say(5, "我家的猫叫汤圆，你不会忘了吧？").say(5, "当然记得汤圆！", "assistant")
        self.assertIn("汤圆", a.recall("我家猫叫什么？"))


class FuzzyMarkerIsNotPremature(unittest.TestCase):
    """问题 4：两周前只说过一次的事就标「很久以前只提过一次，记不太清」，模型会不敢答。"""

    def test_two_weeks_old_fact_is_not_fuzzy(self):
        a = Archive()
        a.say(15, "我搬家了，现在住在成都。")
        context = a.recall("我现在住哪来着？")
        self.assertIn("成都", context)
        self.assertNotIn("记不太清", context)

    def test_old_single_mention_is_still_fuzzy(self):
        a = Archive()
        a.say(90, "我搬家了，现在住在成都。")
        self.assertIn("记不太清", a.recall("我现在住哪来着？"))


class ImplicitChanges(unittest.TestCase):
    """问题 3：不带「更正」字样的改口（搬家/离职/改期/数量），新旧并排、旧的在前、没有标注。"""

    def check(self, said, query, new, old):
        a = Archive()
        for days, text in said:
            a.say(days, text).say(days, "嗯嗯好的呀知道啦", "assistant")
        lines = evidence(a.recall(query))
        new_at = next((i for i, l in enumerate(lines) if new in l), None)
        self.assertIsNotNone(new_at, lines)
        for i, line in enumerate(lines):
            if old in line and new not in line:
                self.assertIn("以新的为准", line, lines)
                # 新说法排在前面；或按时间接续（旧→新），旧行写明「见下一行」、新说法紧随其后。
                if "见下一行" not in line and "这是后来又提到的" not in line:
                    self.assertLess(new_at, i, lines)

    def test_moved_house(self):
        self.check([(60, "我现在住在杭州，离西湖挺近的。"), (15, "我搬家了，现在住在成都。")],
                   "我现在住哪来着？", "成都", "杭州")

    def test_more_cats(self):
        self.check([(45, "我养了两只猫。"), (12, "又捡了一只小猫，现在家里三只猫了。")],
                   "我现在养了几只猫？", "三只", "两只")

    def test_rescheduled(self):
        self.check([(3, "周五要去看牙，智齿疼。"), (1, "看牙改到下周二了，医生周五没空。")],
                   "我什么时候去看牙？", "下周二", "周五要去")

    def test_job_chain(self):
        self.check([(60, "我在字节上班，做产品。"), (25, "跳槽了，从字节去了腾讯。"),
                    (10, "又从腾讯离职了，现在在米哈游。")],
                   "我现在在哪上班？", "米哈游", "去了腾讯")

    def test_her_reply_to_the_old_value_is_marked_too(self):
        a = Archive()
        a.say(45, "我养了两只猫。").say(45, "两只猫好热闹", "assistant")
        a.say(12, "又捡了一只小猫，现在家里三只猫了。").say(12, "猫丁兴旺！", "assistant")
        for line in evidence(a.recall("我现在养了几只猫？")):
            if "两只" in line:
                self.assertIn("以新的为准", line)

    def test_newer_line_without_query_words_still_shows(self):
        # 新说法里没有「上班」，也要跟着旧行一起出现，否则「以新的为准」指向空气。
        self.check([(30, "我在字节上班，做产品，每天都加班。"), (5, "从字节离职了，现在去米哈游了。")],
                   "我在哪上班？", "米哈游", "字节上班")

    def test_change_unrelated_to_the_question_stays_out(self):
        # query_noise 退步：「奶茶我戒了」和「我最喜欢喝奶茶」都在前 8 名时，被当成改口，
        # 新说法绕过过滤混进了问耳机的结果。
        a = Archive()
        a.say(9, "我最喜欢喝奶茶了，基本每天一杯。").say(9, "奶茶党握手～", "assistant")
        a.say(8, "我新买的耳机音质还不错。").say(8, "什么牌子的", "assistant")
        a.say(7, "奶茶我戒了，现在改喝美式。").say(7, "自律了！", "assistant")
        context = a.recall("我新买的耳机怎么样来着？")
        self.assertIn("耳机", context)
        self.assertNotIn("美式", context)

    def test_unrelated_lines_are_not_marked(self):
        a = Archive()
        a.say(20, "我喜欢吃火锅。").say(3, "现在在外面吃饭呢。")
        self.assertNotIn("以新的为准", a.recall("我喜欢吃什么？"))


class CategoryAndHerOwnWords(unittest.TestCase):
    """问题 7：问大类（宠物）只回来一种；问题 6：她自己推荐过的东西查不到。"""

    def test_pets_brings_both_dog_and_cats(self):
        a = Archive()
        a.say(70, "我家的狗叫豆豆，是只柯基。").say(70, "豆豆！小短腿好可爱", "assistant")
        a.say(45, "我养了两只猫。").say(45, "两只猫好热闹", "assistant")
        for n in range(20):
            a.say(30 - n, f"今天加班到{n % 5 + 7}点，好累。")
        context = a.recall("我养了哪些宠物？")
        self.assertIn("豆豆", context)
        self.assertIn("猫", context)

    def test_family_brings_sister(self):
        a = Archive()
        a.say(40, "我妹妹叫小雨，今年读高三。")
        a.say(20, "今天加班到九点，好累。")
        self.assertIn("小雨", a.recall("我家里都有谁来着？"))

    def test_what_she_recommended(self):
        a = Archive()
        a.say(7, "周末好无聊，不知道干啥。").say(7, "你周末可以看看《星际穿越》，很适合放空。", "assistant")
        a.say(6, "最近在追《漫长的季节》，太好看了。").say(6, "那部口碑超好", "assistant")
        a.say(5, "我新买的耳机音质还不错。").say(5, "什么牌子的", "assistant")
        for n in range(6):   # 「试试」这种泛词曾把这句闲聊全拉进来、挤掉目标
            a.say(30 - n * 4, "楼下新开了一家奶茶店，排好长的队。").say(30 - n * 4, "下次去试试", "assistant")
        context = a.recall("你之前推荐我看的那部电影叫啥？")
        self.assertIn("星际穿越", context)
        self.assertNotIn("下次去试试", context)


if __name__ == "__main__":
    unittest.main()
