"""记忆检索回归测试。"""
import unittest

from xuyi.consolidate import refresh_corrections
from xuyi.retrieval import Retriever
from xuyi.strength import rebuild

from tests.test_gauntlet2_fixes import SCOPE, Archive, evidence


def auto(a, query):
    """自动注入那条路（适配器的 --highlight-only）。"""
    refresh_corrections(a.store, SCOPE)
    rebuild(a.store, [SCOPE])
    return Retriever(a.store).recall(SCOPE, query, 900, highlight_only=True)["context"]


class StaleHighlight(unittest.TestCase):
    """①「我不喝酒了」之后说「最近又开始喝点红酒了」，自动重点仍注入「不再喝酒」，还占掉本轮查询。"""

    def setUp(self):
        self.a = Archive()
        self.a.say(70, "我不喝酒了，胃不好。").say(70, "身体要紧", "assistant")
        self.a.say(12, "最近又开始喝点红酒了，睡前一小杯。").say(12, "适量就好", "assistant")

    def test_auto_does_not_inject_the_outdated_value(self):
        self.assertNotIn("不再喝酒", auto(self.a, "我现在还喝酒吗？"))

    def test_lookup_puts_the_new_state_first(self):
        lines = evidence(self.a.recall("我现在还喝酒吗？"))
        self.assertTrue(lines and "红酒" in lines[0], lines)

    def test_unchanged_preference_still_auto_injects(self):
        a = Archive()
        a.say(30, "我最喜欢喝乌龙茶。")
        a.say(5, "今天又加班了，好累。")
        self.assertIn("乌龙茶", auto(a, "我最喜欢喝什么？"))


class MoodRecovery(unittest.TestCase):
    """②「已经走出来了，现在挺好的」不含情绪词，问「最近状态怎么样」查不到。"""

    def test_latest_mood_comes_back(self):
        a = Archive()
        a.say(20, "最近失恋了，很难过。").say(20, "抱抱你", "assistant")
        a.say(10, "今天加班到九点，好累。")
        a.say(3, "已经走出来了，现在挺好的。").say(3, "真为你高兴", "assistant")
        a.say(2, "中午吃了楼下的黄焖鸡，还行。")
        lines = evidence(a.recall("我最近状态怎么样？"))
        self.assertTrue(any("走出来" in line for line in lines), lines)
        first = next(i for i, l in enumerate(lines) if "走出来" in l)
        for i, line in enumerate(lines):
            if "失恋" in line:
                self.assertLess(first, i, lines)


class DateRanges(unittest.TestCase):
    """③「上个月 / 上周 / 前几天」只认了昨天前天，「上个月」反被「下个月」的行干扰。"""

    def test_last_month(self):
        from datetime import datetime
        today = datetime.now().astimezone().date()
        # 剧本里是 25 天前；取「上个月 15 号」所在的天数，保证落在上个月
        last = (today.replace(day=1) - __import__("datetime").timedelta(days=1)).replace(day=15)
        days = (today - last).days
        a = Archive()
        a.say(days, "今天去了趟迪士尼，排队排死了。")
        a.say(4, "下个月15号我要考雅思。")
        a.say(3, "我妈下个月要做个小手术。")
        context = a.recall("上个月我去哪玩了？")
        self.assertIn("迪士尼", context)
        self.assertNotIn("雅思", context)

    def test_a_few_days_ago(self):
        a = Archive()
        a.say(3, "今天去看了莫奈的展，人超多。")
        a.say(40, "去看了一个梵高的展。")
        context = a.recall("我前几天去看的什么展？")
        self.assertIn("莫奈", context)
        self.assertNotIn("梵高", context)


class HerPromises(unittest.TestCase):
    """④她答应的「周五提醒你交报告」，问「你答应过我什么」查不到。"""

    def test_what_she_promised(self):
        a = Archive()
        for n in range(6):
            a.say(30 - n * 4, "楼下新开了一家奶茶店，排好长的队。").say(30 - n * 4, "下次去试试", "assistant")
        a.say(2, "周五要交季度报告，你到时候提醒我一下。").say(2, "好，周五提醒你交报告", "assistant")
        self.assertIn("提醒你交报告", a.recall("你之前答应过我啥？"))


