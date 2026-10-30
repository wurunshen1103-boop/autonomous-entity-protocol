"""AEP v0.5 simulation engine. NOT a blockchain or real-money wallet."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
import threading
from functools import wraps

SCHEMA = '''
CREATE TABLE IF NOT EXISTS resolutions (event_id INTEGER PRIMARY KEY, evidence TEXT NOT NULL, resolved_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency (actor TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(actor, request_key));
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, task_id INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL, balance INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, creator TEXT NOT NULL, worker TEXT, title TEXT NOT NULL, description TEXT NOT NULL, reward INTEGER NOT NULL, status TEXT NOT NULL, submission TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, receiver TEXT, amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, close_at INTEGER NOT NULL, outcome INTEGER, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, agent_id TEXT NOT NULL, probability REAL NOT NULL, rationale TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(event_id, agent_id));
'''
class AEPError(ValueError): pass
class IdempotencyConflict(AEPError): pass

def serialized(fn):
    """One shared SQLite connection must not interleave request transactions."""
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapped


class Engine:
    def __init__(self, path=':memory:'):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('PRAGMA foreign_keys=ON; '+SCHEMA)

    def _one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _tx(self, fn):
        # Task methods may run inside the idempotent request's outer transaction.
        # A savepoint never commits that outer transaction.
        if self.db.in_transaction:
            name='nested_'+secrets.token_hex(8)
            self.db.execute('SAVEPOINT '+name)
            try:
                result=fn()
                self.db.execute('RELEASE SAVEPOINT '+name)
                return result
            except Exception:
                self.db.execute('ROLLBACK TO SAVEPOINT '+name)
                self.db.execute('RELEASE SAVEPOINT '+name)
                raise
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = fn()
            self.db.execute('COMMIT')
            return result
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    @serialized
    def task_request(self, actor, action, data, task_id=None, request_key=None):
        """Replay successful task requests atomically, scoped to an authenticated actor.

        Keys have no expiry. Replays return the original response, not current task
        state. Failed requests do not reserve keys. Direct task methods remain
        available for local operators and do not provide replay protection.
        """
        self.profile(actor)
        if not isinstance(data,dict): raise AEPError('JSON object required')
        if action=='create' and task_id is None:
            call=lambda:self.create_task(actor,data.get('title'),data.get('description'),data.get('reward'))
        elif action in ('accept','submit','approve','cancel','release','revise') and type(task_id) is int and task_id>0:
            if action=='submit': call=lambda:self.submit(actor,task_id,data.get('submission'))
            else: call=lambda:getattr(self,action)(actor,task_id)
        else:
            raise AEPError('invalid task operation')
        if request_key is None:
            return call()
        if not isinstance(request_key,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',request_key):
            raise AEPError('Idempotency-Key must be 1-128 ASCII letters, digits, dot, underscore, colon or hyphen')
        try:
            canonical=json.dumps([action,task_id,data],sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
        except (ValueError,TypeError):
            raise AEPError('request must contain valid finite JSON values')
        digest=hashlib.sha256(canonical.encode()).hexdigest()
        def work():
            previous=self._one('SELECT request_hash,response_json FROM idempotency WHERE actor=? AND request_key=?',(actor,request_key))
            if previous:
                if previous['request_hash']!=digest:
                    raise IdempotencyConflict('Idempotency-Key already used for a different task request')
                return json.loads(previous['response_json'])
            result=call()
            self.db.execute('INSERT INTO idempotency VALUES (?,?,?,?,?)',
                            (actor,request_key,digest,json.dumps(result,allow_nan=False),int(time.time())))
            return result
        return self._tx(work)

    @serialized
    def register(self, name):
        if not isinstance(name,str) or not 2 <= len(name.strip()) <= 40: raise AEPError('name must be 2-40 characters')
        agent_id = 'agt_'+secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        def work():
            self.db.execute('INSERT INTO agents VALUES (?,?,?,?,?)',(agent_id,name.strip(),hashlib.sha256(token.encode()).hexdigest(),100,int(time.time())))
            self.db.execute('INSERT INTO ledger(sender,receiver,amount,reason,created_at) VALUES (?,?,?,?,?)',(None,agent_id,100,'demo genesis grant',int(time.time())))
            return {'agent_id':agent_id,'api_token':token,'demo_balance':100}
        return self._tx(work)

    @serialized
    def authenticate(self, token):
        if not token: raise PermissionError('missing token')
        digest=hashlib.sha256(token.encode()).hexdigest()
        row=self._one('SELECT id FROM agents WHERE token_hash=?',(digest,))
        if not row: raise PermissionError('invalid token')
        return row['id']

    @serialized
    def profile(self, agent_id):
        row=self._one('SELECT id,name,balance,created_at FROM agents WHERE id=?',(agent_id,))
        if not row: raise AEPError('unknown agent')
        return row

    @serialized
    def post(self, actor, body):
        self.profile(actor)
        if not isinstance(body,str) or not 1<=len(body.strip())<=2000: raise AEPError('post length must be 1-2000')
        cur=self.db.execute('INSERT INTO posts(agent_id,body,created_at) VALUES (?,?,?)',(actor,body.strip(),int(time.time())))
        return {'post_id':cur.lastrowid}

    @serialized
    def feed(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM posts ORDER BY id DESC LIMIT 100')]

    @serialized
    def create_task(self, actor, title, description, reward):
        self.profile(actor)
        if not isinstance(title,str) or not 1<=len(title.strip())<=120: raise AEPError('invalid title')
        if not isinstance(description,str) or not 1<=len(description.strip())<=4000: raise AEPError('invalid description')
        if type(reward) is not int or reward<=0 or reward>1000000: raise AEPError('invalid reward')
        def work():
            bal=self.profile(actor)['balance']
            if bal<reward: raise AEPError('insufficient demo credits')
            self.db.execute('UPDATE agents SET balance=balance-? WHERE id=?',(reward,actor))
            cur=self.db.execute('INSERT INTO tasks(creator,title,description,reward,status,created_at) VALUES (?,?,?,?,?,?)',(actor,title.strip(),description.strip(),reward,'open',int(time.time())))
            self.db.execute('INSERT INTO ledger(sender,receiver,amount,reason,created_at) VALUES (?,?,?,?,?)',(actor,None,reward,f'task:{cur.lastrowid}:escrow',int(time.time())))
            self._audit(actor, 'created', cur.lastrowid)
            return {'task_id':cur.lastrowid,'status':'open'}
        return self._tx(work)

    @serialized
    def tasks(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM tasks ORDER BY id DESC LIMIT 100')]

    @serialized
    def accept(self, actor, task_id):
        self.profile(actor)
        def work():
            t=self._one('SELECT * FROM tasks WHERE id=?',(task_id,))
            if not t or t['status']!='open': raise AEPError('task not open')
            if t['creator']==actor: raise AEPError('cannot accept own task')
            self.db.execute("UPDATE tasks SET worker=?,status='accepted' WHERE id=?",(actor,task_id))
            self._audit(actor, 'accepted', task_id)
            return {'task_id':task_id,'status':'accepted'}
        return self._tx(work)

    @serialized
    def submit(self, actor, task_id, submission):
        self.profile(actor)
        if not isinstance(submission,str) or not 1<=len(submission.strip())<=10000: raise AEPError('invalid submission')
        def work():
            t=self._one('SELECT * FROM tasks WHERE id=?',(task_id,))
            if not t or t['worker']!=actor or t['status']!='accepted': raise AEPError('not assigned worker or wrong status')
            self.db.execute("UPDATE tasks SET status='submitted',submission=? WHERE id=?",(submission.strip(),task_id))
            self._audit(actor, 'submitted', task_id)
            return {'task_id':task_id,'status':'submitted'}
        return self._tx(work)

    @serialized
    def approve(self, actor, task_id):
        self.profile(actor)
        def work():
            t=self._one('SELECT * FROM tasks WHERE id=?',(task_id,))
            if not t or t['creator']!=actor or t['status']!='submitted': raise AEPError('only creator can approve submitted task')
            self.db.execute("UPDATE tasks SET status='completed' WHERE id=?",(task_id,))
            self.db.execute('UPDATE agents SET balance=balance+? WHERE id=?',(t['reward'],t['worker']))
            self.db.execute('INSERT INTO ledger(sender,receiver,amount,reason,created_at) VALUES (?,?,?,?,?)',(None,t['worker'],t['reward'],f'task:{task_id}:payout',int(time.time())))
            self._audit(actor, 'completed', task_id)
            return {'task_id':task_id,'status':'completed'}
        return self._tx(work)

    @serialized
    def create_event(self, question, close_at):
        if not isinstance(question,str) or not 5<=len(question.strip())<=500: raise AEPError('invalid question')
        if type(close_at) is not int or close_at<=int(time.time()): raise AEPError('close_at must be a future unix timestamp')
        cur=self.db.execute('INSERT INTO events(question,close_at,created_at) VALUES (?,?,?)',(question.strip(),close_at,int(time.time())))
        return {'event_id':cur.lastrowid}

    @serialized
    def predict(self, actor, event_id, probability, rationale):
        self.profile(actor)
        if type(probability) not in (int,float) or not 0<=probability<=1: raise AEPError('probability must be 0..1')
        if not isinstance(rationale,str) or not 1<=len(rationale.strip())<=3000: raise AEPError('invalid rationale')
        e=self._one('SELECT * FROM events WHERE id=?',(event_id,))
        if not e or e['close_at']<=int(time.time()) or e['outcome'] is not None: raise AEPError('event closed')
        try:
            self.db.execute('INSERT INTO predictions(event_id,agent_id,probability,rationale,created_at) VALUES (?,?,?,?,?)',(event_id,actor,probability,rationale.strip(),int(time.time())))
        except sqlite3.IntegrityError: raise AEPError('already predicted')
        return {'event_id':event_id,'agent_id':actor,'probability':probability}

    @serialized
    def events(self):
        return [dict(r) for r in self.db.execute('SELECT e.*,r.evidence,r.resolved_at FROM events e LEFT JOIN resolutions r ON r.event_id=e.id ORDER BY e.id DESC LIMIT 100')]

    def _audit(self, actor, action, task_id):
        self.db.execute('INSERT INTO audit(actor,action,task_id,created_at) VALUES (?,?,?,?)',
                        (actor,action,task_id,int(time.time())))

    @serialized
    def cancel(self, actor, task_id):
        """Only an unassigned open task may be cancelled and refunded."""
        self.profile(actor)
        def work():
            t=self._one('SELECT * FROM tasks WHERE id=?',(task_id,))
            if not t or t['creator']!=actor or t['status']!='open':
                raise AEPError('only creator can cancel an open task')
            self.db.execute("UPDATE tasks SET status='cancelled' WHERE id=?",(task_id,))
            self.db.execute('UPDATE agents SET balance=balance+? WHERE id=?',(t['reward'],actor))
            self.db.execute('INSERT INTO ledger(sender,receiver,amount,reason,created_at) VALUES (?,?,?,?,?)',
                            (None,actor,t['reward'],f'task:{task_id}:refund',int(time.time())))
            self._audit(actor,'cancelled',task_id)
            return {'task_id':task_id,'status':'cancelled'}
        return self._tx(work)

    @serialized
    def release(self, actor, task_id):
        """Assigned worker may release unfinished work; escrow remains held."""
        self.profile(actor)
        def work():
            t=self._one('SELECT * FROM tasks WHERE id=?',(task_id,))
            if not t or t['worker']!=actor or t['status']!='accepted':
                raise AEPError('only assigned worker can release an accepted task')
            self.db.execute("UPDATE tasks SET status='open',worker=NULL,submission=NULL WHERE id=?",(task_id,))
            self._audit(actor,'released',task_id)
            return {'task_id':task_id,'status':'open'}
        return self._tx(work)

    @serialized
    def revise(self, actor, task_id):
        """Creator requests another submission without moving escrow."""
        self.profile(actor)
        def work():
            t=self._one('SELECT * FROM tasks WHERE id=?',(task_id,))
            if not t or t['creator']!=actor or t['status']!='submitted':
                raise AEPError('only creator can revise a submitted task')
            self.db.execute("UPDATE tasks SET status='accepted' WHERE id=?",(task_id,))
            self._audit(actor,'revision_requested',task_id)
            return {'task_id':task_id,'status':'accepted'}
        return self._tx(work)

    @serialized
    def task_audit(self, actor, task_id):
        self.profile(actor)
        t=self._one('SELECT * FROM tasks WHERE id=?',(task_id,))
        if not t or actor not in (t['creator'],t['worker']):
            raise AEPError('only task participants can read audit records')
        return [dict(r) for r in self.db.execute('SELECT * FROM audit WHERE task_id=? ORDER BY id',(task_id,))]

    @serialized
    def close(self):
        self.db.close()

    @serialized
    def resolve_event(self,event_id,outcome,evidence):
        if type(outcome) is not int or outcome not in (0,1): raise AEPError('outcome must be 0 or 1')
        if not isinstance(evidence,str) or not 1<=len(evidence.strip())<=4000: raise AEPError('resolution evidence required')
        def work():
            event=self._one('SELECT * FROM events WHERE id=?',(event_id,))
            if not event or event['close_at']>int(time.time()): raise AEPError('event not closed yet')
            if event['outcome'] is not None: raise AEPError('event already resolved')
            self.db.execute('UPDATE events SET outcome=? WHERE id=?',(outcome,event_id))
            self.db.execute('INSERT INTO resolutions VALUES (?,?,?)',(event_id,evidence.strip(),int(time.time())))
            return {'event_id':event_id,'outcome':outcome,'evidence':evidence.strip()}
        return self._tx(work)

    @serialized
    def scores(self):
        return [dict(r) for r in self.db.execute('SELECT p.agent_id,COUNT(*) AS resolved_predictions,AVG((p.probability-e.outcome)*(p.probability-e.outcome)) AS brier_loss FROM predictions p JOIN events e ON p.event_id=e.id WHERE e.outcome IS NOT NULL GROUP BY p.agent_id ORDER BY brier_loss,p.agent_id')]
