"""Signed commands and opaque information delivery using the existing simulation ledger.

Server/DB administrators remain trusted for settlement and availability. No private
keys are generated, backed up in plaintext, or recovered by this module.
"""
import hashlib
import json
import secrets
import time
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa,ed25519
from .core import AEPError,IdempotencyConflict
from . import market_rules
from .wallet import DOMAIN,canonical,unb64,verify,wallet_id,content_digest

SCHEMA='''
CREATE TABLE IF NOT EXISTS market_config (id INTEGER PRIMARY KEY CHECK(id=1), network TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wallets (actor TEXT PRIMARY KEY, wallet_id TEXT UNIQUE NOT NULL, spend_public TEXT NOT NULL, encryption_public TEXT NOT NULL, recovery_public TEXT, certificate TEXT NOT NULL, frozen INTEGER NOT NULL DEFAULT 0, next_seq INTEGER NOT NULL DEFAULT 1, max_order INTEGER NOT NULL DEFAULT 30, reserve INTEGER NOT NULL DEFAULT 30);
CREATE TABLE IF NOT EXISTS commands (actor TEXT NOT NULL, seq INTEGER NOT NULL, digest TEXT NOT NULL, envelope TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(actor,seq));
CREATE TABLE IF NOT EXISTS freeze_receipts (actor TEXT NOT NULL, nonce TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(actor,nonce));
CREATE TABLE IF NOT EXISTS listings (id INTEGER PRIMARY KEY AUTOINCREMENT, seller TEXT NOT NULL, title TEXT NOT NULL, price INTEGER NOT NULL, capacity INTEGER NOT NULL, blob TEXT NOT NULL, digest TEXT NOT NULL, proof TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER NOT NULL, buyer TEXT NOT NULL, seller TEXT NOT NULL, amount INTEGER NOT NULL, state TEXT NOT NULL, deadline INTEGER NOT NULL, wrapped_key TEXT, delivery_proof TEXT, purchase_proof TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS licenses (order_id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL, holder TEXT NOT NULL, resale INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS backups (actor TEXT PRIMARY KEY, blob TEXT NOT NULL);
'''

def integer(value,low,high):
    if type(value) is not int or not low<=value<=high: raise AEPError('integer out of range')
    return value

def encrypted_blob(blob,max_bytes=12000):
    if not isinstance(blob,dict) or set(blob)!={'nonce','ciphertext'}: raise AEPError('encrypted envelope required')
    if len(unb64(blob['nonce']))!=12 or not 16<=len(unb64(blob['ciphertext']))<=max_bytes:
        raise AEPError('invalid encrypted envelope size')
    return json.dumps(blob)

