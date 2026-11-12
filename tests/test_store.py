import copy
from pathlib import Path
import tempfile
import unittest

from xuyi.store import Store, MemoryError, digest


def event(mid="m1", text="我家的猫叫甲号。", scope="private:U1", **extra):
    return dict(scope=scope, event_id=mid, sender_id="U1", role="user", origin="human_direct",
                recorded_at="2026-11-07T12:00:00+08:00", text=text, **extra)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"memory.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_restart_preserves_exact_unicode(self):
        e = event(text='原话\n“甲号🐈”，不是丁号。')
        self.store.add(e)
        with Store(self.path) as other:
            self.assertEqual(other.get(e['scope'], 'm1'), e)

    def test_dedup_scope_and_conflict(self):
        self.assertEqual(self.store.add(event()), 'inserted')
        self.assertEqual(self.store.add(event()), 'duplicate')
        with self.assertRaises(MemoryError): self.store.add(event(text='不同内容'))
        self.store.add(event(scope='group:1', text='另一个人'))
        self.assertEqual(len(self.store.events('private:U1')), 1)
        self.assertIsNone(self.store.get('group:2', 'm1'))

    def test_batch_is_atomic(self):
        self.store.add(event())
        with self.assertRaises(MemoryError):
            self.store.add_many([event('m2'), event(text='冲突')])
        self.assertIsNone(self.store.get('private:U1','m2'))

    def test_revision_retains_source(self):
        self.store.add(event())
        self.store.add(event('m2', '更正，猫叫丁号。', supersedes='m1'))
        self.assertEqual(len(self.store.events('private:U1')), 2)
        with self.assertRaises(MemoryError): self.store.add(event('m3', supersedes='absent'))

    def test_restore_empty_and_idempotent(self):
        self.store.add_many([event(),event('m2','我现在喜欢茶。')])
        bundle = self.store.export_bundle('private:U1')
        with Store(':memory:') as new:
            self.assertEqual(new.restore(bundle)['inserted'],2)
            self.assertEqual(new.restore(bundle)['duplicate'],2)
            self.assertEqual(new.events('private:U1'),self.store.events('private:U1'))

    def test_corruption_and_cross_scope_rejected(self):
        self.store.add(event())
        bundle = self.store.export_bundle('private:U1')
        broken = copy.deepcopy(bundle);broken['payload']['events'][0]['text']='被改了'
        with self.assertRaises(MemoryError): self.store.restore(broken)
        broken['sha256']=digest(broken['payload'])
        broken['payload']['events'][0]['scope']='group:9';broken['sha256']=digest(broken['payload'])
        with self.assertRaises(MemoryError): self.store.restore(broken)

    def test_old_bundle_cannot_resurrect_deleted(self):
        self.store.add(event());old=self.store.export_bundle('private:U1')
        self.store.forget('private:U1','m1')
        self.assertEqual(self.store.restore(old)['deleted'],1)
        self.assertEqual(self.store.events('private:U1'),[])
        latest=self.store.export_bundle('private:U1')
        with Store(':memory:') as new:
            new.restore(latest);new.restore(old)
            self.assertEqual(new.events('private:U1'),[])

    def test_restore_conflict_rolls_back_deletion(self):
        self.store.add_many([event(),event('m2','第二条')])
        incoming=self.store.export_bundle('private:U1')
        incoming['payload']['tombstones']=[{'scope':'private:U1','event_id':'m1','deleted_at':'2026-11-07T13:00:00+08:00'}]
        incoming['payload']['events']=[event('m2','冲突')]
        incoming['sha256']=digest(incoming['payload'])
        with self.assertRaises(MemoryError):self.store.restore(incoming)
        self.assertIsNotNone(self.store.get('private:U1','m1'))

    def test_revision_survives_deletion_of_its_parent(self):
        self.store.add(event())
        self.store.add(event('m2','更正：猫叫丁号。',supersedes='m1'))
        self.store.forget('private:U1','m1')
        with Store(':memory:') as new:
            new.restore(self.store.export_bundle('private:U1'))
            self.assertEqual([e['event_id'] for e in new.events('private:U1')],['m2'])

    def test_no_fake_human_role_or_naive_time(self):
        e=event();e['role']='assistant'
        with self.assertRaises(MemoryError):self.store.add(e)
        e=event();e['recorded_at']='2026-11-07T12:00:00'
        with self.assertRaises(MemoryError):self.store.add(e)


if __name__=='__main__':unittest.main()