class RealDeviceFindings(unittest.TestCase):
    """2026-11-11 真机：问「上礼拜我干啥了」，模型把查询改写成「上礼拜 最近干啥 忙不忙」。"""

    def test_the_message_being_answered_is_not_evidence(self):
        a = Archive()
        a.say(2, "今天去看了莫奈的展。")
        a.say(0, "上礼拜我干啥了")
        self.assertNotIn("上礼拜我干啥了", a.recall("上礼拜 最近干啥 忙不忙"))

    def test_empty_span_says_so_instead_of_dumping_chatter(self):
        from datetime import datetime
        today = datetime.now().astimezone().date()
        a = Archive()
        for n in range(today.weekday() + 1):   # 只有本周的闲聊，上周一条没有
            a.say(n, "好久不见").say(n, "哟，还舍得回来", "assistant")
        context = a.recall("上礼拜 最近干啥 忙不忙")
        self.assertNotIn("好久不见", context)
        self.assertIn("没有聊天记录", context)

    def test_change_within_the_same_chat_counts(self):
        a = Archive()
        # 同一次聊天里几分钟就改口很常见；原来要求新旧相隔 1 小时以上才认。
        a.say(1, "我现在住在杭州。", hour=9)
        a.say(1, "我上个月刚搬家，现在住在成都。", hour=10)
        lines = evidence(a.recall("我现在住哪来着？"))
        self.assertTrue(lines and "成都" in lines[0], lines)
        old = [l for l in lines if "杭州" in l and "成都" not in l]
        self.assertTrue(old, lines)   # 旧说法确实被查到了，这条断言才有意义
        self.assertTrue(all("以新的为准" in l for l in old), lines)


class RealDeviceRound2(unittest.TestCase):
    """2026-11-11 真机第二轮（问「你之前答应过我啥」）与真实档案里的奶茶那段。"""

    def test_line_brought_in_beside_a_reply_keeps_its_correction_flag(self):
        a = Archive()
        a.say(2, "我家的猫叫年糕", hour=9).say(2, "年糕这名字真萌，是你家的吗？", "assistant", hour=9)
        a.say(2, "更正一下，我家的猫不叫年糕，叫汤圆", hour=10).say(2, "好嘞，汤圆！", "assistant", hour=10)
        for line in evidence(a.recall("年糕这名字可爱吗")):
            if "年糕" in line and "汤圆" not in line:
                self.assertRegex(line, "后来有更正|以新的为准", line)

    def test_what_did_i_do_is_a_question(self):
        a = Archive()
        a.say(0, "上礼拜我干啥了", hour=0).say(0, "上礼拜？你啥都没跟我说啊。", "assistant", hour=0)
        a.say(0, "你之前答应过我啥")
        self.assertNotIn("上礼拜我干啥了", a.recall("答应 请你 请客 欠你 说好"))

    def test_no_line_appears_twice(self):
        a = Archive()
        a.say(2, "昨天你让我喝哪家奶茶", hour=9).say(2, "蛋挞配奶茶那个呀，别要全糖的", "assistant", hour=9)
        a.say(2, "有啥推荐吗", hour=10).say(2, "奶茶配个蛋挞吧，别要全糖的", "assistant", hour=10)
        lines = evidence(a.recall("奶茶 推荐 蛋挞"))
        self.assertEqual(len(lines), len(set(lines)), lines)

    def test_her_hedges_are_not_evidence(self):
        a = Archive()
        a.say(2, "哪家的好喝你说说", hour=9).say(2, "云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻", "assistant", hour=9)
        a.say(1, "昨天你让我喝哪家奶茶", hour=9).say(1, "我记不清昨天说的是哪家了，就随便说了句奶茶配蛋挞", "assistant", hour=9)
        a.say(1, "品牌啊", hour=10).say(1, "是我记岔了，可到底是哪家，我实在想不起来了。", "assistant", hour=10)
        context = a.recall("你推荐我喝什么奶茶来着")
        self.assertIn("桂花乌龙", context)
        self.assertNotIn("真没提牌子", context)
        self.assertNotIn("真想不起来", context)


class UpcomingWeekendIsNotThisWeek(unittest.TestCase):
    """第四批盲测发现的退步：「这周末」被 query_span 当成「这周（周一到今天）」，上周说的计划被滤掉。

    验证题故意不用盲测原句（盲测那批不拿来调）。说计划的那天固定落在上周，保证修前必失败。
    """

    def setUp(self):
        from datetime import datetime
        self.last_week = datetime.now().astimezone().weekday() + 1   # 上周日

    def test_this_weekend(self):
        a = Archive()
        a.say(self.last_week, "周末准备去露营，帐篷都买好了。")
        a.say(1, "中午吃了楼下的黄焖鸡，还行。")
        self.assertIn("露营", a.recall("我这周末有啥安排？"))

    def test_this_saturday(self):
        a = Archive()
        a.say(self.last_week, "这周六约了牙医，下午两点。")
        self.assertIn("牙医", a.recall("这周六我要干嘛来着？"))

    def test_this_week_so_far_still_means_monday_to_today(self):
        from xuyi.retrieval import query_span
        from datetime import datetime, timedelta
        today = datetime.now().astimezone().date()
        self.assertEqual(query_span("这周我都干了啥"), (today - timedelta(days=today.weekday()), today))
        self.assertIsNone(query_span("这周末去哪玩"))
        self.assertIsNone(query_span("本周六有空吗"))


if __name__ == "__main__":
    unittest.main()
