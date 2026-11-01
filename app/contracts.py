"""Opt-in, deterministic task settlement. Schema validation is NOT quality arbitration."""
import json
import time
from .core import AEPError

SCHEMA='''CREATE TABLE IF NOT EXISTS task_contracts (
 task_id INTEGER PRIMARY KEY, terms TEXT NOT NULL, accept_deadline INTEGER NOT NULL,
 work_deadline INTEGER, review_deadline INTEGER, dispute TEXT, resolution TEXT);
'''

def number(value,low,high):
    if type(value) is not int or not low<=value<=high: raise AEPError('integer out of range')
    return value

def task_terms(value):
    if not isinstance(value,dict):raise AEPError('complete task contract required')
    extra={'evidence_seconds','response_seconds'} if value.get('rule') in ('evidence-v1','failure-v1') else set()
    if set(value)!=({'rule','required_fields','accept_seconds','work_seconds','review_seconds'}|extra):
        raise AEPError('complete task contract required')
    if value['rule'] not in ('fixed-v1','evidence-v1','failure-v1'): raise AEPError('unknown settlement rule')
    fields=value['required_fields']
    if not isinstance(fields,list) or not 1<=len(fields)<=10 or any(not isinstance(x,str) or not 1<=len(x)<=40 for x in fields) or len(set(fields))!=len(fields):
        raise AEPError('required_fields must contain 1-10 unique strings')
    for key in ('accept_seconds','work_seconds','review_seconds',*extra):number(value[key],60,604800)
    return value

