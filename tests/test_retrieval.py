import os
from pathlib import Path
import tempfile
import unittest

from test_store import event
from xuyi.store import Store, MemoryError
from xuyi.retrieval import Retriever, chunks
from xuyi.embeddings import ByteCounter, OnnxEmbedder, TokenizerCounter


class FakeEmbedding:
    fingerprint='structural-test-only'
    def __init__(self):self.document_count=0
    def fit_prefix(self,text,limit):return min(len(text),limit)
    def encode(self,texts,query=False):
        if not query:self.document_count+=len(texts)
        return [[1.0,0.0] if any(w in t for w in ('猫','宠物')) else [0.0,1.0] for t in texts]


class RetrievalTests(unittest.TestCase):
    def setUp(self):self.store=Store(':memory:')
    def tearDown(self):self.store.close()

    def test_byte_budget_includes_all_rendering(self):
        self.store.add_many([event(),event('m2','甲号喜欢晒太阳。')])
        r=Retriever(self.store)
        for budget in (0,50,400,1000):
            out=r.recall('private:U1','甲号',budget)
            self.assertEqual(out['used'],len(out['context'].encode()))
            self.assertLessEqual(out['used'],budget)
            self.assertEqual(out['unit'],'utf8_bytes')

    def test_chunks_reconstruct_exact_original(self):
        e=event(text=('中文🐱\nabcdef'*100))
        pieces=chunks([e])
        self.assertEqual(''.join(p['text'] for p in pieces),e['text'])
        for p in pieces:self.assertEqual(e['text'][p['start']:p['end']],p['text'])

    def test_scope_sender_and_unknown(self):
        self.store.add(event());other=event('m2','我的猫叫饼干。');other['sender_id']='U2';self.store.add(other)
        self.store.add(event('m3','秘密猫名。',scope='group:9'))
        r=Retriever(self.store)
        self.assertEqual({p['sender_id'] for p in r.recall('private:U1','猫',sender_id='U1')['sources']},{'U1'})
        self.assertEqual(r.recall('private:U1','火星飞船不存在词')['sources'],[])
        self.assertEqual(r.recall('private:U1','')['sources'],[])
        self.assertEqual(r.recall('missing','猫')['sources'],[])

    def test_vector_cache_and_deletion(self):
        self.store.add(event());model=FakeEmbedding();r=Retriever(self.store,model)
        out=r.recall('private:U1','宠物')
        self.assertEqual(out['mode'],'hybrid');self.assertEqual(out['sources'][0]['event_id'],'m1')
        r.recall('private:U1','宠物');self.assertEqual(model.document_count,1)
        self.store.forget('private:U1','m1')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM vector_cache').fetchone()[0],0)
        self.assertEqual(r.recall('private:U1','宠物')['sources'],[])

    def test_semantic_only_rescue_is_annotated_for_hedging(self):
        # '甲号' shares no lexical term with the query; only the dense signal
        # rescues it, so the context must carry the uncertainty marker.
        self.store.add_many([event('dsh:m9', '完全无关的开场白，聊聊天气。')])
        r = Retriever(self.store, FakeEmbedding()).recall('private:U1', '宠物', 600)
        self.assertIn('仅语义近似', r['context'])
        self.assertIn('m9', r['context'])
        self.store.add(event('dsh:m10', '我家的猫叫甲号。'))
        matched = Retriever(self.store, FakeEmbedding()).recall('private:U1', '甲号', 600)
        line_of = {line.split(' | ')[0][1:]: line for line in matched['context'].splitlines() if line.startswith('[')}
        self.assertNotIn('仅语义近似', line_of['dsh:m10'])
        self.assertIn('仅语义近似', line_of['dsh:m9'])

    def test_profile_query_returns_structured_memory(self):
        peer = 'a' * 64
        row = event('dsh:m1', '我家的猫叫甲号。', scope='qq:c2c:' + peer)
        row['recorded_at'] = '2025-06-01T12:00:00+08:00'
        row['origin'] = 'quoted'; row['sender_id'] = peer
        self.store.add(row)
        r = Retriever(self.store).recall('qq:c2c:' + peer, '你都记得我什么呀', 1600)
        self.assertEqual(r['mode'], 'profile')
        self.assertIn('unverified_profile', r['context'])
        self.assertIn('2025-06-01', r['context'])
        self.assertIn('甲号', r['context'])
        self.assertLessEqual(r['used'], 900)

    def test_bad_budget_rejected(self):
        for budget in (-1,True,1.5):
            with self.assertRaises(MemoryError):Retriever(self.store).recall('s','q',budget)

    @unittest.skipUnless(os.environ.get('XUYI_MODEL_PATH'),'设置XUYI_MODEL_PATH运行真实中文模型集成检查')
    def test_real_chinese_model(self):
        self.store.add_many([event(),event('m2','周日与小林去公园。'),event('m3','现在更喜欢喝茶。')])
        model=OnnxEmbedder(os.environ['XUYI_MODEL_PATH'])
        out=Retriever(self.store,model).recall('private:U1','宠物叫什么？')
        self.assertEqual(out['sources'][0]['event_id'],'m1')
        counter=TokenizerCounter(Path(os.environ['XUYI_MODEL_PATH'])/'tokenizer.json')
        limited=Retriever(self.store,model,counter).recall('private:U1','宠物叫什么？',300)
        self.assertLessEqual(limited['used'],300)
        self.assertEqual(limited['used'],counter.count(limited['context']))


