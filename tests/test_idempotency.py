import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from app.core import Engine, AEPError, IdempotencyConflict


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.e=Engine()
        self.a=self.e.register('Alpha')['agent_id']
        self.b=self.e.register('Beta')['agent_id']
        self.data={'title':'Evidence','description':'Summarize sources','reward':15}

    def tearDown(self):
        self.e.close()

    def create(self, key='create-1', data=None, actor=None):
        return self.e.task_request(actor or self.a,'create',data or self.data,request_key=key)

    def count(self, table):
        return self.e.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_replay_and_canonical_object_order(self):
        original=self.create()
        self.assertEqual(original,self.create(data=dict(reversed(list(self.data.items())))))
        original['task_id']=999
        self.assertEqual(self.create()['task_id'],1)
        self.assertEqual(self.count('tasks'),1)
        self.assertEqual(self.count('audit'),1)
        self.assertEqual(self.count('ledger'),3)
        self.assertEqual(self.e.profile(self.a)['balance'],85)

    def test_key_cannot_be_reused_with_changed_payload_operation_or_task(self):
        t=self.create()['task_id']
        with self.assertRaises(IdempotencyConflict): self.create(data={**self.data,'reward':16})
        with self.assertRaises(IdempotencyConflict): self.e.task_request(self.a,'cancel',{},t,'create-1')
        t2=self.create('second')['task_id']
        self.e.task_request(self.a,'cancel',{},t,'cancel-1')
        with self.assertRaises(IdempotencyConflict): self.e.task_request(self.a,'cancel',{},t2,'cancel-1')
        self.assertEqual(self.count('tasks'),2)
        self.assertEqual(self.e.profile(self.a)['balance'],85)

    def test_actor_scoping_and_invalid_actor(self):
        self.create()
        other=self.create(actor=self.b)
        self.assertEqual(other['task_id'],2)
        with self.assertRaises(AEPError): self.create(actor='unknown')
        self.assertEqual(self.count('idempotency'),2)

    def test_concurrent_duplicate_create_only_one_charge(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses=list(pool.map(lambda _:self.create(),range(16)))
        self.assertTrue(all(r==responses[0] for r in responses))
        self.assertEqual(self.count('tasks'),1)
        self.assertEqual(self.e.profile(self.a)['balance'],85)
        self.assertEqual(self.count('idempotency'),1)

    def test_successful_transitions_replay_without_rerunning(self):
        t=self.create()['task_id']
        actions=[(self.b,'accept',{}),(self.b,'submit',{'submission':'Draft'}),
                 (self.a,'revise',{}),(self.b,'submit',{'submission':'Final'}),
                 (self.a,'approve',{})]
        for i,(actor,action,data) in enumerate(actions):
            key='step-'+str(i)
            first=self.e.task_request(actor,action,data,t,key)
            self.assertEqual(first,self.e.task_request(actor,action,data,t,key))
        self.assertEqual(self.e.profile(self.b)['balance'],115)
        # Replaying an old accept returns its original response even after completion.
        self.assertEqual(self.e.task_request(self.b,'accept',{},t,'step-0')['status'],'accepted')
        self.assertEqual(self.e.tasks()[0]['status'],'completed')
        self.assertEqual(self.count('audit'),6)
        self.assertEqual(self.count('ledger'),4)

    def test_failed_business_request_does_not_consume_key(self):
        t=self.create()['task_id']
        with self.assertRaises(AEPError): self.e.task_request(self.b,'submit',{'submission':'Done'},t,'submit-1')
        self.assertEqual(self.count('idempotency'),1)
        self.e.accept(self.b,t)
        self.e.task_request(self.b,'submit',{'submission':'Done'},t,'submit-1')
        self.assertEqual(self.count('idempotency'),2)

    def test_cache_write_failure_rolls_back_task_balance_ledger_and_audit(self):
        self.e.db.execute("CREATE TRIGGER fail_cache BEFORE INSERT ON idempotency BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.create()
        self.assertEqual(self.count('tasks'),0)
        self.assertEqual(self.count('audit'),0)
        self.assertEqual(self.count('ledger'),2)
        self.assertEqual(self.e.profile(self.a)['balance'],100)
        self.e.db.execute('DROP TRIGGER fail_cache')
        self.create()
        self.assertEqual(self.e.profile(self.a)['balance'],85)

    def test_key_and_payload_validation(self):
        for key in ('','has space','x'*129,'中文',3):
            with self.assertRaises(AEPError): self.create(key)
        with self.assertRaises(AEPError): self.create('valid', {**self.data,'ignored':float('nan')})
        self.assertEqual(self.count('tasks'),0)
        self.create('valid._:-123')

    def test_reopen_and_independent_connection_replay(self):
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'state.sqlite3')
            first,second=Engine(path),Engine(path)
            try:
                a=first.register('Alpha')['agent_id']
                def call(e): return e.task_request(a,'create',self.data,request_key='persistent')
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses=list(pool.map(call,[first,second]))
                self.assertEqual(responses[0],responses[1])
                self.assertEqual(first.profile(a)['balance'],85)
            finally: first.close(); second.close()
            restored=Engine(path)
            try:
                self.assertEqual(call(restored),responses[0])
                self.assertEqual(restored.profile(a)['balance'],85)
                self.assertEqual(len(restored.tasks()),1)
            finally: restored.close()

    def test_concurrent_conflicting_payloads_have_one_winner(self):
        def create(reward):
            try:
                return self.create('shared-key',{**self.data,'reward':reward})
            except IdempotencyConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(create,[15,20]))
        self.assertEqual(sum(r is not None for r in responses),1)
        self.assertEqual(self.count('tasks'),1)
        self.assertEqual(self.e.profile(self.a)['balance']+self.e.tasks()[0]['reward'],100)

    def test_v03_database_gains_cache_without_losing_state(self):
        from app.core import SCHEMA
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'old.sqlite3')
            old=sqlite3.connect(path)
            old.executescript('\n'.join(line for line in SCHEMA.splitlines() if 'CREATE TABLE IF NOT EXISTS idempotency' not in line))
            old.execute('INSERT INTO agents VALUES (?,?,?,?,?)',('existing','Old Agent','old-token-hash',100,0))
            old.commit(); old.close()
            upgraded=Engine(path)
            try:
                first=upgraded.task_request('existing','create',self.data,request_key='after-upgrade')
                self.assertEqual(first,upgraded.task_request('existing','create',self.data,request_key='after-upgrade'))
                self.assertEqual(upgraded.profile('existing')['balance'],85)
                self.assertEqual(upgraded.profile('existing')['name'],'Old Agent')
            finally: upgraded.close()
