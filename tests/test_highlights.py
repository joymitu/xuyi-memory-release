import unittest

from xuyi.retrieval import Retriever
from xuyi.store import Store


PEER = 'a' * 64
SCOPE = 'qq:c2c:' + PEER
QUERY = '我的宠物叫什么？'


def event(seq, text, **overrides):
    fields = dict(
        scope=SCOPE, sender_id=PEER, role='user', origin='quoted', text=text,
        event_id=f'dsh:session:{seq}', recorded_at=f'2026-01-01T00:00:{seq:02d}Z',
    )
    return dict(fields, **overrides)


class HighlightTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def recall(self, query=QUERY, **options):
        return Retriever(self.store).recall(SCOPE, query, **options)

    def highlighted(self, query=QUERY, **options):
        return [p for p in self.recall(query, **options)['sources'] if 'unverified_current_highlight' in p]

    def test_correction_wins_in_event_time_and_keeps_original_for_history(self):
        old = event(1, '我的猫叫甲号。')
        new = event(2, '更正一下，我的猫叫乙号。')
        self.store.add_many([new, old, old])
        out = self.recall(budget=1600)
        first = out['sources'][0]
        self.assertEqual(first['unverified_current_highlight']['值'], '乙号')
        self.assertEqual(first['text'], new['text'])
        self.assertEqual(first['event_id'], new['event_id'])
        self.assertNotIn('甲号', out['context'])
        self.assertEqual(self.store.get(SCOPE, old['event_id']), old)
        past = self.recall('我的猫以前叫甲号吗？')
        self.assertIn('甲号', past['context'])
        self.assertNotIn('unverified_current_highlight', past['context'])

    def test_abstains_on_competing_or_irrelevant_names(self):
        self.store.add(event(1, '我的猫叫甲号。'))
        self.assertEqual(self.highlighted('我朋友的猫叫什么？'), [])
        self.assertEqual(self.highlighted('我的猫今天干嘛？'), [])
        self.store.add(event(2, '我的猫叫乙号。'))
        self.assertEqual(self.highlighted(), [])
        self.store.add(event(3, '更正，我的猫叫乙号。'))
        self.assertEqual(self.highlighted()[0]['unverified_current_highlight']['值'], '乙号')
        self.store.add(event(4, '我的狗叫丙号。'))
        self.assertEqual(self.highlighted(), [])
        self.assertEqual(self.highlighted('我的猫叫什么？')[0]['unverified_current_highlight']['值'], '乙号')

    def test_composite_hypothetical_question_and_assistant_are_not_promoted(self):
        self.store.add_many([
            event(1, '[Quoted message begins]\n我的猫叫甲号。\n[Current message]\n没说过'),
            event(2, '[Voice message]\n我的猫叫甲号。'),
            event(3, '如果我的猫叫甲号。'),
            event(4, '我的猫叫甲号？'),
            event(5, '我的猫叫甲号吧。'),
            event(6, '我的猫叫甲号。', role='assistant', origin='assistant'),
            event(7, '我的猫叫甲号。', sender_id='other'),
            event(8, '我的猫叫甲号。', scope='qq:group:fake'),
            event(9, '刚才说错了，叫甲号。'),
        ])
        self.assertEqual(self.highlighted(), [])

    def test_budget_deletion_and_restore_follow_the_source(self):
        row = event(1, '以后叫我测试。')
        self.store.add(row)
        query = '以后怎么称呼我？'
        self.assertEqual(self.highlighted(query, budget=1), [])
        self.assertEqual(self.highlighted(query, budget=1600)[0]['unverified_current_highlight']['值'], '测试')
        backup = self.store.export_bundle(SCOPE)
        with Store(':memory:') as restored:
            restored.restore(backup)
            self.assertIn('unverified_current_highlight', Retriever(restored).recall(SCOPE, query)['context'])
        self.store.forget(SCOPE, row['event_id'])
        self.store.restore(backup)
        self.assertEqual(self.highlighted(query), [])
        self.assertNotIn('测试', self.recall(query)['context'])

    def test_fast_lookup_returns_only_an_unambiguous_current_source(self):
        self.store.add_many([
            event(1, '我的猫叫甲号。'),
            event(2, '更正一下，我的猫叫乙号。'),
            event(3, '助手随便猜了一个宠物名字。', role='assistant', origin='assistant'),
        ])
        result = self.recall(budget=900, highlight_only=True)
        self.assertEqual([source['event_id'] for source in result['sources']], ['dsh:session:2'])
        self.assertEqual(result['used'], len(result['context'].encode()))
        self.assertLessEqual(result['used'], 900)
        self.assertEqual(self.recall('我的猫以前叫甲号吗？', highlight_only=True)['context'], '')
        self.assertEqual(self.recall(budget=1, highlight_only=True)['context'], '')