if __name__=='__main__':unittest.main()


class FocusTests(unittest.TestCase):
    """模型主动查询的聚焦模式：不再用只沾一个字的行把预算塞满。"""
    def setUp(self):
        from xuyi import index as xuyi_index
        self.store = Store(':memory:')
        self.scope = 'qq:c2c:' + 'f' * 64
        rows = [('user', '我家的猫叫年糕，超级粘人。'), ('assistant', '年糕这名字真萌！'),
                ('user', '下班路上看到一只橘猫在晒太阳。'), ('assistant', '橘猫都好胖哈哈'),
                ('user', '昨晚做梦梦到考试没带笔。'), ('assistant', '经典噩梦了'),
                ('user', '我家楼下新开了一家面馆。'), ('assistant', '哈哈哈')]
        rows += [('user', f'今天第{i}次去超市买菜，人好多。') for i in range(40)]
        for n, (role, text) in enumerate(rows):
            e = event(f'dsh:s:{n}', text, scope=self.scope)
            e['role'], e['origin'] = role, ('quoted' if role == 'user' else 'assistant')
            if role == 'assistant': e['sender_id'] = 'assistant'
            self.store.add(e)
        xuyi_index.rebuild(self.store, self.scope)

    def tearDown(self): self.store.close()

    def texts(self, query, **options):
        out = Retriever(self.store).recall(self.scope, query, 1600, **options)
        return [p['text'] for p in out['sources']]

    def test_single_character_matches_no_longer_fill_the_budget(self):
        loose, focused = self.texts('我家猫叫啥来着？'), self.texts('我家猫叫啥来着？', focus=True)
        self.assertIn('我家的猫叫年糕，超级粘人。', focused)
        self.assertNotIn('下班路上看到一只橘猫在晒太阳。', focused)
        self.assertNotIn('哈哈哈', focused)
        self.assertLess(len(focused), len(loose))

    def test_a_kept_reply_brings_the_line_it_answers(self):
        focused = self.texts('我做了什么噩梦？', focus=True)
        self.assertIn('经典噩梦了', focused)
        self.assertIn('昨晚做梦梦到考试没带笔。', focused)
        self.assertLess(focused.index('昨晚做梦梦到考试没带笔。'), focused.index('经典噩梦了'))

    def test_unanchored_questions_keep_the_rankers_head(self):
        self.assertTrue(self.texts('那只粘人的小家伙', focus=True))

    def test_default_recall_is_unchanged(self):
        self.assertIn('下班路上看到一只橘猫在晒太阳。', self.texts('我家猫叫啥来着？'))


