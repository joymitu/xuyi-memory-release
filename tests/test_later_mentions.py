"""「后来又提到」：查到一件旧事时，把后来说到同一件事的话一起带回来。

来自第四批盲测的三类失败（体重 120→110、女朋友→分手、别叫我宝宝），但验证题全部换了说法，
不拿盲测原句来调。数值变化要靠语义模型，那几条在没有模型时跳过。
"""
import os
import unittest

from xuyi.consolidate import refresh_corrections
from xuyi.retrieval import Retriever
from xuyi.strength import rebuild

from tests.test_gauntlet2_fixes import SCOPE, Archive, evidence

MODEL = os.environ.get("XUYI_MODEL_PATH")


def recall(a, query, embedder=None):
    refresh_corrections(a.store, SCOPE)
    rebuild(a.store, [SCOPE])
    return Retriever(a.store, embedder).recall(SCOPE, query, 1600, focus=True)["context"]


_EMBEDDER = None


def embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        from xuyi.embeddings import OnnxEmbedder
        _EMBEDDER = OnnxEmbedder(MODEL)
    return _EMBEDDER


def chatter(a, days):
    for n, d in enumerate(days):
        a.say(d, ["中午吃了楼下的黄焖鸡。", "今天地铁好挤。", "同事送了我一盒巧克力。"][n % 3])


class SharedName(unittest.TestCase):
    """后来的话里出现了同一个专有名字（档案里很少见的词）——不需要模型。"""

    def test_relationship_ended(self):
        a = Archive()
        a.say(70, "我男朋友叫阿凯，在一起三年了。").say(70, "好甜", "assistant")
        chatter(a, [60, 50, 40])
        a.say(18, "跟阿凯吹了。").say(18, "抱抱", "assistant")
        chatter(a, [10, 5, 2])
        lines = evidence(recall(a, "我男朋友叫啥来着？"))
        self.assertTrue(any("吹了" in l for l in lines), lines)

    def test_word_fragments_are_not_names(self):
        # 三批复测里的错连：两句都含「叫阿」（跨词的碎片），被当成了罕见专有名字。
        a = Archive()
        a.say(80, "我大学室友叫阿强，现在在深圳。", hour=9)
        a.say(80, "我高中同桌叫阿伟，在北京读研。", hour=10)
        a.say(5, "中午吃了楼下的黄焖鸡。")
        context = recall(a, "我大学室友现在在哪个城市？")
        self.assertNotIn("后来又提到", context)

    def test_common_words_do_not_link(self):
        a = Archive()
        a.say(30, "我男朋友叫阿凯。")
        a.say(10, "今天男朋友加班，我一个人吃饭。")   # 「男朋友」到处都有，不算专有名字
        a.say(5, "中午吃了楼下的黄焖鸡。")
        context = recall(a, "我男朋友叫啥来着？")
        self.assertNotIn("后来又提到", context)


@unittest.skipUnless(MODEL, "需要 XUYI_MODEL_PATH")
class SemanticFollowUp(unittest.TestCase):
    """后来的话和旧事没有共同的词，只能靠语义（且要更晚、够近）。"""

    def test_number_changed(self):
        a = Archive()
        a.say(60, "我房租一个月3800，压力好大。").say(60, "不便宜啊", "assistant")
        chatter(a, [50, 40, 30])
        a.say(9, "房东涨租了，现在要4200。").say(9, "啊这", "assistant")
        chatter(a, [6, 3, 1])
        lines = evidence(recall(a, "我现在房租多少？", embedder()))
        self.assertTrue(any("4200" in l for l in lines), lines)
        # 数值变了就是更新：旧的 3800 要标「以新的为准」，不是中性的「后来又提到过」。
        self.assertTrue(all("以新的为准" in l for l in lines if "3800" in l), lines)

    @unittest.expectedFailure
    def test_very_terse_update_is_a_known_miss(self):
        # 已知做不到：「涨了！现在一个月一万二」和「我月薪8000」语义只有 0.62，低于带数字的门槛 0.70；
        # 门槛不能再低——「月薪8000」和「房租3800」、「体重120」和「身高165」都在 0.68 左右。
        a = Archive()
        a.say(60, "我月薪8000，刚够花。").say(60, "加油", "assistant")
        chatter(a, [50, 40, 30])
        a.say(9, "涨了！现在一个月一万二。").say(9, "恭喜！", "assistant")
        chatter(a, [6, 3, 1])
        # 「一万二」那句有时会作为排序的第二个猜测出现，那是运气；这里只看它有没有被「连上」。
        self.assertIn("后来又提到", recall(a, "我现在工资多少？", embedder()))

    def test_different_numbers_do_not_link(self):
        for old, other, query in [("我月薪8000，刚够花。", "我房租一个月3800，压力好大。", "我工资多少？"),
                                  ("我体重120斤，想减一减。", "我身高165。", "我多重？")]:
            with self.subTest(old=old):
                a = Archive()
                a.say(40, old)
                a.say(10, other)
                a.say(5, "中午吃了楼下的黄焖鸡。")
                self.assertNotIn("后来又提到", recall(a, query, embedder()))

    def test_similar_but_different_things_do_not_link(self):
        a = Archive()
        a.say(40, "我家的猫叫汤圆。")
        a.say(10, "我家狗叫豆豆。")
        a.say(5, "中午吃了楼下的黄焖鸡。")
        context = recall(a, "我家猫叫啥？", embedder())
        # 「豆豆」作为普通检索结果出现可以（和问题共有「我家…叫」），但不能被当成猫的「后来又提到」。
        self.assertNotIn("后来又提到", context)


class HowToAddressMe(unittest.TestCase):
    """问「怎么称呼我」，原话说的是「别喊我…」「叫我…」。"""

    def test_dont_call_me(self):
        a = Archive()
        a.say(40, "我同事叫老张，大家都喊他张哥。").say(40, "张哥听着挺靠谱", "assistant")
        a.say(30, "以后别喊我小可爱了，肉麻。").say(30, "好吧", "assistant")
        chatter(a, [25, 20, 15, 10, 8, 5, 3, 1])
        self.assertIn("小可爱", recall(a, "你一般该怎么称呼我？"))


if __name__ == "__main__":
    unittest.main()
