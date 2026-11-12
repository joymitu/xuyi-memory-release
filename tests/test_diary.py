import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xuyi.board_bridge import (DIARY_WRITER, paths, prepare_yesterday, read, request_diary,
                               write)
from xuyi.diary import day_label, day_messages, diary
from xuyi.store import Store

SCOPE = 'qq:c2c:' + 'd' * 64
PEER = 'd' * 64


class DiaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'diary.sqlite')
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, day, hour, minute, role, text, seq):
        when = datetime(2026, 11, day, hour, minute, tzinfo=datetime.now().astimezone().tzinfo)
        self.store.add({'scope': SCOPE, 'event_id': f'dsh:s:{seq}',
                        'sender_id': PEER if role == 'user' else 'assistant', 'role': role,
                        'origin': 'quoted' if role == 'user' else 'assistant',
                        'recorded_at': when.astimezone(timezone.utc).isoformat(), 'text': text})

    def build(self):
        self.add(8, 10, 0, 'user', '第一天的话', 1)
        self.add(8, 10, 1, 'assistant', '她的回复', 2)
        self.add(9, 23, 50, 'user', '睡前那句', 3)
        self.add(10, 0, 10, 'user', '过了零点', 4)
        return diary(self.store, SCOPE)

    def test_pages_follow_the_local_calendar_newest_first(self):
        book = self.build()
        self.assertEqual([day['date'] for day in book['days']], ['2026-11-10', '2026-11-09', '2026-11-08'])
        self.assertEqual(book['days'][0]['entries'][0]['text'], '过了零点')
        self.assertEqual(book['days'][1]['entries'][0]['text'], '睡前那句')   # 23:50 还算 9 号
        self.assertEqual(book['days'][2]['count'], 2)                        # 两句都算进当天
        self.assertEqual([entry['text'] for entry in book['days'][2]['entries']], ['第一天的话'])

    def test_diary_only_keeps_what_he_said(self):
        book = self.build()
        texts = [entry['text'] for day in book['days'] for entry in day['entries']]
        self.assertNotIn('她的回复', texts)

    def test_notes_land_on_the_day_they_belong_to(self):
        self.build()
        book = diary(self.store, SCOPE, notes=[
            {'kind': 'recall', 'text': '刚想起', 'at': '2026-11-09 15:50'},          # UTC → 本地 9 号 23:50
            {'kind': 'confirm', 'text': '待确认', 'event_id': 'dsh:s:1'},            # 来源在 8 号
            {'kind': 'followup', 'text': '更早的事', 'event_id': 'dsh:s:none'},
        ])
        by_day = {day['date']: [note['text'] for note in day['notes']] for day in book['days']}
        self.assertEqual(by_day['2026-11-09'], ['刚想起'])
        self.assertEqual(by_day['2026-11-08'], ['待确认'])
        self.assertEqual(by_day['2026-11-10'], [])          # 找不到来源的不硬塞

    def test_days_carry_a_short_human_label_for_the_index(self):
        book = self.build()
        self.assertEqual(book['days'][0]['label'], '过了零点')
        self.assertEqual(book['days'][2]['label'], '第一天的话')

    def test_index_label_skips_greetings_and_stays_short(self):
        self.assertEqual(day_label([{'text': '在吗'}, {'text': '嗯嗯'}, {'text': '我家的猫叫年糕'}]),
                         '我家的猫叫年糕')
        self.assertEqual(day_label([{'text': '这一句特别特别特别长的话'}]), '这一句特别特别特别长…')
        self.assertEqual(day_label([]), '')

    def test_a_busy_day_is_sampled_across_the_whole_day(self):
        # 一天 200 句：取样要保住开头和结尾，不能只剩最后一百句。
        for i in range(200):
            self.add(9, 8 + i // 60, i % 60, 'user', f'第{i}句', 100 + i)
        messages = day_messages(self.store, SCOPE, '2026-11-09', limit=10)
        self.assertEqual(len(messages), 10)
        self.assertEqual(messages[0]['text'], '第0句')
        self.assertEqual(messages[-1]['text'], '第199句')

    def test_day_cap_and_empty_scope(self):
        book = self.build()
        self.assertEqual(diary(self.store, SCOPE, days=1)['days'][0]['date'], '2026-11-10')
        empty = diary(self.store, 'qq:c2c:' + 'e' * 64)
        self.assertEqual(empty['days'], [])

    def test_day_messages_give_the_model_both_sides_in_order(self):
        self.build()
        messages = day_messages(self.store, SCOPE, '2026-11-08')
        self.assertEqual([message['role'] for message in messages], ['user', 'assistant'])
        self.assertEqual(messages[0]['time'], '10:00')

    def test_diary_request_is_cached_per_day_and_content(self):
        self.build()
        messages = day_messages(self.store, SCOPE, '2026-11-09')
        persona = '你叫小雅。'
        now = '2026-11-09T16:00:00+00:00'
        text, pending = request_diary(self.db, SCOPE, '2026-11-09', messages, persona, now)
        self.assertIsNone(text)
        self.assertTrue(pending)
        request_file, reply_file = paths(self.db, SCOPE, 'diary-2026-11-09')
        asked = read(request_file)
        self.assertEqual(asked['kind'], 'diary')
        self.assertEqual(asked['date'], '2026-11-09')
        self.assertEqual(asked['messages'], messages)
        self.assertIn('2 到 3 段', asked['prompt'])   # 这天只聊了一句：按条数写短
        self.assertIn('不超过 500 字', asked['prompt'])
        self.assertIn('不要编', asked['prompt'])
        write(reply_file, {'fingerprint': asked['fingerprint'], 'writer': DIARY_WRITER,
                           'text': '第一段。\n\n第二段。', 'at': now})
        text, pending = request_diary(self.db, SCOPE, '2026-11-09', messages, persona, now)
        self.assertEqual(text, '第一段。\n\n第二段。')
        self.assertFalse(pending)
        # 写完就定稿：这一天后来又多了几句，也不会重写。
        changed = messages + [{'time': '23:59', 'role': 'user', 'text': '又一句'}]
        text, pending = request_diary(self.db, SCOPE, '2026-11-09', changed, persona, now)
        self.assertEqual(text, '第一段。\n\n第二段。')
        self.assertFalse(pending)

    def test_today_is_not_written_until_the_day_ends(self):
        self.build()
        messages = day_messages(self.store, SCOPE, '2026-11-09')
        text, pending = request_diary(self.db, SCOPE, '2026-11-09', messages, '你叫小雅。',
                                      '2026-11-09T16:00:00+00:00', allow_write=False)
        self.assertIsNone(text)
        self.assertFalse(pending)
        self.assertFalse(paths(self.db, SCOPE, 'diary-2026-11-09')[0].exists())

    def test_the_first_message_of_a_new_day_asks_for_yesterday(self):
        tz = datetime.now().astimezone().tzinfo
        yesterday = datetime.now(tz) - timedelta(days=1)
        when = yesterday.replace(hour=20, minute=0, second=0, microsecond=0)
        self.store.add({'scope': SCOPE, 'event_id': 'dsh:s:y1', 'sender_id': PEER, 'role': 'user',
                        'origin': 'quoted', 'recorded_at': when.astimezone(timezone.utc).isoformat(),
                        'text': '昨天说的话'})
        written = prepare_yesterday(self.store, self.db, {SCOPE}, persona='你叫小雅。',
                                    today=yesterday.date() + timedelta(days=1))
        self.assertEqual(written, 1)
        request_file, reply_file = paths(self.db, SCOPE, 'diary-' + yesterday.date().isoformat())
        self.assertTrue(request_file.exists())
        self.assertEqual(read(request_file)['messages'][0]['text'], '昨天说的话')
        # 写完定稿后，再进新消息也不会重写。
        asked = read(request_file)
        write(reply_file, {'fingerprint': asked['fingerprint'], 'writer': DIARY_WRITER,
                           'text': '昨天的日记。', 'at': '2026-11-09T16:00:00+00:00'})
        self.assertEqual(prepare_yesterday(self.store, self.db, {SCOPE}, persona='你叫小雅。',
                                           today=yesterday.date() + timedelta(days=1)), 0)


if __name__ == '__main__':
    unittest.main()


class DiaryLengthTests(unittest.TestCase):
    """用户定的长度：200–400 字最好，500 字封顶；按当天聊了多少条来定写多少。"""

    def lines(self, n):
        return [{'time': f'10:{i % 60:02d}', 'role': 'user' if i % 2 == 0 else 'assistant', 'text': f'第{i}句'}
                for i in range(n)]

    def test_length_follows_how_much_was_said_that_day(self):
        from xuyi.board_bridge import DIARY_MAX_CHARS, diary_length
        self.assertEqual(DIARY_MAX_CHARS, 500)
        few, some, many = diary_length(6), diary_length(30), diary_length(120)
        self.assertEqual((few['min'], few['max']), (150, 250))
        self.assertEqual((some['min'], some['max']), (200, 350))
        self.assertEqual((many['min'], many['max']), (300, 450))
        for tier in (few, some, many):
            self.assertLessEqual(tier['max'], DIARY_MAX_CHARS)
        self.assertLess(few['paragraphs'][1], many['paragraphs'][1])

    def test_prompt_states_the_range_and_the_hard_cap_and_no_longer_asks_for_600(self):
        from xuyi.board_bridge import diary_prompt
        light = diary_prompt('2026-11-09', '周一', self.lines(6))
        busy = diary_prompt('2026-11-09', '周一', self.lines(80))
        self.assertIn('150 到 250 字', light)
        self.assertIn('300 到 450 字', busy)
        for prompt in (light, busy):
            self.assertIn('不超过 500 字', prompt)
            self.assertNotIn('600', prompt)

    def test_request_carries_the_cap_for_the_adapter_and_old_diaries_get_rewritten(self):
        from xuyi.board_bridge import DIARY_WRITER
        self.assertEqual(DIARY_WRITER, 2)   # 写法升级：已定稿的旧日记按新长度重写一次
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'm.sqlite')
            request_diary(db, SCOPE, '2026-11-09', self.lines(30), '你是小雅。', '2026-11-10T00:00:00+00:00')
            asked = read(paths(db, SCOPE, 'diary-2026-11-09')[0])
            self.assertEqual(asked['max_chars'], 500)
            self.assertEqual(asked['writer'], 2)