class AnswerPairTests(unittest.TestCase):
    """问句被召回时，要把它的回答一起带回来。

    真机：昨天她答过「云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻」，今天问「哪家的好喝」，
    检索只带回了这句问话，没带回回答，模型就顺口编了一个牌子。
    """
    def setUp(self):
        from xuyi import index as xuyi_index
        self.store = Store(':memory:')
        self.scope = 'qq:c2c:' + 'a' * 64
        rows = [('user', '哪家的好喝你说说'), ('assistant', '云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻'),
                ('user', '有啥推荐吗'), ('assistant', '奶茶配个蛋挞吧，别要全糖的'),
                ('user', '我觉得晴天果茶也可以'), ('assistant', '晴天果茶是葡萄冻那种，一杯顶一顿饭')]
        rows += [('user', f'今天第{i}次去超市买菜，人好多。') for i in range(40)]
        for n, (role, text) in enumerate(rows):
            e = event(f'dsh:s:{n}', text, scope=self.scope)
            e['role'], e['origin'] = role, ('quoted' if role == 'user' else 'assistant')
            if role == 'assistant': e['sender_id'] = 'assistant'
            self.store.add(e)
        xuyi_index.rebuild(self.store, self.scope)

    def tearDown(self): self.store.close()

    def test_a_kept_question_brings_the_answer_it_got(self):
        out = Retriever(self.store).recall(self.scope, '我不是问过你哪家好喝', 1600, focus=True)
        texts = [p['text'] for p in out['sources']]
        self.assertIn('哪家的好喝你说说', texts)
        self.assertIn('云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻', texts)
        self.assertLess(texts.index('哪家的好喝你说说'),
                        texts.index('云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻'))

    def test_the_models_own_lookup_is_told_not_to_invent(self):
        retriever = Retriever(self.store)
        focused = retriever.recall(self.scope, '哪家的好喝', 1600, focus=True)['context']
        plain = retriever.recall(self.scope, '哪家的好喝', 1600)['context']
        self.assertIn('没证据别编', focused)
        self.assertNotIn('没证据别编', plain)   # 自动召回不背这句，省预算


