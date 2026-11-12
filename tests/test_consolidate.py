import datetime as dt
import unittest

from xuyi import index

from xuyi.consolidate import consolidate, probes, snapshot
from xuyi.store import Store


SCOPE = 'qq:c2c:' + 'a' * 64


def event(mid, text, **extra):
    fields = dict(scope=SCOPE, event_id='dsh:session:' + mid, sender_id='a' * 64, role='user',
                  origin='quoted', recorded_at='2026-11-07T12:00:%02d+08:00' % int(mid[1:]),
                  text=text)
    return dict(fields, **extra)


class ConsolidateTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def status_by_id(self, report):
        return {row['event_id'].removeprefix('dsh:session:'): row['status']
                for row in report['probes']}

    def test_probe_classifies_current_replaced_and_shadowed(self):
        self.store.add_many([
            event('m1', '请叫我小渔。'),
            event('m2', '更正一下，请叫我大肥鱼。'),
            event('m3', '我的猫叫甲号。'),
            event('m4', '我的猫叫乙号。'),
            event('m5', '周六见小林。'),
        ])
        report = consolidate(self.store, SCOPE)
        status = self.status_by_id(report)
        self.assertEqual(status['m1'], '已被更替')
        self.assertEqual(status['m2'], '当前候选')
        self.assertEqual(status['m3'], '存而不达')
        self.assertEqual(status['m4'], '存而不达')
        self.assertEqual(status['m5'], '当前候选')
        self.assertTrue(report['index']['fresh_after'])

    def test_rebuilds_stale_index_and_stays_idempotent(self):
        self.store.add_many([event('m1', '请叫我小渔。'), event('m2', '周六见小林。')])
        first = consolidate(self.store, SCOPE)
        self.assertFalse(first['index']['fresh_before'])
        self.assertTrue(first['index']['fresh_after'])
        self.assertTrue(index.fresh(self.store, SCOPE))
        second = consolidate(self.store, SCOPE)
        self.assertTrue(second['index']['fresh_before'])
        stripped = lambda report: {k: v for k, v in report.items()}
        self.assertEqual(stripped(first)['probes'], stripped(second)['probes'])
        self.assertEqual(stripped(first)['audit'], stripped(second)['audit'])

    def test_correction_keeps_history_reachable_for_history_queries(self):
        self.store.add_many([event('m1', '我的猫叫甲号。'),
                             event('m2', '更正一下，我的猫叫乙号。')])
        report = probes(self.store, SCOPE)
        self.assertEqual({row['event_id'].removeprefix('dsh:session:'): row['status']
                          for row in report},
                         {'m1': '已被更替', 'm2': '当前候选'})

    def test_snapshot_covers_every_unambiguous_family_within_budget(self):
        self.store.add_many([
            event('m1', '请叫我小渔。'),
            event('m2', '从今天起，我最喜欢的饮料改成茶了。'),
            event('m3', '周六见小林。'),
            event('m4', '周六见小林的约定取消了。'),
        ])
        out = snapshot(self.store, SCOPE, budget=900)
        self.assertIn('请叫我小渔', out['context'])
        self.assertIn('改成茶', out['context'])
        self.assertIn('已取消', out['context'])
        self.assertIn('unverified_current_highlight', out['context'])
        self.assertLessEqual(out['used'], 900)
        self.assertEqual(out['used'], len(out['context'].encode()))
        self.assertEqual(len(out['sources']), 3)

    def test_snapshot_stays_silent_without_candidates(self):
        self.store.add(event('m1', '今天天气不错。'))
        self.assertEqual(snapshot(self.store, SCOPE)['context'], '')
        self.assertEqual(snapshot(self.store, 'qq:group:fake')['context'], '')

    def test_snapshot_budget_drops_lowest_priority_last(self):
        self.store.add_many([
            event('m1', '请叫我小渔。'),
            event('m2', '从今天起，我最喜欢的饮料改成茶了。'),
        ])
        tight = snapshot(self.store, SCOPE, budget=400)
        self.assertTrue(tight['context'])
        self.assertLessEqual(tight['used'], 400)
        self.assertLess(len(tight['sources']), 2)

    def test_upcoming_lists_live_appointments_with_dates(self):
        self.store.add_many([
            event('m1', '周三见小林。'),
            event('m2', '周五见小王。'),
            event('m3', '周五的约定改到周六了。'),
            event('m4', '周六见小赵的约定取消了。'),
        ])
        report = consolidate(self.store, SCOPE, today=dt.date(2026, 11, 7))
        rows = {(row['who'], row['时间']): row for row in report['upcoming']}
        self.assertEqual(set(rows), {('小林', '周三'), ('小王', '周六')})
        self.assertEqual(rows[('小林', '周三')]['date'], '2026-11-11')
        self.assertEqual(rows[('小王', '周六')]['date'], '2026-11-14')
        self.assertEqual(report['upcoming'][0]['who'], '小林')


