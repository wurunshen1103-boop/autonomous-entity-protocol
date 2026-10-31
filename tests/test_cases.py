import copy
import json
import sqlite3
import threading
import tempfile
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from app.core import Engine,AEPError,IdempotencyConflict
from app.market import Market
from app.market_rules import EVIDENCE_RULE
from app.wallet import Wallet,encrypt_content,wrap_content_key
from app.evidence import seal_evidence,open_evidence,verify_history
from app.server import make_server
from app.client import Client,APIError

TERMS={'rule':'evidence-v1','required_fields':['summary'],'accept_seconds':3600,'work_seconds':3600,'review_seconds':60,'evidence_seconds':60,'response_seconds':60}

class CaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.wallets=[Wallet() for _ in range(3)]
    def setUp(self):
        self.e=Engine();self.m=Market(self.e);self.c=self.e.contracts.cases
        self.agents=[self.e.register(n) for n in ('Buyer','Seller','Outsider')];self.ids=[a['agent_id'] for a in self.agents]
        for a,w in zip(self.ids,self.wallets):self.m.enroll(a,w.enrollment(a,self.m.network))
    def tearDown(self):self.e.close()
    def cmd(self,i,action,body):
        a=self.ids[i];return self.m.execute(a,self.wallets[i].command(a,self.m.network,self.m.wallet(a)['next_seq'],action,body))
    def task_case(self):
        tid=self.cmd(0,'task:create',{'data':{'title':'Fixture','description':'Public contract','reward':15,'contract':TERMS}})['task_id']
        self.cmd(1,'task:accept',{'task_id':tid,'data':{'expected_contract':TERMS}})
        self.cmd(1,'task:submit',{'task_id':tid,'data':{'submission':'{"summary":"fixture delivered"}'}})
        cid=self.cmd(0,'task:dispute',{'task_id':tid,'data':{'reason':'quality_dispute'}})['case_id']
        return tid,self.c.view(self.ids[0],cid)
    def entry(self,case,i=0,eid='evidence_00000001',**kw):
        return seal_evidence(self.wallets[i].decrypt,case,self.ids[i],eid,{'text':'PRIVATE EVIDENCE SENTINEL'},trusted_recipients={a:self.c.key(a) for a in (case['claimant'],case['respondent'])},**kw)
    def test_round_trip_no_plaintext_and_access(self):
        tid,c=self.task_case();b=self.entry(c)
        receipt=self.cmd(0,'case:append',{'case_id':c['id'],'entry':b})
        c=self.c.view(self.ids[1],c['id']);entry=c['entries'][0]
        self.assertEqual(open_evidence(self.wallets[1].decrypt,c,self.ids[1],entry),{'text':'PRIVATE EVIDENCE SENTINEL'})
        self.assertEqual(self.c.append(self.ids[0],c['id'],b),receipt)
        verify_history(c,[receipt['record_hash']])
        damaged=copy.deepcopy(c);damaged['entries']=[]
        with self.assertRaises(ValueError):verify_history(damaged,[receipt['record_hash']])
        self.assertNotIn('PRIVATE EVIDENCE SENTINEL','\n'.join(self.e.db.iterdump()))
        with self.assertRaises(PermissionError):self.c.view(self.ids[2],c['id'])
        with self.assertRaises(PermissionError):self.c.append(self.ids[2],c['id'],b)
        self.assertEqual(self.c.list(self.ids[2]),[])
        with self.assertRaises(ValueError):open_evidence(self.wallets[2].decrypt,c,self.ids[1],entry)
        changed=copy.deepcopy(b);changed['envelope']['nonce']='bad'
        with self.assertRaises(IdempotencyConflict):self.c.append(self.ids[0],c['id'],changed)
    def test_recipient_substitution_rejected(self):
        _,c=self.task_case();pinned=dict(c['recipients']);c['recipients'][self.ids[1]]=self.c.key(self.ids[2])
        with self.assertRaises(ValueError):seal_evidence(self.wallets[0].decrypt,c,self.ids[0],'evidence_00000001',{'text':'secret'},trusted_recipients=pinned)

    def test_response_windows_and_settlement_cannot_bypass(self):
        tid,c=self.task_case();self.c.append(self.ids[0],c['id'],self.entry(c))
        b=self.entry(c,1,'response_00000001',kind='response',reply_to='evidence_00000001')
        with self.assertRaises(AEPError):self.c.append(self.ids[1],c['id'],b)
        with patch('time.time',return_value=c['evidence_deadline']):
            with self.assertRaises(AEPError):self.cmd(0,'task:settle',{'task_id':tid})
            with self.assertRaises(AEPError):self.c.append(self.ids[0],c['id'],self.entry(c,eid='evidence_00000002'))
            self.c.append(self.ids[1],c['id'],b)
            self.assertEqual(self.c.view(self.ids[0],c['id'])['phase'],'response')
        with patch('time.time',return_value=c['settle_after']):
            with self.assertRaises(AEPError):self.c.append(self.ids[1],c['id'],self.entry(c,1,'response_00000002',kind='response',reply_to='evidence_00000001'))
            result=self.cmd(1,'task:settle',{'task_id':tid})
            self.assertEqual(result['worker_amount'],7)
            self.assertEqual(self.c.view(self.ids[0],c['id'])['phase'],'resolved')
            self.assertEqual(self.c.append(self.ids[1],c['id'],b)['entry_id'],b['entry_id'])
            with self.assertRaises(AEPError):self.cmd(0,'task:settle',{'task_id':tid})
        self.assertEqual(sum(self.e.profile(a)['balance'] for a in self.ids),300)
    def test_context_quota_and_counterparty_reply_validation(self):
        _,c=self.task_case();b=self.entry(c);bad=copy.deepcopy(b);bad['envelope']['context']['network']='another'
        with self.assertRaises(AEPError):self.c.append(self.ids[0],c['id'],bad)
        for i in range(20):self.c.append(self.ids[0],c['id'],self.entry(c,eid='entry_%016d'%i))
        with self.assertRaises(AEPError):self.c.append(self.ids[0],c['id'],b)
        with patch('time.time',return_value=c['evidence_deadline']):
            with self.assertRaises(AEPError):self.c.append(self.ids[0],c['id'],self.entry(c,eid='response_00000001',kind='response',reply_to='entry_%016d'%0))
    def test_atomic_signed_receipt_and_settlement_rollback(self):
        tid,c=self.task_case();b=self.entry(c)
        self.e.db.execute("CREATE TRIGGER reject_command BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.cmd(0,'case:append',{'case_id':c['id'],'entry':b})
        self.assertEqual(self.c.view(self.ids[0],c['id'])['entries'],[])
        self.e.db.execute('DROP TRIGGER reject_command')
        self.e.db.execute("CREATE TRIGGER reject_close BEFORE UPDATE OF resolution ON cases BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with patch('time.time',return_value=c['settle_after']):
            with self.assertRaises(sqlite3.IntegrityError):self.cmd(0,'task:settle',{'task_id':tid})
        self.assertEqual(self.e.profile(self.ids[1])['balance'],100)
        self.assertEqual(self.e._one('SELECT status FROM tasks WHERE id=?',(tid,))['status'],'disputed')
        self.assertIsNone(self.c.view(self.ids[0],c['id'])['resolution'])
    def test_concurrent_settlement_once(self):
        tid,c=self.task_case()
        def settle(_):
            try:return self.e.task_request(self.ids[0],'settle',{},tid)['worker_amount']
            except AEPError:return None
        with patch('time.time',return_value=c['settle_after']),ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(settle,range(8))).count(7),1)
        self.assertEqual(self.e.profile(self.ids[1])['balance'],107)
    def test_market_case_and_legacy_terms(self):
        blob,key=encrypt_content('fixture')
        lid=self.cmd(1,'list',{'title':'Fixture','price':15,'capacity':1,'blob':blob,'settlement':EVIDENCE_RULE})['listing_id'];l=self.m.listings()[0]
        oid=self.cmd(0,'buy',{'listing_id':lid,'expected_price':15,'expected_digest':l['digest'],'expected_terms_hash':l['terms_hash']})['order_id']
        self.cmd(1,'deliver',{'order_id':oid,'wrapped_key':wrap_content_key(key,self.m.order(self.ids[1],oid))})
        cid=self.cmd(0,'dispute',{'order_id':oid,'reason':'cannot_decrypt'})['case_id'];c=self.c.view(self.ids[0],cid)
        self.cmd(0,'case:append',{'case_id':cid,'entry':self.entry(c)})
        with patch('time.time',return_value=c['snapshot']['review_deadline']):
            with self.assertRaises(AEPError):self.cmd(1,'settle',{'order_id':oid})
        with patch('time.time',return_value=c['settle_after']):self.assertEqual(self.cmd(1,'settle',{'order_id':oid})['seller_amount'],7)
        self.assertIsNotNone(self.c.view(self.ids[0],cid)['resolution'])
        self.assertIsNone(self.m.order(self.ids[0],oid)['license'])
    def test_human_key_required_immutable_and_no_wallet_access(self):
        h=self.e.register('Human',kind='human')['agent_id'];payload={'title':'Test','description':'Test','reward':15,'contract':TERMS}
        with self.assertRaises(AEPError):self.e.task_request(h,'create',payload)
        self.assertEqual(self.e.profile(h)['balance'],100)
        pub=self.wallets[2].enrollment(h,self.m.network)['message']['encryption_public']
        self.c.register_key(h,pub);self.c.register_key(h,pub)
        with self.assertRaises(AEPError):self.c.register_key(h,self.c.key(self.ids[0]))
        self.e.task_request(h,'create',payload)
        self.assertFalse(self.m.enrolled(h))
    def test_restart_preserves_cases_and_chain(self):
        _,c=self.task_case();self.c.append(self.ids[0],c['id'],self.entry(c))
        with tempfile.TemporaryDirectory() as directory:
            db=sqlite3.connect(directory+'/case.sqlite3');self.e.db.backup(db);db.close()
            reopened=Engine(directory+'/case.sqlite3');Market(reopened)
            try:reread=reopened.contracts.cases.view(self.ids[1],c['id'])
            finally:reopened.close()
        self.assertEqual(reread['entries'][0]['previous_hash'],'0'*64)
        self.assertEqual(reread['recipients'],c['recipients'])
    def test_http_human_and_agent_routes(self):
        server=make_server(self.e,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port)
            _,c=self.task_case();a=Client(url,self.agents[0]['api_token']);other=Client(url,self.agents[2]['api_token'])
            self.assertEqual(a.request('GET','/cases')[0]['id'],c['id'])
            with self.assertRaises(APIError) as err:other.request('GET','/cases/'+str(c['id']))
            self.assertEqual(err.exception.status,401)
            with self.assertRaises(APIError):a.request('POST',f"/cases/{c['id']}/entries",self.entry(c))
            h=self.e.register('Human',kind='human');hc=Client(url,h['api_token']);w=self.wallets[2]
            hc.request('POST','/case-key',{'public_key':w.enrollment(h['agent_id'],self.m.network)['message']['encryption_public']})
            tid=hc.request('POST','/requests',{'title':'Fixture','description':'Fixture','reward':15,'contract':TERMS},'human-new')['task_id']
            self.cmd(1,'task:accept',{'task_id':tid,'data':{'expected_contract':TERMS}});self.cmd(1,'task:submit',{'task_id':tid,'data':{'submission':'{"summary":"done"}'}})
            cid=hc.request('POST',f'/requests/{tid}/dispute',{'reason':'quality_dispute'},'human-dispute')['case_id']
            case=hc.request('GET',f'/cases/{cid}');entry=seal_evidence(w.decrypt,case,h['agent_id'],'human_entry_000001',{'text':'private human evidence'},trusted_recipients={h['agent_id']:w.enrollment(h['agent_id'],self.m.network)['message']['encryption_public'],self.ids[1]:self.c.key(self.ids[1])})
            one=hc.request('POST',f'/cases/{cid}/entries',entry);two=hc.request('POST',f'/cases/{cid}/entries',entry)
            self.assertEqual(one,two)
        finally:server.shutdown();thread.join();server.server_close()

    def test_session_unknown_response_restart_and_intent_binding(self):
        from app.agent_session import AgentSession
        server=make_server(self.e,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            _,case=self.task_case();url='http://127.0.0.1:'+str(server.server_port)
            pinned={a:self.c.key(a) for a in (case['claimant'],case['respondent'])}
            with tempfile.TemporaryDirectory() as directory:
                secret=os.urandom(32);path=directory+'/session.vault'
                session=AgentSession.create(path,secret,url,self.ids[0],self.agents[0]['api_token'],self.wallets[0],self.m.network)
                original=session.client.request
                def lost(method,path,data=None,key=None):
                    result=original(method,path,data,key)
                    if path=='/commands':raise TimeoutError('fixture lost successful response')
                    return result
                session.client.request=lost
                try:
                    with self.assertRaises(TimeoutError):session.submit_evidence('case-entry',case['id'],'durable_entry_0001',{'text':'private'},trusted_recipients=pinned)
                finally:session.close()
                with AgentSession(path,secret,url) as restored:
                    result=restored.submit_evidence('case-entry',case['id'],'durable_entry_0001',{'text':'private'},trusted_recipients=pinned)
                    self.assertEqual(result['entry_id'],'durable_entry_0001')
                    with self.assertRaises(ValueError):restored.submit_evidence('case-entry',case['id'],'durable_entry_0001',{'text':'changed'},trusted_recipients=pinned)
                self.assertEqual(len(self.c.view(self.ids[0],case['id'])['entries']),1)
        finally:server.shutdown();thread.join();server.server_close()
