import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.core import Engine,AEPError,IdempotencyConflict
from app.market import Market
from app.wallet import Wallet,encrypt_content,wrap_content_key,public_sign,recovery_request
from app.server import make_server
from app.client import Client,APIError


class PrivateMarketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=[Wallet() for _ in range(3)]
    def setUp(self):
        self.e=Engine();self.m=Market(self.e)
        self.agents=[self.e.register(n) for n in ('Seller','Buyer','Other buyer')]
        self.ids=[a['agent_id'] for a in self.agents]
        self.recovery=[Ed25519PrivateKey.generate() for _ in self.ids]
        for i,a in enumerate(self.ids): self.m.enroll(a,self.keys[i].enrollment(a,self.m.network,public_sign(self.recovery[i])))
    def tearDown(self):self.e.close()
    def envelope(self,i,action,body):
        return self.keys[i].command(self.ids[i],self.m.network,self.m.wallet(self.ids[i])['next_seq'],action,body)
    def command(self,i,action,body):return self.m.execute(self.ids[i],self.envelope(i,action,body))
    def listing(self,capacity=2):
        self.plaintext='Confidential synthetic research result: PRIVATE-SENTINEL-xyz'
        self.blob,self.secret=encrypt_content(self.plaintext)
        return self.command(0,'list',{'title':'Public summary','price':15,'capacity':capacity,'blob':self.blob})['listing_id']
    def buy(self,i=1,lid=None):
        listing=next(l for l in self.m.listings() if lid is None or l['id']==lid)
        return self.command(i,'buy',{'listing_id':listing['id'],'expected_price':listing['price'],'expected_digest':listing['digest']})['order_id']
    def deliver(self,oid):
        order=self.m.order(self.ids[0],oid)
        self.command(0,'deliver',{'order_id':oid,'wrapped_key':wrap_content_key(self.secret,order)})
        return self.m.order(self.ids[1],oid)
    def conserved(self):
        balances=self.e.db.execute('SELECT SUM(balance) FROM agents').fetchone()[0]
        held=self.e.db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE state IN ('funded','delivered')").fetchone()[0]
        tasks=self.e.db.execute("SELECT COALESCE(SUM(reward),0) FROM tasks WHERE status IN ('open','accepted','submitted')").fetchone()[0]
        self.assertEqual(balances+held+tasks,300)
    def test_end_to_end_no_server_plaintext_or_private_keys(self):
        self.listing();oid=self.buy();order=self.deliver(oid)
        self.assertEqual(self.keys[1].decrypt_order(order),self.plaintext)
        with self.assertRaises(ValueError):self.keys[2].decrypt_order(order)
        self.command(1,'confirm',{'order_id':oid})
        self.assertEqual(self.e.profile(self.ids[0])['balance'],115)
        self.assertEqual(self.m.order(self.ids[1],oid)['license']['resale'],0)
        dump='\n'.join(self.e.db.iterdump())
        self.assertNotIn(self.plaintext,dump);self.assertNotIn('PRIVATE KEY',dump)
        self.assertNotIn(self.secret.hex(),dump);self.conserved()
    def test_token_identity_and_signature_tampering_rejected(self):
        env=self.envelope(1,'policy',{'max_order':10,'reserve':40})
        with self.assertRaises(AEPError):self.m.execute(self.ids[2],env)
        env['message']['body']['reserve']=0
        with self.assertRaises(ValueError):self.m.execute(self.ids[1],env)
        self.assertEqual(self.m.wallet(self.ids[1])['next_seq'],1)
    def test_replay_conflict_expiry_and_network_binding(self):
        env=self.envelope(1,'policy',{'max_order':10,'reserve':40})
        first=self.m.execute(self.ids[1],env)
        with patch('app.market.time.time',return_value=time.time()+100000):self.assertEqual(self.m.execute(self.ids[1],env),first)
        changed=self.keys[1].command(self.ids[1],self.m.network,1,'policy',{'max_order':11,'reserve':40})
        with self.assertRaises(IdempotencyConflict):self.m.execute(self.ids[1],changed)
        expired=self.keys[1].command(self.ids[1],self.m.network,2,'policy',{'max_order':11,'reserve':40},expires_at=int(time.time())-1)
        with self.assertRaises(AEPError):self.m.execute(self.ids[1],expired)
        wrong=self.keys[1].command(self.ids[1],'another-network',2,'policy',{'max_order':11,'reserve':40})
        with self.assertRaises(AEPError):self.m.execute(self.ids[1],wrong)
    def test_concurrent_inventory_and_same_command(self):
        self.listing(capacity=1);terms=self.m.listings()[0]
        body={'listing_id':terms['id'],'expected_price':15,'expected_digest':terms['digest']}
        envs=[self.envelope(i,'buy',body) for i in (1,2)]
        def buy(pair):
            i,env=pair
            try:return self.m.execute(self.ids[i],env)
            except AEPError:return None
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(buy,zip((1,2),envs)))
        self.assertEqual(sum(r is not None for r in results),1)
        winner=1 if results[0] else 2
        env=envs[winner-1]
        self.assertEqual(self.m.execute(self.ids[winner],env),results[winner-1]);self.conserved()
    def test_distinct_buyers_get_distinct_licenses(self):
        self.listing();one=self.buy(1);two=self.buy(2)
        self.deliver(one)
        self.command(1,'confirm',{'order_id':one})
        order=self.m.order(self.ids[0],two)
        self.command(0,'deliver',{'order_id':two,'wrapped_key':wrap_content_key(self.secret,order)})
        self.assertEqual(self.keys[2].decrypt_order(self.m.order(self.ids[2],two)),self.plaintext)
        self.command(2,'confirm',{'order_id':two});self.conserved()
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM licenses').fetchone()[0],2)
    def test_ciphertext_and_key_directory_tampering(self):
        self.listing();oid=self.buy();order=self.m.order(self.ids[0],oid)
        order['buyer_encryption_public']=self.m.enrolled(self.ids[2])['encryption_public']
        with self.assertRaises(ValueError):wrap_content_key(self.secret,order)
        order=self.deliver(oid);order['blob']['ciphertext']='AAAA'
        with self.assertRaises(ValueError):self.keys[1].decrypt_order(order)
    def test_wrong_order_context_cannot_decrypt(self):
        self.listing();oid=self.buy();order=self.deliver(oid)
        order['id']+=10
        with self.assertRaises(ValueError):self.keys[1].decrypt_order(order)
    def test_timeout_refund_and_no_refund_after_delivery(self):
        self.listing();oid=self.buy();deadline=self.m.order(self.ids[1],oid)['deadline']
        with self.assertRaises(AEPError):self.command(1,'refund',{'order_id':oid})
        with patch('app.market.time.time',return_value=deadline):
            self.command(1,'refund',{'order_id':oid})
            with self.assertRaises(AEPError):self.command(1,'refund',{'order_id':oid})
        self.assertEqual(self.e.profile(self.ids[1])['balance'],100)
        other=self.buy();self.deliver(other)
        with patch('app.market.time.time',return_value=deadline+100):
            with self.assertRaises(AEPError):self.command(1,'refund',{'order_id':other})
        self.conserved()
    def test_freeze_needs_preauthorized_key_and_preserves_escrow(self):
        self.listing();oid=self.buy();self.deliver(oid)
        bad=recovery_request(self.recovery[2],self.ids[1],self.m.network,self.keys[1].id,'freeze1')
        with self.assertRaises(ValueError):self.m.freeze(self.ids[1],bad)
        good=recovery_request(self.recovery[1],self.ids[1],self.m.network,self.keys[1].id,'freeze1')
        self.m.freeze(self.ids[1],good);self.m.freeze(self.ids[1],good)
        self.command(1,'confirm',{'order_id':oid})
        self.listing()
        with self.assertRaises(AEPError):self.buy()
        with self.assertRaises(AEPError):self.command(1,'policy',{'max_order':100,'reserve':0})
        self.assertEqual(self.e.profile(self.ids[0])['balance'],115);self.conserved()
    def test_encrypted_backup_opt_in_and_offline_restore(self):
        with self.assertRaises(AEPError):self.m.backup(self.ids[1])
        blob,offline=self.keys[1].backup();self.command(1,'backup',{'blob':blob})
        restored=Wallet.restore(self.m.backup(self.ids[1]),offline)
        self.assertEqual(restored.id,self.keys[1].id)
        with self.assertRaises(InvalidTag):Wallet.restore(blob,os.urandom(32))
        self.assertNotIn('PRIVATE KEY','\n'.join(self.e.db.iterdump()))
    def test_policy_limits_and_signed_tasks_share_ledger(self):
        self.command(1,'policy',{'max_order':10,'reserve':90})
        self.listing()
        with self.assertRaises(AEPError):self.buy()
        task=self.command(1,'task:create',{'data':{'title':'Work','description':'Public test','reward':10}})['task_id']
        self.command(0,'task:accept',{'task_id':task})
        self.command(0,'task:submit',{'task_id':task,'data':{'submission':'Done'}})
        self.command(1,'task:approve',{'task_id':task})
        self.assertEqual(self.e.profile(self.ids[0])['balance'],110);self.conserved()
    def test_failed_receipt_storage_rolls_back_payment(self):
        self.listing()
        self.e.db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.buy()
        self.assertEqual(self.e.profile(self.ids[1])['balance'],100)
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
        self.assertEqual(self.m.wallet(self.ids[1])['next_seq'],1);self.conserved()
    def test_unauthorized_order_and_resale_rejected(self):
        self.listing();oid=self.buy()
        with self.assertRaises(PermissionError):self.m.order(self.ids[2],oid)
        with self.assertRaises(AEPError):self.command(0,'list',{'title':'Resale','price':1,'capacity':1,'blob':self.blob,'resale':True})
    def test_enrollment_no_platform_reset(self):
        with self.assertRaises(AEPError):self.m.enroll(self.ids[1],self.keys[2].enrollment(self.ids[1],self.m.network))
        self.assertEqual(self.m.wallet(self.ids[1])['wallet_id'],self.keys[1].id)
    def test_http_legacy_write_bypass_blocked(self):
        server=make_server(self.e,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        c=Client('http://127.0.0.1:'+str(server.server_port),self.agents[1]['api_token'])
        try:
            with self.assertRaises(APIError) as error:c.request('POST','/tasks',{'title':'Attack','description':'Unsigned','reward':10})
            self.assertEqual(error.exception.status,403)
            response=c.request('POST','/commands',self.envelope(1,'task:create',{'data':{'title':'Work','description':'Signed','reward':10}}))
            self.assertEqual(response['status'],'open')
            self.assertEqual(c.request('GET','/wallet')['balance'],90)
            freeze=recovery_request(self.recovery[1],self.ids[1],self.m.network,self.keys[1].id,'offline-freeze')
            self.assertTrue(Client(c.base).request('POST','/wallet/freeze',freeze)['frozen'])
        finally:server.shutdown();thread.join();server.server_close()
    def test_database_restart_preserves_keys_commands_and_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'market.sqlite3');e=Engine(path);m=Market(e)
            a=e.register('Restored')['agent_id'];m.enroll(a,self.keys[1].enrollment(a,m.network,public_sign(self.recovery[1])))
            env=self.keys[1].command(a,m.network,1,'policy',{'max_order':20,'reserve':40})
            first=m.execute(a,env);network=m.network
            m.freeze(a,recovery_request(self.recovery[1],a,network,self.keys[1].id,'freeze'))
            e.close();e=Engine(path);m=Market(e)
            try:
                self.assertEqual(m.network,network);self.assertEqual(m.execute(a,env),first)
                self.assertEqual(m.wallet(a)['frozen'],1)
            finally:e.close()
