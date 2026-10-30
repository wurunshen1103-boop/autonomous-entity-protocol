import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from app.core import Engine, AEPError


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.e=Engine()
        self.a=self.e.register('Alpha')['agent_id']
        self.b=self.e.register('Beta')['agent_id']
        self.c=self.e.register('Gamma')['agent_id']

    def tearDown(self):
        self.e.close()

    def task(self, reward=15):
        return self.e.create_task(self.a,'Research','Public data',reward)['task_id']

    def assert_conserved(self, engine=None):
        e=engine or self.e
        balances=e.db.execute('SELECT SUM(balance) FROM agents').fetchone()[0]
        held=e.db.execute("SELECT COALESCE(SUM(reward),0) FROM tasks WHERE status IN ('open','accepted','submitted')").fetchone()[0]
        grants=e.db.execute("SELECT SUM(amount) FROM ledger WHERE reason='demo genesis grant'").fetchone()[0]
        self.assertEqual(balances+held,grants)
        self.assertEqual(e.db.execute('SELECT COUNT(*) FROM agents WHERE balance<0').fetchone()[0],0)

    def test_cancel_refunds_once_and_rejects_foreign_actor(self):
        t=self.task()
        with self.assertRaises(AEPError): self.e.cancel(self.b,t)
        self.e.cancel(self.a,t)
        self.assertEqual(self.e.profile(self.a)['balance'],100)
        for action in (self.e.cancel,self.e.approve,self.e.accept):
            with self.assertRaises(AEPError): action(self.a,t)
        self.assertEqual([r['action'] for r in self.e.task_audit(self.a,t)],['created','cancelled'])
        self.assert_conserved()

    def test_revision_release_reassignment_and_payout(self):
        t=self.task()
        self.e.accept(self.b,t)
        with self.assertRaises(AEPError): self.e.cancel(self.a,t)
        with self.assertRaises(AEPError): self.e.release(self.c,t)
        self.e.submit(self.b,t,'First draft')
        with self.assertRaises(AEPError): self.e.release(self.b,t)
        with self.assertRaises(AEPError): self.e.revise(self.b,t)
        self.e.revise(self.a,t)
        self.e.release(self.b,t)
        self.e.accept(self.c,t)
        self.e.submit(self.c,t,'Final draft')
        self.e.approve(self.a,t)
        self.assertEqual(self.e.profile(self.c)['balance'],115)
        self.assertEqual(self.e.profile(self.b)['balance'],100)
        self.assertEqual([r['action'] for r in self.e.task_audit(self.a,t)],
                         ['created','accepted','submitted','revision_requested','released','accepted','submitted','completed'])
        with self.assertRaises(AEPError): self.e.task_audit(self.b,t)
        self.assert_conserved()

    def test_unknown_actor_cannot_mutate(self):
        with self.assertRaises(AEPError): self.e.post('unknown','Text')
        with self.assertRaises(AEPError): self.e.create_task('unknown','Title','Desc',1)
        with self.assertRaises(AEPError): self.e.accept('unknown',self.task())

    def race(self, calls):
        def invoke(call):
            try: call(); return True
            except AEPError: return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(invoke,calls))

    def test_concurrent_spending_accept_and_payout(self):
        results=self.race([lambda:self.task(60) for _ in range(8)])
        self.assertEqual(sum(results),1)
        t=self.e.tasks()[0]['id']
        results=self.race([lambda:self.e.accept(self.b,t),lambda:self.e.accept(self.c,t)])
        self.assertEqual(sum(results),1)
        worker=self.e.tasks()[0]['worker']
        self.e.submit(worker,t,'Done')
        results=self.race([lambda:self.e.approve(self.a,t) for _ in range(8)])
        self.assertEqual(sum(results),1)
        self.assertEqual(self.e.profile(worker)['balance'],160)
        self.assert_conserved()

    def test_cancel_vs_accept_has_one_winner(self):
        for _ in range(25):
            t=self.task(1)
            results=self.race([lambda:self.e.cancel(self.a,t),lambda:self.e.accept(self.b,t)])
            self.assertEqual(sum(results),1)
            self.assert_conserved()

    def test_registration_and_payout_roll_back_with_ledger_failure(self):
        self.e.db.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON ledger BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): self.e.register('Delta')
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM agents').fetchone()[0],3)
        self.e.db.execute('DROP TRIGGER fail_ledger')
        t=self.task()
        self.e.accept(self.b,t)
        self.e.submit(self.b,t,'Done')
        self.e.db.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON ledger BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.e.approve(self.a,t)
        self.assertEqual(self.e.tasks()[0]['status'],'submitted')
        self.assertEqual(self.e.profile(self.b)['balance'],100)
        self.assert_conserved()

    def test_disk_persistence_and_multiple_connections(self):
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'test.sqlite3')
            e1,e2=Engine(path),Engine(path)
            try:
                a=e1.register('Alpha')
                aid=a['agent_id']
                results=self.race([lambda:e1.create_task(aid,'Title','Desc',60),lambda:e2.create_task(aid,'Title','Desc',60)])
                self.assertEqual(sum(results),1)
                self.assert_conserved(e1)
            finally:
                e1.close(); e2.close()
            restored=Engine(path)
            try:
                self.assertEqual(restored.authenticate(a['api_token']),aid)
                self.assertEqual(restored.profile(aid)['balance'],40)
                restored.cancel(aid,restored.tasks()[0]['id'])
                self.assertEqual(restored.profile(aid)['balance'],100)
                self.assert_conserved(restored)
            finally: restored.close()

    def test_one_hundred_tasks_ten_agents(self):
        workers=[self.b,self.c]+[self.e.register('Worker '+str(i))['agent_id'] for i in range(7)]
        for i in range(100):
            t=self.task(1)
            w=workers[i%len(workers)]
            self.e.accept(w,t)
            self.e.submit(w,t,'Verifiable demo result '+str(i))
            self.e.approve(self.a,t)
            self.assert_conserved()
        self.assertEqual(self.e.db.execute("SELECT COUNT(*) FROM tasks WHERE status='completed'").fetchone()[0],100)
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],400)
