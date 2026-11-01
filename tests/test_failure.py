import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from app.core import Engine,AEPError
from app.market import Market
from app.wallet import Wallet
from app.server import make_server
from app.client import Client,APIError

TERMS={'rule':'failure-v1','required_fields':['summary'],'accept_seconds':3600,'work_seconds':3600,'review_seconds':60,'evidence_seconds':60,'response_seconds':60}

class FailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.wallets=[Wallet(),Wallet(),Wallet()]
    def setUp(self):
        self.e=Engine();self.m=Market(self.e);self.a=[self.e.register(x) for x in ('Publisher','Worker','Outsider')];self.ids=[x['agent_id'] for x in self.a]
        for a,w in zip(self.ids,self.wallets):self.m.enroll(a,w.enrollment(a,self.m.network))
    def tearDown(self):self.e.close()
    def cmd(self,i,action,data,tid=None):
        a=self.ids[i];b={'data':data}
        if tid is not None:b['task_id']=tid
        return self.m.execute(a,self.wallets[i].command(a,self.m.network,self.m.wallet(a)['next_seq'],action,b))
    def task(self,submitted=True,disputed=False,reward=15,terms=None):
        terms=terms or TERMS
        tid=self.cmd(0,'task:create',{'title':'Fixture','description':'Synthetic task','reward':reward,'contract':terms})['task_id']
        self.cmd(1,'task:accept',{'expected_contract':terms},tid)
        if submitted:self.cmd(1,'task:submit',{'submission':'{"summary":"partial fixture result"}'},tid)
        if disputed:self.cmd(0,'task:dispute',{'reason':'incomplete_delivery'},tid)
        return tid
    def test_worker_failure_full_refund_before_and_after_delivery(self):
        for submitted,disputed in ((False,False),(True,False),(True,True)):
            tid=self.task(submitted,disputed)
            with self.assertRaises(AEPError):self.cmd(0,'task:fail',{},tid)
            result=self.cmd(1,'task:fail',{},tid)
            self.assertEqual((result['worker_amount'],result['refund']),(0,15))
            with self.assertRaises(AEPError):self.cmd(1,'task:fail',{},tid)
            self.assertEqual(self.e.profile(self.ids[0])['balance'],100)
            if disputed:self.assertEqual(self.e.contracts.cases.list(self.ids[0])[0]['resolved'],True)
    def test_partial_requires_both_and_caps_worker_share(self):
        tid=self.task(disputed=True)
        with self.assertRaises(AEPError):self.cmd(0,'task:accept_partial',{'expected_worker_amount':3},tid)
        with self.assertRaises(AEPError):self.cmd(0,'task:offer_partial',{'worker_amount':3},tid)
        for bad in (4,-1,True,1.5,'3',None):
            with self.assertRaises(AEPError):self.cmd(1,'task:offer_partial',{'worker_amount':bad},tid)
        self.cmd(1,'task:offer_partial',{'worker_amount':3},tid)
        self.assertEqual(self.e.profile(self.ids[1])['balance'],100)
        with self.assertRaises(AEPError):self.cmd(1,'task:offer_partial',{'worker_amount':2},tid)
        with self.assertRaises(AEPError):self.cmd(1,'task:accept_partial',{'expected_worker_amount':3},tid)
        with self.assertRaises(AEPError):self.cmd(0,'task:accept_partial',{'expected_worker_amount':2},tid)
        self.assertEqual(self.cmd(0,'task:accept_partial',{'expected_worker_amount':3},tid)['refund'],12)
        self.assertEqual(self.e.profile(self.ids[1])['balance'],103)
        self.assertEqual(self.e.profile(self.ids[0])['balance'],97)
        self.assertTrue(self.e.contracts.cases.list(self.ids[0])[0]['resolved'])
    def test_tiny_reward_rounds_in_publisher_favor(self):
        tid=self.task(reward=4)
        with self.assertRaises(AEPError):self.cmd(1,'task:offer_partial',{'worker_amount':1},tid)
        self.cmd(1,'task:offer_partial',{'worker_amount':0},tid)
        self.assertEqual(self.cmd(0,'task:accept_partial',{'expected_worker_amount':0},tid)['refund'],4)
    def test_legacy_rule_cannot_change(self):
        tid=self.task(terms=TERMS|{'rule':'evidence-v1'})
        for action,data in (('task:fail',{}),('task:offer_partial',{'worker_amount':3}),('task:accept_partial',{'expected_worker_amount':3})):
            with self.assertRaises(AEPError):self.cmd(1,action,data,tid)
        self.assertEqual(self.e.profile(self.ids[0])['balance'],85)
    def test_no_delivery_and_unproved_allegation_distinct(self):
        tid=self.task(submitted=False)
        due=self.e.contracts.get(tid)['work_deadline']
        with patch('time.time',return_value=due):self.assertEqual(self.cmd(0,'task:settle',{},tid)['refund'],15)
        tid=self.task(disputed=True);case=self.e.contracts.cases.view(self.ids[0],self.e.contracts.cases.list(self.ids[0])[0]['id'])
        self.cmd(1,'task:offer_partial',{'worker_amount':3},tid)
        with patch('time.time',return_value=case['settle_after']):self.assertEqual(self.cmd(0,'task:settle',{},tid)['refund'],8)
        with self.assertRaises(AEPError):self.cmd(0,'task:accept_partial',{'expected_worker_amount':3},tid)
    def test_receipt_failure_rolls_back_agreement_and_balance(self):
        tid=self.task(disputed=True);self.cmd(1,'task:offer_partial',{'worker_amount':3},tid)
        self.e.db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.cmd(0,'task:accept_partial',{'expected_worker_amount':3},tid)
        self.assertIsNone(self.e.contracts.get(tid)['resolution'])
        self.assertFalse(self.e.contracts.cases.list(self.ids[0])[0]['resolved'])
        self.assertEqual(self.e.profile(self.ids[1])['balance'],100)
    def test_replay_and_race_settle_once(self):
        tid=self.task();self.cmd(1,'task:offer_partial',{'worker_amount':3},tid)
        actor=self.ids[0]
        envelope=self.wallets[0].command(actor,self.m.network,self.m.wallet(actor)['next_seq'],'task:accept_partial',{'task_id':tid,'data':{'expected_worker_amount':3}})
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda _:self.m.execute(actor,envelope),range(6)))
        self.assertTrue(all(r==results[0] for r in results));self.assertEqual(self.e.profile(self.ids[1])['balance'],103)
        self.assertEqual(sum(self.e.profile(a)['balance'] for a in self.ids),300)
    def test_human_acceptance_and_signed_agent_bypass(self):
        server=make_server(self.e,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port);client=Client(url)
            h=client.request('POST','/humans',{'name':'Human'});hc=Client(url,h['api_token']);pub=self.wallets[2].enrollment(h['agent_id'],self.m.network)['message']['encryption_public']
            hc.request('POST','/case-key',{'public_key':pub})
            tid=hc.request('POST','/requests',{'title':'Fixture','description':'Fixture','reward':15,'contract':TERMS},'create')['task_id']
            self.cmd(1,'task:accept',{'expected_contract':TERMS},tid);self.cmd(1,'task:submit',{'submission':'{"summary":"partial"}'},tid)
            self.cmd(1,'task:offer_partial',{'worker_amount':3},tid)
            a=Client(url,self.a[1]['api_token'])
            with self.assertRaises(APIError) as err:a.request('POST',f'/tasks/{tid}/fail',{},'unsigned')
            self.assertEqual(err.exception.status,403)
            result=hc.request('POST',f'/requests/{tid}/accept_partial',{'expected_worker_amount':3},'accept')
            self.assertEqual(result['refund'],12)
            self.assertEqual(hc.request('POST',f'/requests/{tid}/accept_partial',{'expected_worker_amount':3},'accept'),result)
        finally:server.shutdown();thread.join();server.server_close()
