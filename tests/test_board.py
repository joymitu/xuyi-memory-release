import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from xuyi.board import board, contacts
from xuyi.board_bridge import (NOTE_WRITER, find_persona, fingerprint, paths, read, request_notes,
                               write)
from xuyi.store import Store

ALICE = 'qq:c2c:' + 'a' * 64
BOB = 'qq:c2c:' + 'b' * 64


class BoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / 'demo.sqlite')
        subprocess.run([sys.executable, str(ROOT / 'scripts/board_demo.py'), cls.db], check=True,
                       capture_output=True, cwd=ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def data(self, scope=ALICE):
        with Store(self.db) as store:
            return board(store, scope)

    def items(self, data):
        return {(b['name'], i['label']): i for b in data['branches'] for i in b['items']}

    def test_map_shows_what_she_would_recall_with_the_words_it_came_from(self):
        data = self.data()
        items = self.items(data)
        self.assertEqual(data['center']['name'], '小鱼')
        cat = items[('宠物', '猫的名字')]
        self.assertEqual(cat['value'], '汤圆')
        self.assertIn('叫汤圆', cat['quote'])
        self.assertIn('年糕', cat['history'][0]['quote'])
        self.assertEqual(items[('吃喝', '饮品')]['value'], '美式')
        self.assertEqual(items[('吃喝', '忌口过敏')]['value'], '对芒果过敏')
        self.assertEqual(items[('朋友', '小林')]['value'], '周六，已取消')

    def test_conflicts_become_a_question_not_a_fact(self):
        data = self.data()
        self.assertNotIn(('吃喝', '食物'), self.items(data))
        self.assertTrue(any(n['kind'] == 'confirm' and '火锅' in n['text'] and '寿司' in n['text']
                            for n in data['notes']))

    def test_sticky_notes_speak_in_her_voice_about_real_recalls(self):
        notes = self.data()['notes']
        recalls = [n for n in notes if n['kind'] == 'recall']
        self.assertTrue(any('汤圆' in n['text'] for n in recalls))
        self.assertTrue(all(n['text'].startswith('你问我「') for n in recalls))
        self.assertTrue(any(n['kind'] == 'followup' and '书架' in n['text'] for n in notes))

    def test_each_friend_has_a_separate_board(self):
        with Store(self.db) as store:
            people = contacts(store)
        self.assertEqual([p['label'] for p in people], ['小鱼', '阿杰'])
        bob = json.dumps(self.data(BOB), ensure_ascii=False)
        for word in ('汤圆', '芒果', '小林', '美式'):
            self.assertNotIn(word, bob)


class BoardServerTests(unittest.TestCase):
    def test_token_is_required_and_the_page_and_api_work(self):
        from xuyi.board_server import serve
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'demo.sqlite')
            subprocess.run([sys.executable, str(ROOT / 'scripts/board_demo.py'), db], check=True,
                           capture_output=True, cwd=ROOT)
            box = {}
            ready = threading.Event()
            def started(server, url):
                box.update(server=server, url=url)
                ready.set()
            thread = threading.Thread(target=serve, args=(db, 0, False, started),
                                      kwargs={'profile_root': str(Path(tmp) / 'no-profile')}, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(10))
            base, token = box['url'].split('/?t=')
            self.assertTrue(base.startswith('http://127.0.0.1:'))
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + '/api/contacts', timeout=10)
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + '/api/contacts?t=wrong', timeout=10)
            page = urllib.request.urlopen(box['url'], timeout=10).read().decode('utf-8')
            self.assertIn('续忆', page)
            self.assertIn('TA的记忆', page)
            self.assertIn('font/ZCOOLKuaiLe.ttf', page)
            # 内置手写字体：页面不带口令也能取（固定资源，不含聊天内容），浏览器会长期缓存。
            font = urllib.request.urlopen(f'{base}/font/ZCOOLKuaiLe.ttf', timeout=10)
            self.assertEqual(font.headers.get_content_type(), 'font/ttf')
            self.assertGreater(len(font.read()), 100_000)
            people = json.loads(urllib.request.urlopen(f'{base}/api/contacts?t={token}', timeout=10).read())
            data = json.loads(urllib.request.urlopen(
                f"{base}/api/board?scope={people[0]['scope']}&t={token}", timeout=10).read())
            self.assertEqual(data['center']['name'], '小鱼')
            self.assertEqual(data['notes_source'], 'template')   # 没配置人设：不请模型写
            # 便利贴上的「一直记着 / 别再提了」：只收带口令的 JSON POST。
            target = next((item for item in data['note_facts'] if item.get('event_id')), None)
            if target:
                body = json.dumps({'scope': people[0]['scope'], 'event_id': target['event_id'],
                                   'pinned': True}).encode()
                request = urllib.request.Request(
                    f"{base}/api/override?t={token}", data=body, method='POST',
                    headers={'Content-Type': 'application/json'})
                answer = json.loads(urllib.request.urlopen(request, timeout=10).read())
                self.assertTrue(answer['pinned'])
                refreshed = json.loads(urllib.request.urlopen(
                    f"{base}/api/board?scope={people[0]['scope']}&t={token}", timeout=10).read())
                pinned = next(item for item in refreshed['note_facts']
                              if item.get('event_id') == target['event_id'])
                self.assertEqual(pinned['strength'], 1.0)
                self.assertTrue(pinned['pinned'])
                with self.assertRaises(urllib.error.HTTPError) as denied_write:
                    urllib.request.urlopen(urllib.request.Request(
                        f"{base}/api/override", data=body, method='POST',
                        headers={'Content-Type': 'application/json'}), timeout=10)
                self.assertEqual(denied_write.exception.code, 403)
            book = json.loads(urllib.request.urlopen(
                f"{base}/api/diary?scope={people[0]['scope']}&t={token}", timeout=10).read())
            self.assertTrue(book['days'])
            self.assertEqual(book['days'][0]['date'], max(day['date'] for day in book['days']))
            self.assertTrue(all('entries' in day and 'notes' in day for day in book['days']))
            md = urllib.request.urlopen(
                f"{base}/export?scope={people[0]['scope']}&format=md&t={token}", timeout=10)
            self.assertEqual(md.headers.get_content_type(), 'text/markdown')
            self.assertIn('attachment', md.headers.get('Content-Disposition', ''))
            self.assertIn('filename="xuyi-diary.md"', md.headers.get('Content-Disposition', ''))
            self.assertIn('TA的日记本', md.read().decode('utf-8'))
            docx = urllib.request.urlopen(
                f"{base}/export?scope={people[0]['scope']}&format=docx&t={token}", timeout=10)
            self.assertTrue(docx.read().startswith(b'PK'))
            box['server'].shutdown()