PREF_QUERY = '我现在最爱喝什么？'
APPT_QUERY = '和小林的安排还要赴约吗？'


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def recall(self, query=PREF_QUERY, **options):
        return Retriever(self.store).recall(SCOPE, query, **options)

    def highlighted(self, query=PREF_QUERY, **options):
        return [p for p in self.recall(query, **options)['sources'] if 'unverified_current_highlight' in p]

    def test_change_statement_becomes_current_with_source(self):
        self.store.add_many([
            event(1, '我以前最喜欢咖啡。'),
            event(2, '从今天起，我最喜欢的饮料改成茶了。'),
        ])
        out = self.highlighted()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['unverified_current_highlight'], {'属性': '最喜欢的饮品', '值': '茶'})
        self.assertEqual(out[0]['event_id'], 'dsh:session:2')
        self.assertIn('改成茶', self.recall('我最喜欢喝什么？')['context'])

    def test_verb_form_change_replaces_the_verb_form_favourite(self):
        # "我最喜欢喝的改成…" 的类别是"喝"（饮品），不是一个叫"喝的"的新类别。
        self.store.add_many([event(1, '我最喜欢喝奶茶。'), event(2, '我最喜欢喝的改成美式了。'),
                             event(3, '我最爱吃的换成火锅了。')])
        for query in ('我最喜欢喝什么？', '你还记得我最喜欢喝什么吗？'):
            out = self.highlighted(query)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]['unverified_current_highlight'], {'属性': '最喜欢的饮品', '值': '美式'})
            self.assertNotIn('我最喜欢喝奶茶', self.recall(query)['context'])
        self.assertEqual(self.highlighted('我最爱吃什么？')[0]['unverified_current_highlight'],
                         {'属性': '最喜欢的食物', '值': '火锅'})

    def test_past_taste_never_becomes_current(self):
        self.store.add(event(1, '我以前最喜欢咖啡。'))
        self.assertEqual(self.highlighted(), [])
        self.assertEqual(self.highlighted('我最喜欢的饮料是什么？'), [])

    def test_plain_competing_values_abort(self):
        self.store.add(event(1, '我最喜欢的饮料是茶。'))
        self.assertEqual(self.highlighted('我最喜欢的饮料是什么？')[0]['unverified_current_highlight']['值'], '茶')
        self.store.add(event(2, '我最喜欢的饮料是咖啡。'))
        self.assertEqual(self.highlighted('我最喜欢的饮料是什么？'), [])
        self.store.add(event(3, '更正一下，我最喜欢的饮料是气泡水。'))
        self.assertEqual(self.highlighted('我最喜欢的饮料是什么？')[0]['unverified_current_highlight']['值'], '气泡水')

    def test_negation_and_category_routing(self):
        self.store.add(event(1, '我不再喝奶茶了。'))
        out = self.highlighted('我还喝奶茶吗？')
        self.assertEqual(out[0]['unverified_current_highlight']['值'], '不再喝奶茶')
        self.assertEqual(self.highlighted('我最喜欢的颜色是什么？'), [])

    def test_appointment_cancel_carries_both_sources(self):
        self.store.add_many([
            event(1, '我本来约好周六见小林。'),
            event(2, '周六见小林的约定取消了。'),
        ])
        result = self.recall(APPT_QUERY)
        first = result['sources'][0]
        self.assertEqual(first['unverified_current_highlight'], {'属性': '和小林的约定', '值': '周六，已取消'})
        self.assertEqual(first['event_id'], 'dsh:session:2')
        self.assertIn('dsh:session:1', {source['event_id'] for source in result['sources']})
        past = self.recall('当时和小林约了什么？')
        self.assertNotIn('unverified_current_highlight', past['context'])
        self.assertIn('周六见小林', past['context'])

    def test_appointment_reschedule_and_missing_target(self):
        self.store.add(event(1, '周六见小林。'))
        self.assertEqual(self.highlighted(APPT_QUERY)[0]['unverified_current_highlight']['值'], '周六')
        self.store.add(event(2, '周六的约定改到周日了。'))
        out = self.highlighted('和小林的安排还要赴约吗？')
        self.assertEqual(out[0]['unverified_current_highlight'], {'属性': '和小林的约定', '值': '周六改到周日'})
        self.store.add(event(3, '周日见小王。'))
        self.assertEqual(self.highlighted('和小王的安排还在吗？')[0]['unverified_current_highlight']['值'], '周日')
        self.assertEqual(self.highlighted('周日的安排还在吗？'), [])
        self.store.add(event(4, '取消了。'))
        self.assertEqual(self.highlighted('周日见小王的约定还有吗？'), [])

    def test_tentative_and_foreign_owner_appointments_are_ignored(self):
        self.store.add_many([
            event(1, '我打算周六见小林。'),
            event(2, '朋友约了小林周六。'),
            event(3, '周六见小林吧。'),
        ])
        self.assertEqual(self.highlighted(APPT_QUERY), [])

    def test_family_routes_exactly_one(self):
        self.store.add_many([
            event(1, '我最喜欢的饮料是茶。'),
            event(2, '周六见小林。'),
        ])
        self.assertEqual(self.highlighted('我最喜欢的饮料和小林的约定？'), [])
        self.assertEqual(self.highlighted('我现在最爱喝什么？')[0]['event_id'], 'dsh:session:1')
        self.assertEqual(self.highlighted('周末有什么安排？')[0]['event_id'], 'dsh:session:2')