if __name__ == '__main__':
    unittest.main()


class DueReminderTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_due_appointment_injected_once_per_date(self):
        import datetime as dt
        self.store.add(event('m1', '明天见小林。'))
        today = dt.date(2026, 11, 7)  # 明天 relative to recorded_at resolves from today
        first = snapshot(self.store, SCOPE, today=today)
        self.assertIn('unverified_due', first['context'])
        self.assertIn('明天', first['context'])
        second = snapshot(self.store, SCOPE, today=today)
        self.assertNotIn('unverified_due', second['context'])
        self.assertIn('和', first['context'])

    def test_far_appointment_not_injected(self):
        import datetime as dt
        self.store.add(event('m1', '周六见小林。'))
        out = snapshot(self.store, SCOPE, today=dt.date(2026, 11, 2))  # 周六=11-07, >1 天
        self.assertNotIn('unverified_due', out['context'])
        near = snapshot(self.store, SCOPE, today=dt.date(2026, 11, 6))
        self.assertIn('unverified_due', near['context'])


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_stats_reports_families_and_layers(self):
        self.store.add_many([
            event('m1', '请叫我小渔。'),
            event('m2', '我下周要考试。'),
            event('m3', '周六见小林。'),
        ])
        from xuyi.stats import stats
        from xuyi import index as xuyi_index
        from xuyi.retrieval import Retriever
        xuyi_index.rebuild(self.store, SCOPE)
        Retriever(self.store).recall(SCOPE, '周六', 900)
        out = stats(self.store, SCOPE)
        self.assertEqual(out['事件数'], 3)
        self.assertIn('小渔', str(out['家族候选']))
        self.assertIn('考试', out['开放环'])
        self.assertTrue(out['索引']['fresh'])
        self.assertEqual(out['交互日志'], 1)
        self.assertEqual(out['最早'], '2026-11-07T12:00')

    def test_stats_empty_scope(self):
        from xuyi.stats import stats
        out = stats(self.store, SCOPE)
        self.assertEqual((out['事件数'], out['家族候选'], out['开放环']), (0, {}, []))


class SensitiveAndIronRuleTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_sensitive_loops_stay_out_of_proactive_surfaces(self):
        self.store.add(event('m1', '我打算去医院复查。'))
        self.store.add(event('m2', '我打算买switch。'))
        out = snapshot(self.store, SCOPE)
        self.assertNotIn('复查', out['context'])
        self.assertIn('买switch', out['context'])

    def test_diet_and_allergies_stay_out_of_the_snapshot(self):
        # Health-adjacent: recalled on eating turns only, never proactively.
        self.store.add(event('m1', '我对芒果过敏。'))
        self.store.add(event('m2', '我打算买switch。'))
        out = snapshot(self.store, SCOPE)
        self.assertNotIn('芒果', out['context'])
        self.assertIn('买switch', out['context'])

    def test_recall_never_reinforces_anything(self):
        # fish-memory's iron rule, as a test: recalling may only append the
        # interaction log; events, states and scores stay byte-identical.
        from xuyi import index as xuyi_index
        from xuyi.retrieval import Retriever
        self.store.add(event('m1', '我下周要考试。'))
        xuyi_index.rebuild(self.store, SCOPE)
        def snapshot_state():
            return {
                'events': self.store.db.execute("SELECT group_concat(payload) FROM events").fetchone()[0],
                'stats': tuple(self.store.db.execute("SELECT chunks,tokens FROM chunk_stats").fetchone()),
                'postings': self.store.db.execute(
                    "SELECT group_concat(tf) FROM chunk_postings").fetchone()[0],
                'supersedes': self.store.db.execute(
                    "SELECT group_concat(supersedes_id) FROM supersedes_map").fetchone()[0],
            }
        before = snapshot_state()
        retriever = Retriever(self.store)
        retriever.recall(SCOPE, '考试', 900)
        retriever.recall(SCOPE, '考试', 900)
        retriever.recall(SCOPE, '最近有什么打算？', 900)
        after = snapshot_state()
        self.assertEqual(before, after)


class ResumeThreadTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def session_event(self, session, text, message=9):
        return dict(scope=SCOPE, event_id=f'dsh:s{session}:{message}', sender_id='a' * 64,
                    role='user', origin='quoted',
                    recorded_at='2026-11-07T12:00:00+08:00',
                    text=text)

    def test_snapshot_resumes_the_previous_session_thread(self):
        # Session one ends mid-topic; automatic rotation starts session two.
        self.store.add_many([
            self.session_event(1, '我最近在看一部纪录片，讲敦煌的。', message=1),
            self.session_event(2, '在吗', message=2),
        ])
        out = snapshot(self.store, SCOPE)
        self.assertIn('unverified_resume', out['context'])
        self.assertIn('敦煌', out['context'])

    def test_resume_uses_the_session_the_host_says_is_current(self):
        # 真机：宿主先准备请求、之后才发出用户消息事件，所以快照计算时新会话的第一条还没入档，
        # 档案最后一条仍属上一个会话。靠档案推断"当前会话"会把上一个会话当成当前，
        # 转而去取更早的会话（这里是个单问句），丢掉刚刚聊的话题。适配器知道当前会话，应直接告知。
        self.store.add_many([
            self.session_event(1, '我家猫叫啥来着？', message=1),
            self.session_event(2, '你咋不按常理出牌，我们刚刚才说过话', message=2),
        ])
        guessed = snapshot(self.store, SCOPE)
        self.assertNotIn('unverified_resume', guessed['context'])   # 旧的推断方式：丢了话题
        told = snapshot(self.store, SCOPE, current_session='s3')
        self.assertIn('unverified_resume', told['context'])
        self.assertIn('刚刚才说过话', told['context'])
        # 新会话的消息已入档时，告知与推断结果一致
        self.store.add(self.session_event(3, '哈喽', message=3))
        self.assertEqual(snapshot(self.store, SCOPE)['context'],
                         snapshot(self.store, SCOPE, current_session='s3')['context'])

    def test_no_resume_when_thread_is_bare_question(self):
        self.store.add(self.session_event(1, '在吗', message=1))
        self.assertEqual(snapshot(self.store, SCOPE)['sources'], [])
        # The previous session's last user message is a bare question — no
        # thread to pick up, so the snapshot stays silent about resuming.
        self.store.add(self.session_event(1, '最近怎么样', message=2))
        self.store.add(self.session_event(2, '在吗', message=3))
        self.assertNotIn('unverified_resume', snapshot(self.store, SCOPE)['context'])


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_weekly_storyline_aggregates_shared_time(self):
        self.store.add_many([
            event('m1', '我下周要考试。'),
            event('m2', '今天和小林吃饭聊起考试的事。'),
        ])
        out = consolidate(self.store, SCOPE)
        self.assertTrue(out['narrative'])
        week = out['narrative'][-1]
        self.assertEqual(week['消息'], 2)
        self.assertIn('想法', week)
        self.assertTrue(week['高频话题'])
        self.assertLessEqual(len(week['高频话题']), 5)