class BoardNoteFactsTests(unittest.TestCase):
    """便利贴要同时给出"中性事实"（给宿主模型改写）和模板文案（零模型调用时展示）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / 'demo.sqlite')
        subprocess.run([sys.executable, str(ROOT / 'scripts/board_demo.py'), cls.db], check=True,
                       capture_output=True, cwd=ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def data(self, scope=ALICE):
        with Store(self.db) as store:
            return board(store, scope)

    def test_every_note_has_a_neutral_fact_in_the_same_order(self):
        data = self.data()
        self.assertEqual([note['kind'] for note in data['notes']],
                         [fact['kind'] for fact in data['note_facts']])
        facts = ' '.join(fact['fact'] for fact in data['note_facts'])
        self.assertIn('汤圆', facts)
        self.assertNotIn('你问我', facts)      # 事实不是她那句话的模板
        self.assertNotIn('想起来啦', facts)

    def test_fingerprint_changes_only_when_facts_or_persona_change(self):
        facts = self.data()['note_facts']
        first = fingerprint(facts, '你叫小雅。')
        self.assertEqual(first, fingerprint(list(facts), '你叫小雅。'))
        self.assertNotEqual(first, fingerprint(facts, '你叫阿杰。'))
        changed = [dict(fact) for fact in facts]
        changed[-1]['fact'] += '（改）'
        self.assertNotEqual(first, fingerprint(changed, '你叫小雅。'))

    def test_bridge_asks_once_and_reuses_matching_notes(self):
        facts = self.data()['note_facts']
        persona = '你叫小雅，说话短，爱损他。'
        notes, pending = request_notes(self.db, ALICE, facts, persona, '2026-11-09T09:00:00+00:00')
        self.assertIsNone(notes)
        self.assertTrue(pending)
        request_file, reply_file = paths(self.db, ALICE)
        asked = read(request_file)
        self.assertEqual(asked['fingerprint'], fingerprint(facts, persona))
        self.assertEqual(asked['facts'], facts)
        self.assertEqual(asked['persona'], persona)
        # 提示词随请求下发：改语气只改 Python，不用重新打包适配器。
        self.assertIn('15 到 35 个字', asked['prompt'])
        self.assertIn('一张一行', asked['prompt'])

        write(reply_file, {'fingerprint': asked['fingerprint'], 'writer': NOTE_WRITER,
                           'at': '2026-11-09T09:00:05+00:00',
                           'notes': [{'kind': fact['kind'], 'text': f'纸条{i}'}
                                     for i, fact in enumerate(facts)]})
        notes, pending = request_notes(self.db, ALICE, facts, persona, '2026-11-09T09:00:20+00:00')
        self.assertFalse(pending)
        self.assertEqual([note['text'] for note in notes], [f'纸条{i}' for i in range(len(facts))])

    def test_notes_written_by_an_older_version_are_rewritten(self):
        # 旧版适配器写的纸条没有 writer（或小于当前版本），要当过期处理，不能一直显示旧的短句。
        facts = self.data()['note_facts']
        persona = '你叫小雅。'
        now = '2026-11-09T09:00:00+00:00'
        _, reply_file = paths(self.db, ALICE)
        reply_file.unlink(missing_ok=True)
        self.assertTrue(request_notes(self.db, ALICE, facts, persona, now)[1])
        asked = read(paths(self.db, ALICE)[0])
        _, reply_file = paths(self.db, ALICE)
        write(reply_file, {'fingerprint': asked['fingerprint'], 'at': now,
                           'notes': [{'kind': fact['kind'], 'text': '旧'} for fact in facts]})
        notes, pending = request_notes(self.db, ALICE, facts, persona, '2026-11-09T09:10:00+00:00')
        self.assertIsNone(notes)
        self.assertTrue(pending)

    def test_notes_written_for_other_content_are_ignored(self):
        facts = self.data()['note_facts']
        persona = '你叫小雅。'
        _, reply_file = paths(self.db, ALICE)
        write(reply_file, {'fingerprint': 'old-content', 'at': '2026-11-09T09:00:05+00:00',
                           'notes': [{'kind': 'recall', 'text': '旧纸条'}]})
        notes, pending = request_notes(self.db, ALICE, facts, persona, '2026-11-09T09:10:00+00:00')
        self.assertIsNone(notes)
        self.assertTrue(pending)

    def test_bridge_writes_are_atomic_and_leave_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'x.json'
            write(target, {'a': 1})
            write(target, {'a': 2})
            self.assertEqual(read(target), {'a': 2})
            self.assertEqual([path.name for path in Path(tmp).iterdir()], ['x.json'])

    def test_model_notes_keep_the_event_they_came_from(self):
        # 适配器只写「类型 + 文字」；来源在 Python 侧按顺序补回去，日记本才贴得对页。
        facts = self.data()['note_facts']
        self.assertTrue(any('event_id' in fact for fact in facts))
        persona = '你叫小雅。'
        now = '2026-11-09T09:00:00+00:00'
        self.assertTrue(request_notes(self.db, ALICE, facts, persona, now)[1])
        asked = read(paths(self.db, ALICE)[0])
        _, reply_file = paths(self.db, ALICE)
        write(reply_file, {'fingerprint': asked['fingerprint'], 'writer': NOTE_WRITER, 'at': now,
                           'notes': [{'kind': fact['kind'], 'text': f'纸条{i}'}
                                     for i, fact in enumerate(facts)]})
        notes, pending = request_notes(self.db, ALICE, facts, persona, now)
        self.assertFalse(pending)
        self.assertEqual([note.get('event_id') for note in notes],
                         [fact.get('event_id') for fact in facts])

    def test_no_facts_or_no_persona_never_asks_the_host(self):
        with Store(self.db) as store:
            facts = board(store, BOB)['note_facts']
        self.assertEqual(request_notes(self.db, BOB, [], '你叫小雅。', '2026-11-09T09:00:00+00:00'),
                         (None, False))
        self.assertEqual(request_notes(self.db, BOB, facts, None, '2026-11-09T09:00:00+00:00'),
                         (None, False))
        self.assertFalse(paths(self.db, BOB)[0].exists())


class PersonaTests(unittest.TestCase):
    def test_persona_comes_from_the_hosts_own_qq_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / 'desktop'
            profile.mkdir()
            (profile / 'cordis.patch.yml').write_text(
                '- id: im-qqbot\n  config:\n    appId: x\n    directPrompt: |-\n      你叫阿甲，说话冲。\n',
                encoding='utf-8')
            found = find_persona(profile)
        self.assertEqual(found['text'], '你叫阿甲，说话冲。')
        self.assertTrue(found['source'].endswith('cordis.patch.yml'))

    def test_missing_persona_means_no_model_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_persona(Path(tmp) / 'nope'))

    def test_board_server_swaps_templates_for_model_notes_once_written(self):
        from xuyi.board_server import serve
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'demo.sqlite')
            subprocess.run([sys.executable, str(ROOT / 'scripts/board_demo.py'), db], check=True,
                           capture_output=True, cwd=ROOT)
            profile = Path(tmp) / 'desktop'
            profile.mkdir()
            (profile / 'cordis.patch.yml').write_text(
                '- id: im-qqbot\n  config:\n    directPrompt: 你叫小雅，说话短。\n', encoding='utf-8')
            box, ready = {}, threading.Event()
            thread = threading.Thread(target=serve, args=(db, 0, False, lambda s, u: (box.update(server=s, url=u), ready.set())),
                                      kwargs={'profile_root': str(profile)}, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(10))
            base, token = box['url'].split('/?t=')
            scope = 'qq:c2c:' + 'a' * 64
            first = json.loads(urllib.request.urlopen(f'{base}/api/board?scope={scope}&t={token}', timeout=10).read())
            self.assertEqual(first['notes_source'], 'template')
            self.assertTrue(first['notes_pending'])
            request_file, reply_file = paths(db, scope)
            asked = read(request_file)
            self.assertEqual(asked['persona'], '你叫小雅，说话短。')
            self.assertEqual(len(asked['facts']), len(first['note_facts']))
            write(reply_file, {'fingerprint': asked['fingerprint'], 'writer': NOTE_WRITER,
                               'at': '2026-11-09T09:00:05+00:00',
                               'notes': [{'kind': fact['kind'], 'text': f'模型纸条{i}'}
                                         for i, fact in enumerate(asked['facts'])]})
            second = json.loads(urllib.request.urlopen(f'{base}/api/board?scope={scope}&t={token}', timeout=10).read())
            self.assertEqual(second['notes_source'], 'model')
            self.assertFalse(second['notes_pending'])
            self.assertEqual([note['text'] for note in second['notes']],
                             [f'模型纸条{i}' for i in range(len(asked['facts']))])
            box['server'].shutdown()


if __name__ == '__main__':
    unittest.main()


class ToolQueryNotesTests(unittest.TestCase):
    def test_model_tool_queries_never_become_sticky_notes(self):
        from datetime import datetime, timezone
        with Store(':memory:') as store:
            peer = 'c' * 64
            scope = 'qq:c2c:' + peer
            rows = [('我家的猫叫年糕', 'user'), ('年糕好可爱', 'assistant'), ('更正一下，我家的猫不叫年糕，叫汤圆', 'user'),
                    ('我家猫叫啥来着？', 'user')]
            for n, (text, role) in enumerate(rows):
                store.add({'scope': scope, 'event_id': f'dsh:s:{n}', 'sender_id': peer if role == 'user' else 'assistant',
                           'role': role, 'origin': 'quoted' if role == 'user' else 'assistant',
                           'recorded_at': datetime(2026, 11, 9, 8, n, tzinfo=timezone.utc).isoformat(), 'text': text})
            from xuyi import index
            from xuyi.retrieval import Retriever
            index.rebuild(store, scope)  # 线上写入时就会建索引与回忆记录表
            retriever = Retriever(store)
            retriever._log(scope, '猫 年糕', [store.get(scope, 'dsh:s:0')])  # 模型自己调工具用的查询词
            retriever.recall(scope, '我家猫叫啥来着？', 900, highlight_only=True)  # 自动回忆：查询词是用户原话
            recalls = [n for n in board(store, scope)['notes'] if n['kind'] == 'recall']
        self.assertEqual(len(recalls), 1)
        self.assertIn('我家猫叫啥来着', recalls[0]['text'])
        self.assertIn('汤圆', recalls[0]['text'])
        self.assertNotIn('猫 年糕', recalls[0]['text'])


class BoardInDshTests(unittest.TestCase):
    """日记本放进 DSH：适配器在后台起看板，读一行公告拿端口和口令，DSH 退出时看板跟着退。"""

    def start(self, db, parent_pid):
        proc = subprocess.Popen(
            [sys.executable, '-m', 'xuyi', '--db', db, 'board', '--no-open', '--announce',
             '--parent-pid', str(parent_pid), '--profile', str(Path(db).parent / 'no-profile')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT,
            env={**__import__('os').environ, 'PYTHONUTF8': '1'})
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        line = proc.stdout.readline().decode('utf-8')
        return proc, json.loads(line)

    def test_announce_gives_a_machine_readable_url_and_the_api_works_with_it(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'demo.sqlite')
            subprocess.run([sys.executable, str(ROOT / 'scripts/board_demo.py'), db], check=True,
                           capture_output=True, cwd=ROOT)
            proc, info = self.start(db, os.getpid())
            self.assertTrue(info['url'].startswith('http://127.0.0.1:'))
            self.assertTrue(info['token'])
            people = json.loads(urllib.request.urlopen(
                f"{info['url']}/api/contacts?t={info['token']}", timeout=10).read())
            self.assertTrue(people)
            # 关看板要口令；带口令时看板自己退出（杀 Windows 虚拟环境的启动器杀不掉真正的进程）。
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{info['url']}/api/shutdown", timeout=10)
            urllib.request.urlopen(f"{info['url']}/api/shutdown?t={info['token']}", timeout=10).read()
            self.assertEqual(proc.wait(20), 0)

    def test_board_exits_when_its_parent_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'empty.sqlite')
            parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            proc, _ = self.start(db, parent.pid)
            parent.kill()
            parent.wait(10)
            self.assertIsNotNone(proc.wait(20))

    def test_page_uses_relative_paths_so_it_works_under_a_prefix(self):
        page = (ROOT / 'xuyi/board.html').read_text(encoding='utf-8')
        for absolute in ("'/api/", '"/api/', "'/export", 'url("/font'):
            self.assertNotIn(absolute, page)
        self.assertIn("url(\"font/ZCOOLKuaiLe.ttf\")", page)
        # 嵌在 DSH 里时按 Esc 关闭：通知外层面板。
        self.assertIn("xuyi-diary-close", page)


class SetupPanelPageTests(unittest.TestCase):
    def test_setup_panel_only_talks_to_the_dsh_setup_api_with_its_header(self):
        page = (ROOT / 'xuyi/board.html').read_text(encoding='utf-8')
        self.assertIn("fetch('setup/state')", page)
        self.assertIn("'X-Xuyi-Setup': '1'", page)
        self.assertIn("setupPost('setup/persona'", page)
        self.assertIn("setupPost('setup/install-qq'", page)
        self.assertIn("type: 'xuyi-open-qq'", page)
        # 单独打开的看板（xuyi board）没有 setup 接口：拿不到状态就不显示按钮。
        self.assertIn("if (!setupState) return;", page)

    def test_setup_panel_shows_first_run_prepare_progress(self):
        page = (ROOT / 'xuyi/board.html').read_text(encoding='utf-8')
        self.assertIn('renderPrepare(setupState.prepare)', page)
        self.assertIn('id="prepLine"', page)
        self.assertIn("renderPrepare(setupState.prepare, 'wizPrepLine')", page)
        self.assertIn('id="wizPrepLine"', page)
        self.assertIn('当前使用词法检索', page)

    def test_sticky_notes_stay_beside_the_page_inside_the_dsh_panel(self):
        # 日记本嵌在 DSH 左侧栏右边，宽度常在 900–1180：便利贴要留在左右两边，只有很窄才放到下面。
        page = (ROOT / 'xuyi/board.html').read_text(encoding='utf-8')
        mid = page[page.index('@media (max-width: 1180px)'):page.index('@media (max-width: 760px)')]
        self.assertNotIn('position: static; display: flex', mid)
        self.assertIn('.page-notes .side.left { right: calc(100% + 14px); }', mid)
        narrow = page[page.index('@media (max-width: 760px)'):]
        self.assertIn('.page-notes { position: static', narrow)

    def test_reopening_the_guide_from_settings_walks_every_step(self):
        # 2026-11-12 真机：都设置好了，从「设置」点「打开引导」只剩完成页。手动打开要从第 1 步逐屏走，
        # 做完的显示 ✓、可以上一步 / 下一步；新用户自动弹出的引导仍停在第一个没做完的步骤。
        page = (ROOT / 'xuyi/board.html').read_text(encoding='utf-8')
        self.assertIn("again.querySelector('button').onclick = () => openWizard(true);", page)
        self.assertIn("if (wizardStep() !== 'done' && !flag('xuyi-wizard-later')) openWizard();", page)
        self.assertIn('data-prev', page)
        self.assertIn('data-next', page)
        self.assertIn('✓ 已经装好了。', page)

    def test_setup_panel_lets_the_user_pick_when_the_diary_is_written(self):
        page = (ROOT / 'xuyi/board.html').read_text(encoding='utf-8')
        self.assertIn('id="stepDiary"', page)
        self.assertIn("setupPost('setup/diary-hour'", page)
        self.assertIn('写完不改', page)
        # 今天那页告诉用户几点写，而不是笼统的"等这一天结束"。
        self.assertIn('hourText(hour)}TA会把今天写成日记', page)