OPEN_QUERY = '最近有什么打算？'


class OpenLoopTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def recall(self, query=OPEN_QUERY, **options):
        return Retriever(self.store).recall(SCOPE, query, **options)

    def highlighted(self, query=OPEN_QUERY, **options):
        return [p for p in self.recall(query, **options)['sources'] if 'unverified_current_highlight' in p]

    def test_intention_becomes_followup_hook(self):
        self.store.add(event(1, '我下周要考试。'))
        out = self.highlighted()
        self.assertEqual(out[0]['unverified_current_highlight'], {'属性': '想做的事', '值': '考试'})
        self.store.add(event(2, '我打算买switch。'))
        self.assertEqual(self.highlighted(), [])
        self.assertEqual(self.highlighted('我打算买的东西还记得吗？'), [])
        self.assertEqual(self.highlighted('考试准备得怎么样？')[0]['unverified_current_highlight']['值'], '考试')

    def test_close_statement_resolves_loop(self):
        self.store.add_many([event(1, '我打算考驾照。'), event(2, '驾照考完了。')])
        self.assertEqual(self.highlighted(), [])
        self.assertEqual(self.highlighted('最近有什么打算？'), [])

    def test_garbage_intentions_are_not_promoted(self):
        self.store.add_many([
            event(1, '我要疯了。'),
            event(2, '我想你了。'),
            event(3, '我要回家。'),
        ])
        self.assertEqual(self.highlighted(), [])

    def test_uncertain_and_history_are_ignored(self):
        self.store.add(event(1, '我打算去旅游。'))
        self.store.add(event(2, '我可能要考试。'))
        # The uncertain statement never becomes a loop; the remaining single
        # open loop is unambiguous and answers the generic query.
        self.assertEqual(self.highlighted('最近有什么打算？')[0]['unverified_current_highlight']['值'], '去旅游')
        # Open loops are still current even when asked about via 之前 — the
        # loop was never closed, so answering it is the natural behavior.
        self.assertEqual(self.highlighted('我之前说要做什么来着？')[0]['unverified_current_highlight']['值'], '去旅游')


EMO_QUERY = '我今天心情怎么样？'


class EmotionTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def highlighted(self, query=EMO_QUERY, **options):
        return [p for p in Retriever(self.store).recall(SCOPE, query, **options)['sources']
                if 'unverified_current_highlight' in p]

    def test_self_report_answers_and_latest_wins(self):
        self.store.add(event(1, '我今天好开心。'))
        self.assertEqual(self.highlighted()[0]['unverified_current_highlight']['值'], '开心')
        self.store.add(event(2, '我有点累了，因为加班。'))
        self.assertEqual(self.highlighted()[0]['unverified_current_highlight']['值'], '累')

    def test_hedged_feelings_are_not_promoted(self):
        self.store.add(event(1, '我可能有点难过。'))
        self.assertEqual(self.highlighted(), [])

    def test_snapshot_never_carries_emotions(self):
        from xuyi.consolidate import snapshot
        self.store.add(event(1, '我今天好开心。'))
        self.assertNotIn('开心', snapshot(self.store, SCOPE)['context'])
        self.assertIn('开心', self.highlighted()[0]['text'])


class SupersededAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_history_recall_marks_corrected_statements(self):
        from xuyi.consolidate import refresh_corrections
        self.store.add_many([
            event(1, '我的猫叫甲号。'),
            event(2, '更正一下，我的猫叫乙号。'),
        ])
        self.assertEqual(refresh_corrections(self.store, SCOPE), 1)
        out = Retriever(self.store).recall(SCOPE, '我的猫以前叫甲号吗？', 1600)
        line = [l for l in out['context'].splitlines() if '甲号' in l and l.startswith('[')]
        self.assertTrue(line)
        self.assertIn('此说法后来有更正', line[0])
        fresh = [l for l in out['context'].splitlines() if '乙号' in l and l.startswith('[')]
        if fresh:
            self.assertNotIn('此说法后来有更正', fresh[0])


class PersonaPresetTests(unittest.TestCase):
    def test_ten_presets_are_pure_personas_with_examples(self):
        from xuyi.personas import list_personas, load_persona
        names = [item["name"] for item in list_personas()]
        self.assertGreaterEqual(len(names), 10)
        self.assertEqual(len([n for n in names]), len(set(names)))
        for name in names:
            data = load_persona(name)
            prompt = data["systemPrompt"]
            self.assertGreater(len(prompt), 200, name)
            # 例句驱动（AI 味研究的元发现），每个预设都有示范对话
            self.assertIn("——", prompt, name)
            # 纯人设：不得替记忆系统说话
            for banned in ("续忆", "档案", "检索", "系统会给你", "旧事原文"):
                self.assertNotIn(banned, prompt, f"{name}: {banned}")
            # 有分寸段（AI 味反模式）；「一句话别回五行、别老反问、意见最多一条」2026-11-11 起挪进
            # 所有人设共用的 CHAT_STYLE，每个人设都会带上。
            self.assertIn("【他不同状态时】", prompt, name)
            from xuyi.personas import CHAT_STYLE
            self.assertIn("对话的分寸", CHAT_STYLE)
            # 无公认 AI 套话
            for flavor in ("我理解你的感受", "值得注意的是", "首先", "愿你", "闪闪发光"):
                self.assertNotIn(flavor, prompt, f"{name}: {flavor}")




class DietFamilyTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def current(self, query):
        out = Retriever(self.store).recall(SCOPE, query, 900, highlight_only=True)
        return next((p['unverified_current_highlight'] for p in out['sources']
                     if 'unverified_current_highlight' in p), None)

    def test_eating_turns_bring_back_every_live_restriction_with_sources(self):
        self.store.add_many([event(1, '我对芒果过敏，吃了会起疹子。'), event(2, '我不吃香菜。')])
        self.assertEqual(self.current('推荐个外卖呗'), {'属性': '忌口/过敏', '值': '对芒果过敏；不吃香菜'})
        out = Retriever(self.store).recall(SCOPE, '晚上吃点什么好？', 900, highlight_only=True)
        self.assertEqual({p['event_id'] for p in out['sources']}, {'dsh:session:1', 'dsh:session:2'})
        self.assertIsNone(self.current('推荐个电影呗'))

    def test_lifting_and_correcting_a_restriction(self):
        self.store.add_many([event(1, '我对芒果过敏。'), event(2, '不对，我是对菠萝过敏，不是芒果。'),
                             event(3, '我不能吃辣。'), event(4, '现在能吃一点辣了。')])
        self.assertEqual(self.current('我能吃什么？')['值'], '对菠萝过敏')

    def test_taste_questions_stay_with_the_preference_family(self):
        self.store.add_many([event(1, '我对芒果过敏。'), event(2, '我最喜欢吃火锅。')])
        self.assertEqual(self.current('我最喜欢吃什么？')['值'], '火锅')
        self.assertEqual(self.current('我能不能吃芒果？')['值'], '对芒果过敏')

    def test_others_past_hypothetical_and_questions_are_not_restrictions(self):
        self.store.add_many([event(1, '我妈对花生过敏。'), event(2, '我以前对芒果过敏，现在好了。'),
                             event(3, '要是我对虾过敏就惨了。'), event(4, '我是不是对牛奶过敏啊？'),
                             event(5, '我不吃香菜了。')])
        self.assertIsNone(self.current('推荐个吃的'))

    def test_adapter_trigger_mirrors_the_python_query(self):
        import re
        from pathlib import Path
        from xuyi.highlights import FAMILIES
        source = (Path(__file__).resolve().parents[1] / 'dsh-adapter/logic.js').read_text(encoding='utf-8')
        js = re.search(r"const dietQuestion = /(.+)/;", source).group(1)
        self.assertEqual(js, FAMILIES[5].QUERY.pattern)
