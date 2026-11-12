import sys
import unittest
from test_store import event
from xuyi.extraction import extract
from xuyi.retrieval import Retriever
from xuyi.states import States
from xuyi.store import MemoryError, Store, digest

SCOPE = 'private:U1'


def snapshot(mid='m1', quote='我家的猫叫甲号。', value='甲号'):
    return {'through_id':mid,'extractor':'test',
            'records':[{'subject_id':'U1','attribute':'宠物名','value':value,'status':'confirmed',
                        'effective_at':None,'sources':[{'event_id':mid,'quote':quote}]}]}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.add(event())
        self.states = States(self.store)

    def tearDown(self):
        self.store.close()

    def test_compare_and_set_and_rollback(self):
        first = self.states.propose(SCOPE,snapshot(),0)
        self.store.add(event('m2','更正：猫叫丁号。'))
        self.assertTrue(self.states.view(SCOPE)['stale'])
        second = self.states.propose(SCOPE,snapshot('m2','猫叫丁号','丁号'),1)
        self.assertEqual(second['verification'],'unverified')
        with self.assertRaises(MemoryError):
            self.states.propose(SCOPE,snapshot(),1)
        self.assertEqual(self.states.view(SCOPE),second)
        rolled = self.states.rollback(SCOPE,first['version'],2)
        self.assertTrue(rolled['stale'])
        self.assertEqual(rolled['revision'],3)
        self.assertEqual(rolled['candidate']['records'][0]['value'],'甲号')

    def test_source_quote_and_role_checked(self):
        with self.assertRaises(MemoryError):
            self.states.propose(SCOPE,snapshot(quote='不存在'),0)
        reply=event('m2','建议买相机。')
        reply.update(role='assistant',origin='assistant')
        self.store.add(reply)
        with self.assertRaises(MemoryError):
            self.states.propose(SCOPE,snapshot('m2','建议买相机'),0)
        self.assertEqual(self.states.versions(SCOPE),[])

    def test_restore_candidate_then_prevent_resurrection(self):
        self.states.propose(SCOPE,snapshot(),0)
        bundle=self.store.export_bundle(SCOPE)
        with Store(':memory:') as recovered:
            recovered.restore(bundle)
            self.assertEqual(States(recovered).view(SCOPE), self.states.view(SCOPE))
            recovered.forget(SCOPE,'m1')
            recovered.restore(bundle)
            self.assertEqual(States(recovered).versions(SCOPE),[])
            self.assertIsNone(States(recovered).view(SCOPE)['candidate'])

    def test_legacy_archive_restore(self):
        payload={'format':'xuyi-memory/1','scope':SCOPE,'events':[event()],'tombstones':[]}
        with Store(':memory:') as recovered:
            self.assertEqual(recovered.restore({'payload':payload,'sha256':digest(payload)})['inserted'],1)

    def test_recall_pairs_candidate_with_evidence_and_skips_stale(self):
        self.states.propose(SCOPE,snapshot(),0)
        r=Retriever(self.store)
        self.assertEqual(len(r.recall(SCOPE,'甲号')['state_candidates']),1)
        self.assertEqual(r.recall(SCOPE,'甲号',budget=100)['state_candidates'],[])
        self.store.add(event('m2','更正：猫叫丁号。'))
        out=r.recall(SCOPE,'甲号')
        self.assertTrue(out['state_stale'])
        self.assertEqual(out['state_candidates'],[])

    def test_extractor_failure_preserves_archive_and_head(self):
        self.states.propose(SCOPE,snapshot(),0)
        old=self.states.view(SCOPE)
        with self.assertRaises(MemoryError):
            extract(self.store,SCOPE,[sys.executable,'-c','print("not JSON")'])
        self.assertEqual(self.states.view(SCOPE),old)
        self.assertIsNotNone(self.store.get(SCOPE,'m1'))

    def test_extractor_pipeline_reads_actual_input(self):
        cmd = [sys.executable,'-c',"""
import json,sys
request=json.load(sys.stdin)
e=request['messages'][-1]
print(json.dumps([dict(subject_id=e['sender_id'],attribute='原话',value=e['text'],
 status='uncertain',effective_at=None,sources=[dict(event_id=e['event_id'],quote=e['text'])])]))
"""]
        out=extract(self.store,SCOPE,cmd)
        self.assertEqual(out['state']['candidate']['records'][0]['value'],event()['text'])
        self.assertLessEqual(out['input_used'],out['input_budget'])

    def test_withdrawal_survives_restore_then_old_import(self):
        self.states.propose(SCOPE,snapshot(),0)
        old=self.store.export_bundle(SCOPE)
        self.states.rollback(SCOPE,None,1)
        with Store(':memory:') as recovered:
            recovered.restore(self.store.export_bundle(SCOPE))
            recovered.restore(old)
            self.assertIsNone(States(recovered).view(SCOPE)['candidate'])
            self.assertEqual(States(recovered).view(SCOPE)['revision'],2)

    def test_empty_extractor_does_not_discard_previous_candidate(self):
        first=self.states.propose(SCOPE,snapshot(),0)
        out=extract(self.store,SCOPE,[sys.executable,'-c','print("[]")'])
        self.assertEqual(out['status'],'empty_candidate')
        self.assertEqual(out['state'],first)
