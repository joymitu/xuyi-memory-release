"""体重这类「新说法和问句、旧说法没有一个共同字」的数值变化：没有语义模型时也要连上，新旧都带出来并标明以新的为准。

（第四批盲测的「体重 120→110」没有模型时只递了旧值；验证用的是换了说法的句子，不拿盲测原句调。）
"""
import unittest

from xuyi.consolidate import refresh_corrections
from xuyi.retrieval import Retriever, same_subject
from xuyi.strength import rebuild

from tests.test_gauntlet2_fixes import SCOPE, Archive, evidence
from tests.test_later_mentions import chatter


def recall(a, query):
    refresh_corrections(a.store, SCOPE)
    rebuild(a.store, [SCOPE])
    return Retriever(a.store).recall(SCOPE, query, 1600, focus=True)["context"]


class WeightLink(unittest.TestCase):
    def test_new_weight_without_shared_characters_is_linked(self):
        a = Archive()
        a.say(70, "我体重65公斤，想减一减。").say(70, "慢慢来", "assistant")
        chatter(a, [60, 50, 40])
        a.say(15, "最近瘦到60了！").say(15, "太强了", "assistant")
        chatter(a, [10, 5, 2])
        context = recall(a, "我现在多少斤？")
        lines = evidence(context)
        self.assertTrue(any("瘦到60" in l for l in lines), lines)
        self.assertIn("以新的为准", context)

    def test_other_wordings_of_the_question_also_link(self):
        a = Archive()
        a.say(70, "我体重130斤。").say(70, "嗯", "assistant")
        chatter(a, [60, 50, 40])
        a.say(15, "胖到140了，哭。").say(15, "抱抱", "assistant")
        chatter(a, [10, 5, 2])
        self.assertTrue(any("胖到140" in l for l in evidence(recall(a, "我最近体重多少？"))))

    def test_groceries_are_not_weight(self):
        self.assertFalse(same_subject("我体重65公斤。", "今天买了两斤苹果，花了20块。", "我现在多重？"))
        self.assertFalse(same_subject("我体重65公斤。", "存到5万了！", "我现在多重？"))
        self.assertTrue(same_subject("我体重65公斤。", "最近瘦到60了！", "我现在多重？"))

    def test_questions_not_about_weight_do_not_link(self):
        self.assertFalse(same_subject("我体重65公斤。", "最近瘦到60了！", "我存了多少钱？"))


if __name__ == "__main__":
    unittest.main()
