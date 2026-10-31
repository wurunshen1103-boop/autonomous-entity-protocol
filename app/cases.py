"""Participant-only evidence records; deterministic settlement, no quality judge."""
import hashlib
import json
import re
import time
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from .core import AEPError, IdempotencyConflict
from .wallet import canonical, unb64

SCHEMA='''
CREATE TABLE IF NOT EXISTS case_keys(actor TEXT PRIMARY KEY, public_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, resource_id INTEGER NOT NULL,
 claimant TEXT NOT NULL, respondent TEXT NOT NULL, network TEXT NOT NULL,
 reason TEXT NOT NULL, snapshot TEXT NOT NULL, recipients TEXT NOT NULL,
 opened_at INTEGER NOT NULL, evidence_deadline INTEGER NOT NULL,
 response_deadline INTEGER NOT NULL, settle_after INTEGER NOT NULL,
 resolution TEXT, UNIQUE(kind,resource_id));
CREATE TABLE IF NOT EXISTS case_entries(
 case_id INTEGER NOT NULL, entry_id TEXT NOT NULL, author TEXT NOT NULL,
 kind TEXT NOT NULL, reply_to TEXT, envelope TEXT NOT NULL, created_at INTEGER NOT NULL,
 digest TEXT NOT NULL, previous_hash TEXT NOT NULL, record_hash TEXT NOT NULL,
 PRIMARY KEY(case_id,entry_id));
'''
REASONS=('cannot_decrypt','description_mismatch','quality_dispute','incomplete_delivery')

def public_key(value):
    if not isinstance(value,str) or len(value)>2000:raise AEPError('RSA-3072 public key required')
    try: key=serialization.load_pem_public_key(value.encode())
    except (ValueError,TypeError):raise AEPError('invalid evidence public key')
    if not isinstance(key,rsa.RSAPublicKey) or key.key_size!=3072:raise AEPError('RSA-3072 public key required')
    return key.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()

def fingerprint(value):return hashlib.sha256(public_key(value).encode()).hexdigest()

def context(case,author,entry_id,kind,reply_to):
    return {'purpose':'aep-case-evidence-v1','network':case['network'],'case_id':case['id'],
            'author':author,'entry_id':entry_id,'kind':kind,'reply_to':reply_to,
            'recipients':{a:fingerprint(k) for a,k in case['recipients'].items()}}