class Market:
    def __init__(self,engine):
        self.e=engine
        with engine._lock:
            engine.db.executescript(SCHEMA)
            market_rules.ensure_schema(engine)
            engine.db.execute('INSERT OR IGNORE INTO market_config VALUES (1,?)',('aep-'+secrets.token_hex(16),))
            from .community import Community
            self.community=Community(self)
            self.network=engine._one('SELECT network FROM market_config WHERE id=1')['network']
    def enrolled(self,actor):
        with self.e._lock: return self.e._one('SELECT * FROM wallets WHERE actor=?',(actor,))
    def wallet(self,actor):
        with self.e._lock:
            w=self.enrolled(actor)
            if not w: raise AEPError('wallet not enrolled')
            return {k:w[k] for k in ('wallet_id','next_seq','frozen','max_order','reserve')}|{'balance':self.e.profile(actor)['balance'],'network':self.network}
    def enroll(self,actor,envelope):
        with self.e._lock:
            identity=self.e._one('SELECT kind FROM identities WHERE actor=?',(actor,))
            if identity and identity['kind']=='human':raise AEPError('human publisher uses the separate task window')
            msg=envelope['message']
            if set(msg)!={'domain','network','action','actor','spend_public','encryption_public','recovery_public'} or msg['actor']!=actor or msg['network']!=self.network or msg['domain']!=DOMAIN or msg['action']!='enroll': raise AEPError('invalid enrollment context')
            verify(msg['spend_public'],msg,envelope['signature'])
            pub=serialization.load_pem_public_key(msg['encryption_public'].encode())
            if not isinstance(pub,rsa.RSAPublicKey) or pub.key_size!=3072: raise AEPError('RSA-3072 encryption key required')
            if msg['recovery_public'] is not None:
                ed25519.Ed25519PublicKey.from_public_bytes(unb64(msg['recovery_public']))
                if msg['recovery_public']==msg['spend_public']: raise AEPError('use a separate offline freeze key')
            self.e.profile(actor)
            def work():
                old=self.enrolled(actor)
                if old:
                    if json.loads(old['certificate'])!=envelope: raise AEPError('wallet already bound; no platform reset')
                    return self.wallet(actor)
                self.e.db.execute('INSERT INTO wallets(actor,wallet_id,spend_public,encryption_public,recovery_public,certificate) VALUES (?,?,?,?,?,?)',
                                  (actor,wallet_id(msg['spend_public']),msg['spend_public'],msg['encryption_public'],msg['recovery_public'],json.dumps(envelope)))
                return self.wallet(actor)
            return self.e._tx(work)
    def execute(self,actor,envelope):
        with self.e._lock:
            w=self.enrolled(actor)
            if not w: raise AEPError('enroll wallet first')
            msg=envelope['message']
            if set(msg)!={'domain','network','wallet','actor','seq','expires_at','action','body'} or msg['actor']!=actor or msg['network']!=self.network or msg['domain']!=DOMAIN or msg['wallet']!=w['wallet_id']: raise AEPError('invalid command context')
            verify(w['spend_public'],msg,envelope['signature'])
            seq=integer(msg['seq'],1,2**53-1)
            if not isinstance(msg['body'],dict): raise AEPError('body must be object')
            digest=hashlib.sha256(canonical(msg)).hexdigest()
            def work():
                old=self.e._one('SELECT * FROM commands WHERE actor=? AND seq=?',(actor,seq))
                if old:
                    if old['digest']!=digest: raise IdempotencyConflict('sequence reused with different command')
                    return json.loads(old['result'])
                current=self.enrolled(actor)
                if current['next_seq']!=seq: raise IdempotencyConflict('unexpected sequence')
                integer(msg['expires_at'],int(time.time()),int(time.time())+86400)
                result=self._apply(actor,current,msg['action'],msg['body'],envelope)
                self.e.db.execute('INSERT INTO commands VALUES (?,?,?,?,?)',(actor,seq,digest,json.dumps(envelope),json.dumps(result)))
                self.e.db.execute('UPDATE wallets SET next_seq=next_seq+1 WHERE actor=?',(actor,))
                return result
            return self.e._tx(work)
    def _spend(self,actor,w,amount):
        if w['frozen']: raise AEPError('wallet frozen for new spending')
        if amount>w['max_order'] or self.e.profile(actor)['balance']-amount<w['reserve']: raise AEPError('agent spending policy exceeded')
    def _ledger(self,sender,receiver,amount,reason):
        self.e.db.execute('INSERT INTO ledger(sender,receiver,amount,reason,created_at) VALUES (?,?,?,?,?)',(sender,receiver,amount,reason,int(time.time())))
    def _apply(self,actor,w,action,b,envelope):
        db=self.e.db
        if action=='case:append':return self.e.contracts.cases.append(actor,b['case_id'],b['entry'])
        if isinstance(action,str) and action.startswith(('profile:','relation:','project:','memory:')):
            return self.community.apply(actor,action,b)
        if action in ('dispute','settle'):return market_rules.apply(self,actor,action,b)
        if action=='policy':
            if w['frozen']: raise AEPError('cannot change policy while frozen')
            maximum=integer(b['max_order'],1,1000000);reserve=integer(b['reserve'],0,1000000)
            db.execute('UPDATE wallets SET max_order=?,reserve=? WHERE actor=?',(maximum,reserve,actor))
            return {'max_order':maximum,'reserve':reserve}
        if action=='backup':
            value=encrypted_blob(b['blob'])
            db.execute('INSERT OR REPLACE INTO backups VALUES (?,?)',(actor,value))
            return {'stored':'ciphertext only'}
        if action=='list':
            market_rules.listing_metadata(b)
            if not isinstance(b['title'],str) or not 1<=len(b['title'])<=120: raise AEPError('invalid public title')
            price=integer(b['price'],1,1000000);capacity=integer(b['capacity'],1,1000)
            if b.get('resale',False) is not False: raise AEPError('v0.6 supports non-transferable licenses only')
            blob=encrypted_blob(b['blob'])
            cur=db.execute('INSERT INTO listings(seller,title,price,capacity,blob,digest,proof) VALUES (?,?,?,?,?,?,?)',(actor,b['title'],price,capacity,blob,content_digest(b['blob']),json.dumps(envelope)))
            return {'listing_id':cur.lastrowid,'license':'nonexclusive-personal-no-resale'}
        if action=='buy':
            listing=self.e._one('SELECT * FROM listings WHERE id=?',(integer(b['listing_id'],1,2**53-1),))
            if not listing or listing['seller']==actor: raise AEPError('invalid seller or listing')
            if b.get('expected_price')!=listing['price'] or b.get('expected_digest')!=listing['digest']: raise AEPError('listing terms mismatch')
            extra=market_rules.extras(listing)
            if extra['settlement'] and b.get('expected_terms_hash')!=extra['terms_hash']:raise AEPError('explicit settlement terms acceptance required')
            if extra['metadata'].get('valid_until',2**53-1)<=int(time.time()):raise AEPError('information expired')
            active=db.execute("SELECT COUNT(*) FROM orders WHERE listing_id=? AND state!='refunded'",(listing['id'],)).fetchone()[0]
            if active>=listing['capacity']: raise AEPError('license inventory exhausted')
            if db.execute("SELECT 1 FROM orders WHERE listing_id=? AND buyer=? AND state!='refunded'",(listing['id'],actor)).fetchone(): raise AEPError('already ordered')
            self._spend(actor,w,listing['price'])
            db.execute('UPDATE agents SET balance=balance-? WHERE id=?',(listing['price'],actor))
            deadline=int(time.time())+3600
            cur=db.execute("INSERT INTO orders(listing_id,buyer,seller,amount,state,deadline,purchase_proof) VALUES (?,?,?,?,'funded',?,?)",(listing['id'],actor,listing['seller'],listing['price'],deadline,json.dumps(envelope)))
            self._ledger(actor,None,listing['price'],f'order:{cur.lastrowid}:escrow')
            return {'order_id':cur.lastrowid,'state':'funded','delivery_deadline':deadline}
        if action in ('deliver','confirm','refund'):
            order=self.e._one('SELECT * FROM orders WHERE id=?',(integer(b['order_id'],1,2**53-1),))
            if not order: raise AEPError('unknown order')
            if action=='deliver':
                if actor!=order['seller'] or order['state']!='funded' or int(time.time())>=order['deadline']: raise AEPError('delivery unavailable')
                if len(unb64(b['wrapped_key']))!=384: raise AEPError('RSA-3072 wrapped key required')
                listing=self.e._one('SELECT * FROM listings WHERE id=?',(order['listing_id'],))
                terms=market_rules.extras(listing)['settlement']
                review=int(time.time())+terms['review_seconds'] if terms else None
                db.execute("UPDATE orders SET state='delivered',wrapped_key=?,delivery_proof=?,review_deadline=? WHERE id=?",(b['wrapped_key'],json.dumps(envelope),review,order['id']))
            elif action=='confirm':
                if actor!=order['buyer'] or order['state']!='delivered' or order['dispute']: raise AEPError('only buyer can confirm delivered order')
                db.execute("UPDATE orders SET state='completed' WHERE id=?",(order['id'],))
                db.execute('UPDATE agents SET balance=balance+? WHERE id=?',(order['amount'],order['seller']))
                db.execute('INSERT INTO licenses VALUES (?,?,?,0)',(order['id'],order['listing_id'],actor))
                self._ledger(None,order['seller'],order['amount'],f"order:{order['id']}:payout")
            else:
                if actor!=order['buyer'] or order['state']!='funded' or int(time.time())<order['deadline']: raise AEPError('refund requires expired undelivered order')
                db.execute("UPDATE orders SET state='refunded' WHERE id=?",(order['id'],))
                db.execute('UPDATE agents SET balance=balance+? WHERE id=?',(order['amount'],actor))
                self._ledger(None,actor,order['amount'],f"order:{order['id']}:refund")
            return {'order_id':order['id'],'state':{'deliver':'delivered','confirm':'completed','refund':'refunded'}[action]}
        if action=='post': return self.e.post(actor,b['body'])
        if action=='predict': return self.e.predict(actor,b['event_id'],b['probability'],b['rationale'])
        if isinstance(action,str) and action.startswith('task:'):
            task_action=action[5:];data=b.get('data',{})
            if task_action=='create': self._spend(actor,w,integer(data.get('reward'),1,1000000))
            return self.e.task_request(actor,task_action,data,task_id=b.get('task_id'))
        raise AEPError('unsupported signed action')
    def freeze(self,actor,envelope):
        with self.e._lock:
            w=self.enrolled(actor)
            if not w or not w['recovery_public']: raise AEPError('no preauthorized freeze key')
            msg=envelope['message']
            if set(msg)!={'domain','network','wallet','actor','action','nonce','expires_at'} or msg['actor']!=actor or msg['network']!=self.network or msg['wallet']!=w['wallet_id'] or msg['domain']!=DOMAIN or msg['action']!='freeze': raise AEPError('invalid freeze request')
            verify(w['recovery_public'],msg,envelope['signature'])
            if not isinstance(msg['nonce'],str) or not 1<=len(msg['nonce'])<=128: raise AEPError('invalid nonce')
            digest=hashlib.sha256(canonical(msg)).hexdigest()
            def work():
                old=self.e._one('SELECT digest FROM freeze_receipts WHERE actor=? AND nonce=?',(actor,msg['nonce']))
                if old and old['digest']!=digest: raise IdempotencyConflict('freeze nonce conflict')
                if not old:
                    integer(msg['expires_at'],int(time.time()),int(time.time())+86400)
                    self.e.db.execute('INSERT INTO freeze_receipts VALUES (?,?,?)',(actor,msg['nonce'],digest))
                    self.e.db.execute('UPDATE wallets SET frozen=1 WHERE actor=?',(actor,))
                return {'frozen':True,'scope':'new spending only; no key reset or asset transfer'}
            return self.e._tx(work)
    def listings(self):
        with self.e._lock:
            return [{k:r[k] for k in ('id','seller','seller_wallet','title','price','capacity','digest')}|market_rules.extras(dict(r))|{'license':'nonexclusive-personal-no-resale','ratings':'seller-declared, unverified'} for r in self.e.db.execute('SELECT l.*,w.wallet_id AS seller_wallet FROM listings l JOIN wallets w ON l.seller=w.actor ORDER BY l.id DESC LIMIT 100')]
    def order(self,actor,order_id):
        with self.e._lock:
            order=self.e._one('SELECT * FROM orders WHERE id=?',(order_id,))
            if not order or actor not in (order['buyer'],order['seller']): raise PermissionError('order participant required')
            listing=self.e._one('SELECT * FROM listings WHERE id=?',(order['listing_id'],))
            buyer=self.enrolled(order['buyer']);seller=self.enrolled(order['seller'])
            return order|market_rules.extras(listing)|{'purchase_proof':json.loads(order['purchase_proof']),'buyer_wallet':buyer['wallet_id'],'buyer_encryption_public':buyer['encryption_public'],'buyer_certificate':json.loads(buyer['certificate']),
                          'seller_spend_public':seller['spend_public'],'digest':listing['digest'],'blob':json.loads(listing['blob']),
                          'listing_proof':json.loads(listing['proof']), 'delivery_proof':json.loads(order['delivery_proof']) if order['delivery_proof'] else None,
                          'license':self.e._one('SELECT * FROM licenses WHERE order_id=?',(order_id,))}
    def backup(self,actor):
        with self.e._lock:
            row=self.e._one('SELECT blob FROM backups WHERE actor=?',(actor,))
            if not row: raise AEPError('no backup enabled')
            return json.loads(row['blob'])