class DiaryScheduleTests(unittest.TestCase):
    """用户定：当天只贴便利贴；晚上十点把当天写成日记并定稿，之后永不重写（规则升级也不重写）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'diary.sqlite')
        self.store = Store(self.db)
        self.tz = datetime.now().astimezone().tzinfo

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def say(self, when, text, seq):
        self.store.add({'scope': SCOPE, 'event_id': f'dsh:s:{seq}', 'sender_id': PEER, 'role': 'user',
                        'origin': 'quoted', 'recorded_at': when.astimezone(timezone.utc).isoformat(), 'text': text})

    def at(self, day, hour, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=self.tz)

    def test_due_from_ten_pm_on_the_same_day(self):
        from xuyi.board_bridge import DIARY_HOUR, diary_due
        self.assertEqual(DIARY_HOUR, 22)
        self.assertFalse(diary_due('2026-11-10', self.at(10, 21, 59)))
        self.assertTrue(diary_due('2026-11-10', self.at(10, 22, 0)))
        self.assertTrue(diary_due('2026-11-09', self.at(10, 9, 0)))    # 前一天：任何时候都该写了
        self.assertFalse(diary_due('2026-11-11', self.at(10, 23, 0)))  # 还没到的日子

    def test_clock_asks_for_today_at_ten_and_for_a_missed_yesterday(self):
        from xuyi.board_bridge import prepare_due
        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        today_date = now.date().isoformat()
        yesterday_date = (now - timedelta(days=1)).date().isoformat()
        today_0900 = now.replace(hour=9, minute=0, second=0, microsecond=0)
        today_2201 = now.replace(hour=22, minute=1, second=0, microsecond=0)
        self.say(now.replace(hour=20, minute=0).replace(day=now.day) - timedelta(days=1), '昨天说的话', 1)
        self.say(now.replace(hour=15, minute=0, second=0, microsecond=0), '今天下午说的话', 2)
        persona = '你叫小雅。'
        # 早上：只补前一天，不写今天。
        self.assertEqual(prepare_due(self.store, self.db, {SCOPE}, persona=persona, now=today_0900), 1)
        self.assertTrue(paths(self.db, SCOPE, f'diary-{yesterday_date}')[0].exists())
        self.assertFalse(paths(self.db, SCOPE, f'diary-{today_date}')[0].exists())
        # 十点以后：今天也写。
        self.assertGreaterEqual(prepare_due(self.store, self.db, {SCOPE}, persona=persona, now=today_2201), 1)
        asked = read(paths(self.db, SCOPE, f'diary-{today_date}')[0])
        self.assertEqual([m['text'] for m in asked['messages']], ['今天下午说的话'])

    def test_finalized_never_rewritten_even_after_a_rule_change_and_late_messages(self):
        from xuyi.board_bridge import cached_diary, prepare_due
        self.say(self.at(10, 15), '今天下午说的话', 1)
        persona = '你叫小雅。'
        prepare_due(self.store, self.db, {SCOPE}, persona=persona, now=self.at(10, 22, 1))
        request_file, reply_file = paths(self.db, SCOPE, 'diary-2026-11-10')
        asked = read(request_file)
        # 旧版本写法写成的定稿（模拟以后规则升级）：照样算定稿。
        write(reply_file, {'fingerprint': asked['fingerprint'], 'writer': 1, 'text': '十点写好的日记。',
                           'at': '2026-11-10T14:01:00+00:00'})
        request_file.unlink()
        self.say(self.at(10, 23, 30), '十点以后又聊的', 2)
        self.assertEqual(prepare_due(self.store, self.db, {SCOPE}, persona=persona, now=self.at(10, 23, 40)), 0)
        self.assertEqual(prepare_due(self.store, self.db, {SCOPE}, persona=persona, now=self.at(11, 9)), 0)
        self.assertFalse(request_file.exists())
        messages = day_messages(self.store, SCOPE, '2026-11-10')
        text, pending = request_diary(self.db, SCOPE, '2026-11-10', messages, persona, 'now')
        self.assertEqual((text, pending), ('十点写好的日记。', False))
        # 导出也照用定稿（内容变了也不丢）。
        self.assertEqual(cached_diary(self.db, SCOPE, '2026-11-10', messages, persona), '十点写好的日记。')

    def test_cli_diary_due_runs_without_a_board(self):
        import json, os, subprocess, sys
        from datetime import datetime, timedelta
        self.say((datetime.now().astimezone() - timedelta(days=1)).replace(
            hour=20, minute=0, second=0, microsecond=0), '昨天说的话', 1)
        self.store.close()
        profile = Path(self.tmp.name) / 'profile'
        profile.mkdir()
        (profile / 'cordis.patch.yml').write_text(
            "- id: im-qqbot\n  config:\n    directPrompt: 你叫小雅。\n", encoding='utf-8')
        env = {**os.environ, 'PYTHONUTF8': '1', 'XUYI_PROFILE_DIR': str(profile)}
        out = subprocess.run([sys.executable, '-m', 'xuyi', '--db', self.db, 'diary-due'],
                             capture_output=True, env=env, timeout=60, cwd=Path(__file__).resolve().parents[1])
        self.store = Store(self.db)
        self.assertEqual(out.returncode, 0, out.stderr.decode('utf-8', 'replace'))
        self.assertGreaterEqual(json.loads(out.stdout.decode('utf-8'))['requested'], 1)

    def test_user_can_pick_the_hour_and_bad_values_are_refused(self):
        from xuyi.board_bridge import diary_hour, prepare_due, set_diary_hour
        self.assertEqual(diary_hour(self.db), 22)
        set_diary_hour(self.db, 20)
        self.assertEqual(diary_hour(self.db), 20)
        for bad in (17, 24, 0, '21', True):
            with self.assertRaises(ValueError):
                set_diary_hour(self.db, bad)
        self.assertEqual(diary_hour(self.db), 20)
        # 选了八点：八点过后今天就写。
        self.say(self.at(10, 15), '今天下午说的话', 1)
        self.assertGreaterEqual(prepare_due(self.store, self.db, {SCOPE}, persona='你叫小雅。', now=self.at(10, 20, 5)), 1)
        self.assertTrue(paths(self.db, SCOPE, 'diary-2026-11-10')[0].exists())