class Cases:
    def __init__(self,engine):
        self.e=engine;engine.db.executescript(SCHEMA)
    def register_key(self,actor,value):
        """Human encryption only; never registers a spending key or replaces a key."""
        with self.e._lock:
            def write():
                identity=self.e._one('SELECT kind FROM identities WHERE actor=?',(actor,))
                if not identity or identity['kind']!='human':raise PermissionError('human publisher required')
                key=public_key(value);old=self.e._one('SELECT public_key FROM case_keys WHERE actor=?',(actor,))
                if old and old['public_key']!=key:raise AEPError('evidence key immutable; no platform reset')
                self.e.db.execute('INSERT OR IGNORE INTO case_keys VALUES (?,?)',(actor,key))
                return {'actor':actor,'fingerprint':fingerprint(key)}
            return self.e._tx(write)
    def key(self,actor):
        # Wallet table is present in the HTTP runtime; pure legacy Engine usage need not have it.
        if self.e._one("SELECT name FROM sqlite_master WHERE type='table' AND name='wallets'"):
            row=self.e._one('SELECT encryption_public FROM wallets WHERE actor=?',(actor,))
            if row:return public_key(row['encryption_public'])
        row=self.e._one('SELECT public_key FROM case_keys WHERE actor=?',(actor,))
        if not row:raise AEPError('participant must register an evidence encryption key first')
        return row['public_key']
    def open(self,kind,rid,claimant,respondent,reason,terms,review_deadline,snapshot):
        if reason not in REASONS:raise AEPError('use a dispute reason code; evidence belongs in ciphertext')
        now=int(time.time());ed=now+terms['evidence_seconds'];rd=ed+terms['response_seconds']
        recipients={a:self.key(a) for a in (claimant,respondent)}
        network=self.e._one('SELECT network FROM market_config WHERE id=1')['network']
        cur=self.e.db.execute('INSERT INTO cases(kind,resource_id,claimant,respondent,network,reason,snapshot,recipients,opened_at,evidence_deadline,response_deadline,settle_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (kind,rid,claimant,respondent,network,reason,json.dumps(snapshot),json.dumps(recipients),now,ed,rd,max(review_deadline,rd)))
        return cur.lastrowid
    def _row(self,actor,cid):
        if type(cid) is not int or cid<1:raise AEPError('invalid case id')
        c=self.e._one('SELECT * FROM cases WHERE id=?',(cid,))
        if not c or actor not in (c['claimant'],c['respondent']):raise PermissionError('case participant required')
        for k in ('snapshot','recipients','resolution'):
            if c[k] is not None:c[k]=json.loads(c[k])
        return c
    def view(self,actor,cid):
        with self.e._lock:
            c=self._row(actor,cid);now=int(time.time())
            c['phase']='resolved' if c['resolution'] else ('evidence' if now<c['evidence_deadline'] else ('response' if now<c['response_deadline'] else ('waiting_settlement' if now<c['settle_after'] else 'ready')))
            c['entries']=[dict(r) for r in self.e.db.execute('SELECT * FROM case_entries WHERE case_id=? ORDER BY rowid',(cid,))]
            for r in c['entries']:r['envelope']=json.loads(r['envelope'])
            return c
    def list(self,actor):
        with self.e._lock:
            return [{'id':r['id'],'kind':r['kind'],'resource_id':r['resource_id'],'settle_after':r['settle_after'],'resolved':r['resolution'] is not None} for r in self.e.db.execute('SELECT * FROM cases WHERE claimant=? OR respondent=? ORDER BY id DESC LIMIT 100',(actor,actor))]
    def append(self,actor,cid,body):
        with self.e._lock:return self.e._tx(lambda:self._append(actor,cid,body))
    def _append(self,actor,cid,b):
        c=self._row(actor,cid)
        if not isinstance(b,dict) or set(b)!={'entry_id','kind','reply_to','envelope'}:raise AEPError('complete encrypted evidence entry required')
        eid=b['entry_id'];kind=b['kind'];reply=b['reply_to'];env=b['envelope']
        if not isinstance(eid,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}',eid):raise AEPError('entry_id must be 16-80 safe characters')
        old=self.e._one('SELECT * FROM case_entries WHERE case_id=? AND entry_id=?',(cid,eid))
        digest=hashlib.sha256(canonical(b)).hexdigest()
        if old:
            if old['author']!=actor or old['digest']!=digest:raise IdempotencyConflict('entry id already used with another payload')
            return {'case_id':cid,'entry_id':eid,'record_hash':old['record_hash']}
        now=int(time.time())
        if c['resolution']:raise AEPError('case closed')
        if kind=='evidence':
            if now>=c['evidence_deadline'] or reply is not None:raise AEPError('evidence window closed or unexpected reply_to')
        elif kind=='response':
            if not c['evidence_deadline']<=now<c['response_deadline']:raise AEPError('response window not open')
            if not isinstance(reply,str):raise AEPError('response requires an evidence entry id')
            target=self.e._one('SELECT * FROM case_entries WHERE case_id=? AND entry_id=?',(cid,reply))
            if not target or target['kind']!='evidence' or target['author']==actor:raise AEPError('respond to counterparty evidence in this case')
        else:raise AEPError('invalid entry kind')
        count=self.e.db.execute('SELECT COUNT(*) FROM case_entries WHERE case_id=? AND author=?',(cid,actor)).fetchone()[0]
        if count>=20:raise AEPError('20 entries per participant per case maximum')
        expected=context(c,actor,eid,kind,reply)
        if not isinstance(env,dict) or set(env)!={'context','nonce','ciphertext','wrapped_keys'} or env['context']!=expected:raise AEPError('evidence context mismatch')
        if not isinstance(env['wrapped_keys'],dict) or set(env['wrapped_keys'])!=set(c['recipients']):raise AEPError('wrap for both case participants')
        try:
            if len(unb64(env['nonce']))!=12 or not 17<=len(unb64(env['ciphertext']))<=6016 or any(len(unb64(v))!=384 for v in env['wrapped_keys'].values()):raise ValueError()
        except (ValueError,TypeError):raise AEPError('invalid evidence encryption envelope')
        prev=self.e._one('SELECT record_hash FROM case_entries WHERE case_id=? ORDER BY rowid DESC LIMIT 1',(cid,))
        previous=prev['record_hash'] if prev else '0'*64
        record={'case_id':cid,'entry_id':eid,'author':actor,'kind':kind,'reply_to':reply,'digest':digest,'created_at':now,'previous_hash':previous}
        rh=hashlib.sha256(canonical(record)).hexdigest()
        self.e.db.execute('INSERT INTO case_entries VALUES (?,?,?,?,?,?,?,?,?,?)',(cid,eid,actor,kind,reply,json.dumps(env),now,digest,previous,rh))
        return {'case_id':cid,'entry_id':eid,'record_hash':rh}
    def require_due(self,kind,rid):
        c=self.e._one('SELECT * FROM cases WHERE kind=? AND resource_id=?',(kind,rid))
        if not c or c['resolution'] or int(time.time())<c['settle_after']:raise AEPError('case is not ready for settlement')
    def resolve(self,kind,rid,resolution):
        self.e.db.execute('UPDATE cases SET resolution=? WHERE kind=? AND resource_id=?',(json.dumps(resolution),kind,rid))
