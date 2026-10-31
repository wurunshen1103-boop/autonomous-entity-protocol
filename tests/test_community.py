import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from app.core import Engine,AEPError,IdempotencyConflict
from app.market import Market
from app.market_rules import RULE
from app.wallet import Wallet,encrypt_content,wrap_content_key,seal_memory,open_memory
from app.client import Client,APIError
from app.server import make_server
from app.agent_session import AgentSession
from app.community_demo import TERMS

class CommunityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.wallets=[Wallet(),Wallet(),Wallet()]
    def setUp(self):
        self.e=Engine();self.m=Market(self.e);self.a=[self.e.register(x) for x in ('Alpha','Beta','Gamma')];self.ids=[x['agent_id'] for x in self.a]
        for i in range(3):self.m.enroll(self.ids[i],self.wallets[i].enrollment(self.ids[i],self.m.network))
        self.h=self.e.register('Human',kind='human');self.hid=self.h['agent_id']
    def tearDown(self):self.e.close()
    def cmd(self,i,action,body):return self.m.execute(self.ids[i],self.wallets[i].command(self.ids[i],self.m.network,self.m.wallet(self.ids[i])['next_seq'],action,body))
    def task(self,reward=15):return self.e.task_request(self.hid,'create',{'title':'Task','description':'Test','reward':reward,'contract':TERMS},request_key='create')['task_id']
    def accept(self,tid):return self.cmd(0,'task:accept',{'task_id':tid,'data':{'expected_contract':TERMS}})
    def submit(self,tid):return self.cmd(0,'task:submit',{'task_id':tid,'data':{'submission':json.dumps({'summary':'done','evidence':'fixture'})}})
    def conserved(self):
        total=self.e.db.execute('SELECT SUM(balance) FROM agents').fetchone()[0]
        total+=self.e.db.execute("SELECT COALESCE(SUM(reward),0) FROM tasks WHERE status IN ('open','accepted','submitted','disputed')").fetchone()[0]
        total+=self.e.db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE state IN ('funded','delivered')").fetchone()[0]
        self.assertEqual(total,400)
    def test_relationship_consent_and_private_graph(self):
        self.cmd(0,'relation:set',{'target':self.ids[1],'kind':'collaborate'})
        self.assertEqual(self.m.community.graph()['edges'],[])
        with self.assertRaises(AEPError):self.cmd(2,'relation:accept',{'target':self.ids[0],'kind':'collaborate'})
        self.cmd(1,'relation:accept',{'target':self.ids[0],'kind':'collaborate'})
        self.assertEqual(len(self.m.community.graph()['edges']),1)
        self.cmd(1,'relation:remove',{'target':self.ids[0],'kind':'collaborate'})
        self.assertEqual(self.m.community.graph()['edges'],[])
        self.assertNotIn('balance',json.dumps(self.m.community.graph()))
    def test_expert_profiles_and_subscription_activity(self):
        self.cmd(0,'profile:set',{'bio':'Unverified expertise','skills':['statistics']})
        self.assertFalse(self.m.community.experts()[0]['verified'])
        self.cmd(1,'relation:set',{'target':self.ids[0],'kind':'subscribe'})
        self.cmd(0,'post',{'body':'Public update'})
        self.assertEqual(len(self.m.community.activity(self.ids[1])['posts']),1)
    def test_private_memory_ciphertext_owner_version_and_context(self):
        mid='memory_test_00001';doc={'title':'SECRET-TITLE','text':'SECRET-CONTENT','tags':['private']}
        env=seal_memory(self.wallets[0],self.m.network,self.ids[0],mid,1,doc)
        result=self.cmd(0,'memory:put',{'id':mid,'expected_version':0,'envelope':env})
        row=self.m.community.memory(self.ids[0],mid)
        self.assertEqual(open_memory(self.wallets[0],self.m.network,self.ids[0],mid,1,row['envelope']),doc)
        with self.assertRaises(ValueError):open_memory(self.wallets[1],self.m.network,self.ids[0],mid,1,env)
        with self.assertRaises(ValueError):open_memory(self.wallets[0],self.m.network,self.ids[0],mid,2,env)
        with self.assertRaises(AEPError):self.m.community.memory(self.ids[1],mid)
        with self.assertRaises(IdempotencyConflict):self.cmd(0,'memory:put',{'id':mid,'expected_version':0,'envelope':env})
        dump='\n'.join(self.e.db.iterdump());self.assertNotIn('SECRET-TITLE',dump);self.assertNotIn('SECRET-CONTENT',dump)
        self.cmd(0,'memory:delete',{'id':mid,'expected_version':1})
        self.assertTrue(self.m.community.memory(self.ids[0],mid)['deleted'])
        self.assertEqual(result['version'],1)
    def test_memory_receipt_failure_rolls_back(self):
        env=seal_memory(self.wallets[0],self.m.network,self.ids[0],'memory_test_00001',1,{'text':'private'})
        self.e.db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.cmd(0,'memory:put',{'id':'memory_test_00001','expected_version':0,'envelope':env})
        self.assertEqual(self.m.community.memories(self.ids[0]),[])
    def test_project_invitation_member_acceptance_and_personal_budget(self):
        pid=self.cmd(0,'project:create',{'title':'Team','description':'Private members'})['project_id']
        with self.assertRaises(PermissionError):self.m.community.project(self.ids[2],pid)
        with self.assertRaises(PermissionError):self.cmd(2,'project:join',{'project_id':pid})
        self.cmd(0,'project:invite',{'project_id':pid,'target':self.ids[1]});self.cmd(1,'project:join',{'project_id':pid})
        self.assertEqual(self.m.community.graph()['edges'],[])
        tid=self.cmd(0,'project:task',{'project_id':pid,'data':{'title':'Review','description':'Public task','reward':10,'contract':TERMS}})['task_id']
        with self.assertRaises(PermissionError):self.cmd(2,'task:accept',{'task_id':tid,'data':{'expected_contract':TERMS}})
        self.cmd(1,'task:accept',{'task_id':tid,'data':{'expected_contract':TERMS}})
        self.assertEqual(self.e.profile(self.ids[0])['balance'],90);self.assertEqual(self.e.profile(self.ids[1])['balance'],100);self.conserved()
    def test_human_cannot_impersonate_worker(self):
        tid=self.cmd(0,'task:create',{'data':{'title':'Test','description':'Test','reward':10}})['task_id']
        with self.assertRaises(AEPError):self.e.task_request(self.hid,'accept',{},task_id=tid)
    def test_contract_explicit_acceptance_and_shape(self):
        tid=self.task()
        with self.assertRaises(AEPError):self.cmd(0,'task:accept',{'task_id':tid})
        self.accept(tid)
        with self.assertRaises(AEPError):self.cmd(0,'task:submit',{'task_id':tid,'data':{'submission':'{"summary":"only"}'}})
        self.submit(tid);self.e.task_request(self.hid,'approve',{},task_id=tid,request_key='approve')
        self.e.task_request(self.hid,'approve',{},task_id=tid,request_key='approve')
        self.assertEqual(self.e.profile(self.ids[0])['balance'],115);self.conserved()
    def test_unaccepted_and_undelivered_timeout_refunds(self):
        tid=self.task();due=self.e.contracts.get(tid)['accept_deadline']
        with patch('app.contracts.time.time',return_value=due):self.e.task_request(self.hid,'settle',{},task_id=tid,request_key='settle')
        self.assertEqual(self.e.profile(self.hid)['balance'],100)
        tid=self.e.task_request(self.hid,'create',{'title':'Next','description':'Next','reward':15,'contract':TERMS})['task_id'];self.accept(tid)
        due=self.e.contracts.get(tid)['work_deadline']
        with patch('app.contracts.time.time',return_value=due):
            with self.assertRaises(AEPError):self.submit(tid)
            self.e.task_request(self.hid,'settle',{},task_id=tid)
        self.conserved()
    def test_dispute_split_and_duplicate_settlement(self):
        tid=self.task();self.accept(tid);self.submit(tid)
        self.e.task_request(self.hid,'dispute',{'reason':'quality mismatch'},task_id=tid)
        with self.assertRaises(AEPError):self.e.task_request(self.hid,'approve',{},task_id=tid)
        due=self.e.contracts.get(tid)['review_deadline']
        with patch('app.contracts.time.time',return_value=due):
            results=[]
            def settle(actor):
                try:return self.e.task_request(actor,'settle',{},task_id=tid)
                except AEPError:return None
            with ThreadPoolExecutor(2) as pool:results=list(pool.map(settle,(self.hid,self.ids[0])))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.e.profile(self.ids[0])['balance'],107)
        self.assertEqual(self.e.profile(self.hid)['balance'],93);self.conserved()
    def test_undisputed_review_timeout_pays_worker(self):
        tid=self.task();self.accept(tid);self.submit(tid);due=self.e.contracts.get(tid)['review_deadline']
        with patch('app.contracts.time.time',return_value=due):self.cmd(0,'task:settle',{'task_id':tid})
        self.assertEqual(self.e.profile(self.ids[0])['balance'],115);self.conserved()
    def test_failed_contract_audit_rolls_back_settlement(self):
        tid=self.task();self.accept(tid);self.submit(tid)
        self.e.db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.e.task_request(self.hid,'approve',{},task_id=tid)
        self.assertEqual(self.e._one('SELECT status FROM tasks WHERE id=?',(tid,))['status'],'submitted');self.conserved()
    def test_human_create_replay_after_balance_changes(self):
        tid=self.task(30)
        self.e.task_request(self.hid,'create',{'title':'Other','description':'Test','reward':30,'contract':TERMS},request_key='other')
        self.assertEqual(self.task(30),tid);self.assertEqual(self.e.profile(self.hid)['balance'],40)
        with self.assertRaises(AEPError):self.e.task_request(self.hid,'create',{'title':'Too much','description':'Test','reward':15,'contract':TERMS})
    def fixed_order(self):
        blob,key=encrypt_content('private information')
        lid=self.cmd(0,'list',{'title':'Dataset check','price':15,'capacity':2,'blob':blob,'settlement':RULE,'metadata':{'importance':4,'confidence':.6,'source_type':'synthetic'}})['listing_id']
        listing=self.m.listings()[0]
        with self.assertRaises(AEPError):self.cmd(1,'buy',{'listing_id':lid,'expected_price':15,'expected_digest':listing['digest']})
        oid=self.cmd(1,'buy',{'listing_id':lid,'expected_price':15,'expected_digest':listing['digest'],'expected_terms_hash':listing['terms_hash']})['order_id']
        self.cmd(0,'deliver',{'order_id':oid,'wrapped_key':wrap_content_key(key,self.m.order(self.ids[0],oid))})
        return oid
    def test_market_fixed_dispute_and_no_private_trade_graph(self):
        oid=self.fixed_order();self.assertEqual(self.m.community.graph()['edges'],[])
        self.cmd(1,'dispute',{'order_id':oid,'reason':'cannot_decrypt'})
        with self.assertRaises(AEPError):self.cmd(1,'confirm',{'order_id':oid})
        due=self.m.order(self.ids[1],oid)['review_deadline']
        with patch('app.market_rules.time.time',return_value=due):self.cmd(0,'settle',{'order_id':oid})
        self.assertEqual(self.e.profile(self.ids[0])['balance'],107)
        self.assertEqual(self.e.profile(self.ids[1])['balance'],93)
        self.assertIsNone(self.m.order(self.ids[1],oid)['license']);self.conserved()
    def test_market_undisputed_review_expiry(self):
        oid=self.fixed_order();due=self.m.order(self.ids[1],oid)['review_deadline']
        with patch('app.market_rules.time.time',return_value=due):self.cmd(0,'settle',{'order_id':oid})
        self.assertIsNotNone(self.m.order(self.ids[1],oid)['license']);self.conserved()

class CommunityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.e=Engine();self.server=make_server(self.e,0);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port);self.client=Client(self.url)
    def tearDown(self):self.server.shutdown();self.thread.join();self.server.server_close();self.e.close()
    def enroll(self):
        a=self.client.request('POST','/agents',{'name':'Agent'});w=Wallet();c=Client(self.url,a['api_token']);network=self.server.market.network
        c.request('POST','/wallet/enroll',w.enrollment(a['agent_id'],network));return a,w,network
    def test_portal_assets_origin_guard_and_human_flow(self):
        with urlopen(self.url+'/portal') as r:
            self.assertEqual(r.status,200);self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy']);self.assertIn('task-form',r.read().decode())
        for path in ('/portal.js','/portal.css'):
            with urlopen(self.url+path) as r:self.assertEqual(r.status,200)
        req=Request(self.url+'/humans',b'{"name":"Attack"}',{'Content-Type':'application/json','Origin':'https://evil.invalid'},method='POST')
        with self.assertRaises(HTTPError) as exc:urlopen(req)
        self.assertEqual(exc.exception.code,403)
        h=self.client.request('POST','/humans',{'name':'Human'});c=Client(self.url,h['api_token'])
        body={'title':'Research','description':'Public','reward':15,'contract':TERMS}
        first=c.request('POST','/requests',body,'request-one')
        self.assertEqual(first,c.request('POST','/requests',body,'request-one'))
        self.assertEqual(c.request('GET','/publisher')['balance'],85)
        c.request('POST',f"/requests/{first['task_id']}/cancel",{},'cancel-one')
        self.assertEqual(c.request('GET','/publisher')['balance'],100)
    def test_encrypted_session_restart_uncertain_response_and_lock(self):
        a,w,network=self.enroll();secret=os.urandom(32)
        with tempfile.TemporaryDirectory() as d:
            path=d+'/agent.vault';session=AgentSession.create(path,secret,self.url,a['agent_id'],a['api_token'],w,network)
            with self.assertRaises(BlockingIOError):AgentSession(path,secret,self.url)
            original=session.client.request
            def lost(method,path,data=None,key=None):
                result=original(method,path,data,key)
                if path=='/commands':raise ConnectionError('lost successful response')
                return result
            with patch.object(session.client,'request',side_effect=lost):
                with self.assertRaises(ConnectionError):session.command('one','task:create',{'data':{'title':'Test','description':'Test','reward':10}})
            session.close();session=AgentSession(path,secret,self.url)
            result=session.retry_pending();self.assertEqual(result['status'],'open');self.assertEqual(session.check_binding()['balance'],90)
            with self.assertRaises(ValueError):session.command('one','post',{'body':'changed'})
            session.remember('memory','persistent_note_01',{'title':'SECRET-SENTINEL','text':'some private thought'})
            session.close();session=AgentSession(path,secret,self.url)
            self.assertEqual(len(session.search('thought')),1)
            with self.assertRaises(ValueError):session.remember('memory','persistent_note_01',{'text':'changed'})
            self.assertNotIn('SECRET-SENTINEL',open(path).read());self.assertNotIn(a['api_token'],open(path).read())
            self.assertEqual(os.stat(path).st_mode&0o777,0o600);session.close()
            with self.assertRaises(Exception):AgentSession(path,os.urandom(32),self.url)
    def test_memory_http_other_actor_cannot_read(self):
        a,w,network=self.enroll();c=Client(self.url,a['api_token']);mid='short_note'
        env=seal_memory(w,network,a['agent_id'],mid,1,{'text':'secret'})
        # Identifier must meet protocol length rule.
        with self.assertRaises(APIError):c.request('POST','/commands',w.command(a['agent_id'],network,1,'memory:put',{'id':mid,'expected_version':0,'envelope':env}))
        mid='private_note_0001';env=seal_memory(w,network,a['agent_id'],mid,1,{'text':'secret'})
        c.request('POST','/commands',w.command(a['agent_id'],network,1,'memory:put',{'id':mid,'expected_version':0,'envelope':env}))
        other=self.client.request('POST','/agents',{'name':'Other'})
        with self.assertRaises(APIError):Client(self.url,other['api_token']).request('GET','/memories/'+mid)
        with self.assertRaises(APIError):self.client.request('GET','/memories/'+mid)

    def test_task_runner_decides_and_does_not_repeat_uncertain_callback(self):
        from app.task_agent import TaskAgent
        a,w,network=self.enroll();h=self.client.request('POST','/humans',{'name':'Publisher'});c=Client(self.url,h['api_token'])
        c.request('POST','/requests',{'title':'Research','description':'Fixture','reward':15,'contract':TERMS},'runner-task')
        task=c.request('GET','/requests')[0]
        with tempfile.TemporaryDirectory() as d:
            secret=os.urandom(32)
            with AgentSession.create(d+'/agent.vault',secret,self.url,a['agent_id'],a['api_token'],w,network) as session:
                def interrupted(task):raise ConnectionError('provider response lost')
                runner=TaskAgent(session,lambda task:True,interrupted)
                with self.assertRaises(ConnectionError):runner.run(task)
            with AgentSession(d+'/agent.vault',secret,self.url) as session:
                runner=TaskAgent(session,lambda task:self.fail('decision repeated'),lambda task:self.fail('execution repeated'))
                with self.assertRaises(RuntimeError):runner.run(task)
                runner.reconcile(task['id'],{'summary':'recovered result','evidence':'fixture'})
                self.assertEqual(runner.run(task)['status'],'submitted')
                self.assertEqual(runner.run(task)['status'],'submitted')

    def test_v06_migration_preserves_legacy_order_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            path=d+'/legacy.sqlite3';e=Engine(path)
            e.db.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY,listing_id INTEGER,buyer TEXT,seller TEXT,amount INTEGER,state TEXT,deadline INTEGER,wrapped_key TEXT,delivery_proof TEXT,purchase_proof TEXT)')
            m=Market(e);network=m.network
            self.assertIn('review_deadline',{r[1] for r in e.db.execute('PRAGMA table_info(orders)')})
            e.close();e=Engine(path);m=Market(e)
            self.assertEqual(m.network,network);e.close()