class PersonaApplyTests(unittest.TestCase):
    def test_apply_persona_roundtrips_with_backup(self):
        import tempfile
        from pathlib import Path as P
        from xuyi.personas import apply_persona, load_persona
        with tempfile.TemporaryDirectory() as tmp:
            profile = P(tmp)
            (profile / "cordis.patch.yml").write_text(
                "- id: im-qqbot\n  config:\n    directPrompt: |-%n      旧人设\n    appId: \"123\"\n".replace("%n", "\n"), encoding="utf-8")
            out = apply_persona("wenrou", profile)
            import yaml
            entries = yaml.safe_load((profile / "cordis.patch.yml").read_text(encoding="utf-8"))
            target = [e for e in entries if e.get("id") == "im-qqbot"][0]
            from xuyi.personas import CHAT_STYLE, compose_prompt
            self.assertEqual(target["config"]["directPrompt"],
                             compose_prompt(load_persona("wenrou")["systemPrompt"]))
            self.assertTrue(target["config"]["directPrompt"].endswith(CHAT_STYLE))
            # 气泡：关流式、每行一条
            self.assertIs(target["config"]["streaming"], False)
            self.assertEqual(target["config"]["textChunkLimit"], 1)
            self.assertEqual(target["config"]["appId"], "123")  # 其余字段不动
            self.assertTrue(P(out["backup"]).is_file())
            self.assertIn("旧人设", P(out["backup"]).read_text(encoding="utf-8"))

    def test_users_own_persona_gets_the_same_chat_style(self):
        import tempfile, yaml
        from pathlib import Path as P
        from xuyi.personas import CHAT_STYLE, apply_persona
        with tempfile.TemporaryDirectory() as tmp:
            profile = P(tmp)
            (profile / "cordis.patch.yml").write_text(
                "- id: im-qqbot%n  config:%n    appId: '123'%n    streaming: true%n".replace("%n", chr(10)),
                encoding="utf-8")
            out = apply_persona(None, profile, text="你叫阿桃，是他的大学室友。", bubbles=False)
            target = yaml.safe_load((profile / "cordis.patch.yml").read_text(encoding="utf-8"))[0]
            self.assertTrue(target["config"]["directPrompt"].startswith("你叫阿桃"))
            self.assertTrue(target["config"]["directPrompt"].endswith(CHAT_STYLE))
            self.assertIs(target["config"]["streaming"], True)  # --no-bubbles 不改发送方式
            self.assertEqual(out["applied"], "自定义人设")
            with self.assertRaises(Exception):
                apply_persona(None, profile, text="   ")

    def test_chat_style_is_short_lines_and_no_markup(self):
        from xuyi.personas import BUBBLE_CONFIG, CHAT_STYLE
        for banned in ("续忆", "档案", "检索", "我理解你的感受", "值得注意的是", "首先"):
            self.assertNotIn(banned, CHAT_STYLE)
        self.assertIn("一到三条", CHAT_STYLE)   # QQ 同一条消息最多被动回复 4 条
        self.assertIn("不加粗", CHAT_STYLE)
        self.assertEqual(BUBBLE_CONFIG, {"streaming": False, "textChunkLimit": 1})

    def test_apply_refuses_when_qqbot_absent(self):
        import tempfile
        from pathlib import Path as P
        from xuyi.personas import apply_persona
        with tempfile.TemporaryDirectory() as tmp:
            profile = P(tmp)
            (profile / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
            with self.assertRaises(Exception):
                apply_persona("wenrou", profile)


class SuspectedPairsTests(unittest.TestCase):
    def setUp(self):
        self.store = self.enterContext(Store(':memory:'))

    def test_unmarked_competing_values_surface_for_review(self):
        self.store.add_many([
            event('m1', '我最喜欢的饮料是茶。'),
            event('m2', '我最喜欢的饮料是咖啡。'),
        ])
        out = consolidate(self.store, SCOPE)
        pairs = out['疑似更正待确认']
        self.assertEqual(len(pairs), 1)
        self.assertEqual((pairs[0]['旧值'], pairs[0]['新值']), ('茶', '咖啡'))
        # 带明确更正标记的同一场景应消解，不再待确认
        self.store.add(event('m3', '更正一下，我最喜欢的饮料是气泡水。'))
        out = consolidate(self.store, SCOPE)
        self.assertEqual(out['疑似更正待确认'], [])


class ChatStyleVarietyTests(unittest.TestCase):
    def test_message_count_follows_the_moment_not_a_fixed_number(self):
        from xuyi.personas import CHAT_STYLE
        # 真机：规矩只写"一到三条"，模型每次都发两条。现在要求按情形变化，并给出一/二/三条的示范
        self.assertIn("大多数时候一条就够", CHAT_STYLE)
        self.assertIn("别连着几次都发同样的条数", CHAT_STYLE)
        for count in ("（一条）", "（两条）", "（三条）"):
            self.assertIn(count, CHAT_STYLE)
        self.assertIn("一到三条", CHAT_STYLE)


class ResumeExchangeTests(unittest.TestCase):
    def test_resume_carries_the_last_lines_of_both_sides_and_how_long_ago(self):
        from datetime import datetime, timedelta, timezone
        with Store(':memory:') as store:
            now = datetime.now(timezone.utc)
            rows = [('s1', 'user', '今天降温了', 12), ('s1', 'assistant', '降温了多加件衣服，别光穿个单衣', 12),
                    ('s1', 'user', '你那里冷不冷', 11), ('s1', 'assistant', '我这边也冷，手都冻僵了', 11),
                    ('s1', 'user', '又在瞎编了', 10), ('s1', 'assistant', '哼，才没瞎编，手是真的凉', 10)]
            for n, (session, role, text, minutes_ago) in enumerate(rows):
                store.add(dict(scope=SCOPE, event_id=f'dsh:{session}:{n}', sender_id='a' * 64 if role == 'user' else 'assistant',
                               role=role, origin='quoted' if role == 'user' else 'assistant',
                               recorded_at=(now - timedelta(minutes=minutes_ago)).isoformat(), text=text))
            out = snapshot(store, SCOPE, current_session='s2')
        self.assertIn('unverified_resume', out['context'])
        record = next(line for line in out['context'].split('\n') if 'unverified_resume' in line)
        self.assertIn('我：我这边也冷', record)        # 她自己说过的话也在，接着聊才不会自相矛盾
        self.assertIn('他：又在瞎编了', record)
        self.assertIn('10分钟前', record)               # 真实时钟下分钟级

    def test_the_guide_tells_her_not_to_say_long_time_no_see_after_a_minutes_old_chat(self):
        from xuyi.consolidate import FOLLOWUP_GUIDE
        self.assertIn('不要说好久不见', FOLLOWUP_GUIDE)
        self.assertIn('多久前', FOLLOWUP_GUIDE)