class Contracts:
    def __init__(self,engine):
        self.e=engine;engine.db.executescript(SCHEMA)
        from .cases import Cases
        self.cases=Cases(engine)
        engine.db.execute('CREATE TABLE IF NOT EXISTS task_partial_offers (task_id INTEGER PRIMARY KEY, worker_amount INTEGER NOT NULL, created_at INTEGER NOT NULL)')
    def get(self,tid):return self.e._one('SELECT * FROM task_contracts WHERE task_id=?',(tid,))
    def view(self,tid):
        row=self.get(tid)
        if row:
            row['terms']=json.loads(row['terms'])
            row['partial_offer']=self.e._one('SELECT worker_amount,created_at FROM task_partial_offers WHERE task_id=?',(tid,))
        return row
    def request(self,actor,action,data,tid=None):
        e=self.e;now=int(time.time())
        if action=='create':
            terms=task_terms(data['contract'])
            if terms['rule'] in ('evidence-v1','failure-v1'):self.cases.key(actor)
            result=e.create_task(actor,data.get('title'),data.get('description'),data.get('reward'))
            e.db.execute('INSERT INTO task_contracts(task_id,terms,accept_deadline) VALUES (?,?,?)',
                         (result['task_id'],json.dumps(terms),now+terms['accept_seconds']))
            return result|{'contract':self.view(result['task_id'])}
        t=e._one('SELECT * FROM tasks WHERE id=?',(tid,));c=self.get(tid)
        if not t or not c:raise AEPError('unknown contracted task')
        terms=json.loads(c['terms'])
        if action=='accept':
            if data.get('expected_contract')!=terms:raise AEPError('explicit acceptance of exact contract required')
            if now>=c['accept_deadline']:raise AEPError('acceptance expired')
            if terms['rule'] in ('evidence-v1','failure-v1'):self.cases.key(actor)
            result=e.accept(actor,tid)
            e.db.execute('UPDATE task_contracts SET work_deadline=? WHERE task_id=?',(now+terms['work_seconds'],tid))
            return result
        if actor not in (t['creator'],t['worker']):raise PermissionError('task participant required')
        if action in ('fail','offer_partial','accept_partial'):
            if terms['rule']!='failure-v1':raise AEPError('failure-v1 contract required; old terms unchanged')
            if action=='fail':
                if actor!=t['worker'] or t['status'] not in ('accepted','submitted','disputed'):raise AEPError('only assigned worker can acknowledge failure before settlement')
                return self._finish(t,0,'worker_acknowledged_failure')
            if action=='offer_partial':
                if actor!=t['worker'] or t['status'] not in ('submitted','disputed'):raise AEPError('worker may offer partial settlement after submission')
                amount=number(data.get('worker_amount'),0,t['reward']//5)
                old=e._one('SELECT worker_amount FROM task_partial_offers WHERE task_id=?',(tid,))
                if old and old['worker_amount']!=amount:raise AEPError('partial offer immutable; cannot replace a pending agreement')
                if not old:
                    e.db.execute('INSERT INTO task_partial_offers VALUES (?,?,?)',(tid,amount,now))
                    e._audit(actor,'partial_offered',tid)
                return {'task_id':tid,'worker_amount':amount,'refund':t['reward']-amount,'requires':'creator_acceptance'}
            offer=e._one('SELECT worker_amount FROM task_partial_offers WHERE task_id=?',(tid,))
            if actor!=t['creator'] or t['status'] not in ('submitted','disputed') or not offer:raise AEPError('creator may accept an existing partial offer only')
            amount=number(data.get('expected_worker_amount'),0,t['reward']//5)
            if amount!=offer['worker_amount']:raise AEPError('explicit acceptance of exact partial amount required')
            return self._finish(t,amount,'mutually_agreed_partial')
        if action=='submit':
            if t['status']!='accepted' or now>=c['work_deadline']:raise AEPError('submission deadline passed or wrong state')
            try: payload=json.loads(data.get('submission',''))
            except (ValueError,TypeError):raise AEPError('submission must be a JSON object encoded as a string')
            if not isinstance(payload,dict) or any(not isinstance(payload.get(f),str) or not payload[f].strip() for f in terms['required_fields']):raise AEPError('required nonempty string fields missing')
            result=e.submit(actor,tid,data['submission'])
            e.db.execute('UPDATE task_contracts SET review_deadline=? WHERE task_id=?',(now+terms['review_seconds'],tid))
            return result
        if action=='approve':
            if actor!=t['creator'] or t['status']!='submitted' or c['dispute']:raise AEPError('creator may approve undisputed submission only')
            return self._finish(t,t['reward'],'accepted')
        if action=='dispute':
            if actor!=t['creator'] or t['status']!='submitted' or now>=c['review_deadline']:raise AEPError('creator may dispute only within review period')
            reason=data.get('reason')
            if not isinstance(reason,str) or not 1<=len(reason)<=1000:raise AEPError('public dispute reason required')
            case_id=None
            if terms['rule'] in ('evidence-v1','failure-v1'):
                import hashlib
                case_id=self.cases.open('task',tid,t['creator'],t['worker'],reason,terms,c['review_deadline'],{'amount':t['reward'],'terms':terms,'submission_sha256':hashlib.sha256(t['submission'].encode()).hexdigest(),'work_deadline':c['work_deadline'],'review_deadline':c['review_deadline']})
            e.db.execute("UPDATE tasks SET status='disputed' WHERE id=?",(tid,))
            e.db.execute('UPDATE task_contracts SET dispute=? WHERE task_id=?',(reason,tid));e._audit(actor,'disputed',tid)
            return {'task_id':tid,'status':'disputed'}|({'case_id':case_id} if case_id else {})
        if action=='cancel' and t['status']=='open' and actor==t['creator']:return self._finish(t,0,'creator_cancelled')
        if action=='release' and t['status']=='accepted' and actor==t['worker']:return self._finish(t,0,'worker_released')
        if action=='settle':
            if t['status']=='open' and now>=c['accept_deadline']:return self._finish(t,0,'not_accepted')
            if t['status']=='accepted' and now>=c['work_deadline']:return self._finish(t,0,'not_delivered')
            if t['status'] in ('submitted','disputed') and now>=c['review_deadline']:
                if c['dispute'] and terms['rule'] in ('evidence-v1','failure-v1'):self.cases.require_due('task',tid)
                share=t['reward']//2 if c['dispute'] else t['reward']
                return self._finish(t,share,'dispute_split' if c['dispute'] else 'review_expired')
            raise AEPError('no settlement due')
        raise AEPError('action unavailable under this contract')
    def _finish(self,t,worker_amount,reason):
        e=self.e;refund=t['reward']-worker_amount
        if t['status'] not in ('open','accepted','submitted','disputed'):raise AEPError('task already settled')
        for recipient,amount,suffix in ((t['worker'],worker_amount,'payout'),(t['creator'],refund,'refund')):
            if amount:
                e.db.execute('UPDATE agents SET balance=balance+? WHERE id=?',(amount,recipient))
                e.db.execute('INSERT INTO ledger(sender,receiver,amount,reason,created_at) VALUES (?,?,?,?,?)',(None,recipient,amount,f"task:{t['id']}:{suffix}",int(time.time())))
        status='completed' if worker_amount==t['reward'] else ('resolved' if worker_amount else 'cancelled')
        resolution={'rule':json.loads(self.get(t['id'])['terms'])['rule'],'reason':reason,'worker_amount':worker_amount,'refund':refund}
        e.db.execute('UPDATE tasks SET status=? WHERE id=?',(status,t['id']))
        e.db.execute('UPDATE task_contracts SET resolution=? WHERE task_id=?',(json.dumps(resolution),t['id']))
        self.cases.resolve('task',t['id'],resolution)
        e._audit(t['creator'],'settled:'+reason,t['id'])
        return {'task_id':t['id'],'status':status,**resolution}