class DayScopeTests(unittest.TestCase):
    """问「昨天」就只在昨天里找：今天的补话（哪怕是模型自己编的）不能挤掉昨天的原话。"""

    def setUp(self):
        from xuyi import index as xuyi_index
        self.store = Store(':memory:')
        self.scope = 'qq:c2c:' + 'd' * 64
        # 用相对日期，测试不会因为跑过午夜而失效（实机踩过）。
        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        yesterday = (now - timedelta(days=1)).date()
        today = now.date()

        def stamp(day, hour, minute=0):
            return datetime(day.year, day.month, day.day, hour, minute,
                            tzinfo=now.tzinfo).isoformat()

        rows = [
            (stamp(yesterday, 16, 57), 'user', '哪个牌子的好喝你说说'),
            (stamp(yesterday, 16, 57), 'assistant', '云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻'),
            (stamp(yesterday, 16, 57), 'user', '有啥推荐吗'),
            (stamp(yesterday, 16, 57), 'assistant', '奶茶配个蛋挞吧，别要全糖的'),
            (stamp(today, 16, 24), 'user', '昨天你让我喝哪家奶茶'),
            (stamp(today, 16, 25), 'assistant', '那就算我说的街角奶茶，选它不会错。'),
        ]
        for n, (stamp, role, text) in enumerate(rows):
            e = event(f'dsh:d:{n}', text, scope=self.scope)
            e['recorded_at'] = stamp
            e['role'], e['origin'] = role, ('quoted' if role == 'user' else 'assistant')
            if role == 'assistant': e['sender_id'] = 'assistant'
            self.store.add(e)
        xuyi_index.rebuild(self.store, self.scope)

    def tearDown(self): self.store.close()

    def test_yesterday_stays_yesterday_and_the_answer_comes_along(self):
        out = Retriever(self.store).recall(self.scope, '昨天你让我喝哪家奶茶', 1600, focus=True)
        texts = [p['text'] for p in out['sources']]
        self.assertIn('云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻', texts)
        self.assertNotIn('那就算我说的街角奶茶，选它不会错。', texts)

    def test_plain_recall_also_respects_the_day(self):
        out = Retriever(self.store).recall(self.scope, '昨天你让我喝哪家奶茶', 1600)
        self.assertIn('云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻', out['context'])
        self.assertNotIn('街角奶茶', out['context'])

    def test_keyword_only_tool_query_still_uses_the_users_day(self):
        # 模型调用工具时会把查询改写成关键词、丢掉「昨天」；用户刚问的那句话里还带着。
        out = Retriever(self.store).recall(self.scope, '奶茶 推荐 喝什么', 1600, focus=True)
        self.assertIn('云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻', out['context'])
        self.assertNotIn('街角奶茶', out['context'])

    def test_several_ways_of_asking_the_same_thing(self):
        # 同一件事换几种问法（含模型改写成关键词），都要找回原话，且不能把今天的补话当证据。
        wordings = ['昨天你让我喝哪家奶茶', '昨天你让我喝哪家奶茶来着',
                    '奶茶 推荐 喝什么', '什么牌子来着', '你上次推荐我喝什么奶茶']
        for query in wordings:
            with self.subTest(query=query):
                out = Retriever(self.store).recall(self.scope, query, 1600, focus=True)
                self.assertIn('云朵茶铺的桂花乌龙，清清爽爽，一下午都喝不腻', out['context'])
                self.assertNotIn('街角奶茶', out['context'])


class LocalTimeDisplayTests(unittest.TestCase):
    """展示按本地时间；同一天内说"N分钟前"，让模型分得出"刚才"和"今天早些时候"。"""
    def test_same_day_recency_is_minute_level_and_clock_time_is_local(self):
        from datetime import datetime, timedelta, timezone
        from xuyi.retrieval import local_moment, relative_day, render_line
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        at = lambda delta: (now - delta).isoformat().replace('+00:00', 'Z')
        self.assertEqual(relative_day(at(timedelta(seconds=30)), now=now), '刚刚')
        self.assertEqual(relative_day(at(timedelta(minutes=8)), now=now), '8分钟前')
        self.assertEqual(relative_day(at(timedelta(minutes=125)), now=now), '2小时前')
        self.assertEqual(relative_day(at(timedelta(days=3)), now=now), '3天前')
        # 显式给出 today（日期）时保持按天的说法，旧调用方不变
        from datetime import date
        self.assertEqual(relative_day(at(timedelta(minutes=8)), today=date(2026, 11, 9)), '今天')
        # 钟点是本地时间：同一时刻，用 UTC 写和用 +08:00 写，显示完全一致
        utc, plus8 = '2026-11-09T08:45:00Z', '2026-11-09T16:45:00+08:00'
        self.assertEqual(local_moment(utc), local_moment(plus8))
        piece = lambda stamp: dict(event_id='e', recorded_at=stamp, sender_id='u', text='好冷')
        self.assertEqual(render_line(piece(utc), today=date(2026, 11, 9)),
                         render_line(piece(plus8), today=date(2026, 11, 9)))

    def test_render_shows_clock_and_recency_for_a_few_minutes_ago(self):
        from datetime import datetime, timedelta, timezone
        from xuyi.retrieval import render_line
        recent = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
        line = render_line(dict(event_id='e', recorded_at=recent, sender_id='u', text='好冷'))
        self.assertRegex(line, r'\| \d{4}-\d\d-\d\d \d\d:\d\d\(8分钟前\) \|')
