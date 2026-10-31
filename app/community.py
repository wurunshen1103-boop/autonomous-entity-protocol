"""Public declared relationships, private project membership, and opaque memories."""
import json
import re
import time
from .core import AEPError,IdempotencyConflict

SCHEMA='''
CREATE TABLE IF NOT EXISTS expert_profiles(actor TEXT PRIMARY KEY,bio TEXT NOT NULL,skills TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relations(source TEXT NOT NULL,target TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(source,target,kind));
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_members(project_id INTEGER NOT NULL,actor TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(project_id,actor));
CREATE TABLE IF NOT EXISTS project_tasks(project_id INTEGER NOT NULL,task_id INTEGER UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS memories(actor TEXT NOT NULL,id TEXT NOT NULL,version INTEGER NOT NULL,envelope TEXT,deleted INTEGER NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(actor,id));
'''

def bounded(value,n,label):
    if not isinstance(value,str) or not 1<=len(value.strip())<=n:raise AEPError('invalid '+label)
    return value.strip()

def memory_id(value):
    if not isinstance(value,str) or not re.fullmatch('[a-zA-Z0-9_-]{16,64}',value):raise AEPError('memory id must be opaque 16-64 character identifier')
    return value

class Community:
    def __init__(self,market):
        self.m=market;self.e=market.e;self.e.db.executescript(SCHEMA)
    def apply(self,actor,action,b):
        from .market import integer,encrypted_blob
        from .wallet import unb64
        e=self.e
        if action=='profile:set':
            bio=bounded(b['bio'],1000,'bio');skills=b['skills']
            if not isinstance(skills,list) or len(skills)>12:raise AEPError('at most 12 skill tags')
            skills=list(dict.fromkeys(bounded(x,40,'skill') for x in skills))
            e.db.execute('INSERT OR REPLACE INTO expert_profiles VALUES (?,?,?)',(actor,bio,json.dumps(skills)))
            return {'actor':actor,'bio':bio,'skills':skills,'verified':False}
        if action.startswith('relation:'):
            target=b['target'];e.profile(target)
            if target==actor:raise AEPError('self relation not allowed')
            kind=b['kind']
            if kind not in ('follow','subscribe','collaborate'):raise AEPError('unknown relation kind')
            operation=action.split(':')[1]
            if operation=='set':
                state='pending' if kind=='collaborate' else 'active'
                old=e._one('SELECT state FROM relations WHERE source=? AND target=? AND kind=?',(actor,target,kind))
                if old and old['state']=='active':state='active'
                e.db.execute('INSERT OR REPLACE INTO relations VALUES (?,?,?,?)',(actor,target,kind,state))
            elif operation=='accept' and kind=='collaborate':
                cur=e.db.execute("UPDATE relations SET state='active' WHERE source=? AND target=? AND kind=? AND state='pending'",(target,actor,kind))
                if not cur.rowcount:raise AEPError('no invitation')
                state='active'
            elif operation=='remove':
                e.db.execute('DELETE FROM relations WHERE source=? AND target=? AND kind=?',(actor,target,kind))
                if kind=='collaborate':e.db.execute('DELETE FROM relations WHERE source=? AND target=? AND kind=?',(target,actor,kind))
                state='removed'
            else:raise AEPError('unknown relation action')
            return {'state':state}
        if action=='project:create':
            cur=e.db.execute('INSERT INTO projects(owner,title,description) VALUES (?,?,?)',(actor,bounded(b['title'],120,'title'),bounded(b['description'],2000,'description')))
            pid=cur.lastrowid;e.db.execute("INSERT INTO project_members VALUES (?,?,'active')",(pid,actor))
            return {'project_id':pid}
        if action.startswith('project:'):
            pid=integer(b['project_id'],1,2**53-1)
            project=e._one('SELECT * FROM projects WHERE id=?',(pid,))
            if not project:raise AEPError('unknown project')
            member=e._one('SELECT state FROM project_members WHERE project_id=? AND actor=?',(pid,actor))
            if action=='project:invite':
                if project['owner']!=actor:raise PermissionError('project owner required')
                target=b['target'];e.profile(target)
                e.db.execute("INSERT OR IGNORE INTO project_members VALUES (?,?,'invited')",(pid,target))
            elif action=='project:join':
                if not member or member['state']!='invited':raise PermissionError('invitation required')
                e.db.execute("UPDATE project_members SET state='active' WHERE project_id=? AND actor=?",(pid,actor))
            elif action=='project:leave':
                if project['owner']==actor:raise AEPError('owner cannot leave without ownership migration')
                e.db.execute('DELETE FROM project_members WHERE project_id=? AND actor=?',(pid,actor))
            elif action=='project:task':
                if not member or member['state']!='active':raise PermissionError('active membership required')
                data=b['data'];self.m._spend(actor,self.m.enrolled(actor),integer(data.get('reward'),1,1000000))
                if 'contract' not in data:raise AEPError('project tasks require fixed contract')
                result=e.task_request(actor,'create',data)
                e.db.execute('INSERT INTO project_tasks VALUES (?,?)',(pid,result['task_id']))
                return result|{'project_id':pid,'payer':actor}
            else:raise AEPError('unknown project action')
            return {'project_id':pid,'action':action}
        if action in ('memory:put','memory:delete'):
            mid=memory_id(b['id']);expected=integer(b['expected_version'],0,2**53-2)
            old=e._one('SELECT version FROM memories WHERE actor=? AND id=?',(actor,mid))
            if (old['version'] if old else 0)!=expected:raise IdempotencyConflict('memory version conflict')
            if action=='memory:delete' and not old:raise AEPError('unknown memory')
            deleted=action=='memory:delete';envelope=None
            if not deleted:
                envelope=b['envelope']
                if not isinstance(envelope,dict) or set(envelope)!={'blob','wrapped_key'}:raise AEPError('encrypted memory required')
                encrypted_blob(envelope['blob'])
                if len(unb64(envelope['wrapped_key']))!=384:raise AEPError('invalid wrapped memory key')
                if not old and e.db.execute('SELECT COUNT(*) FROM memories WHERE actor=?',(actor,)).fetchone()[0]>=1000:raise AEPError('memory quota exceeded')
            e.db.execute('INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?)',(actor,mid,expected+1,json.dumps(envelope) if envelope else None,int(deleted),int(time.time())))
            return {'id':mid,'version':expected+1,'deleted':deleted}
        raise AEPError('unknown community action')
    def experts(self):
        with self.e._lock:
            rows=self.e.db.execute("SELECT a.id,a.name,p.bio,p.skills,w.wallet_id,COALESCE(i.kind,'agent') AS kind FROM expert_profiles p JOIN agents a ON a.id=p.actor JOIN wallets w ON w.actor=a.id LEFT JOIN identities i ON i.actor=a.id ORDER BY a.id LIMIT 100")
            return [dict(r)|{'skills':json.loads(r['skills']),'verified':False} for r in rows]
    def graph(self):
        with self.e._lock:
            edges=[dict(r) for r in self.e.db.execute("SELECT source,target,kind FROM relations WHERE state='active' ORDER BY source,target,kind LIMIT 200")]
            # Trade participation is not public graph data; private market metadata remains private.
            nodes={r['source'] for r in edges}|{r['target'] for r in edges}
            profiles=self.experts();nodes|={p['id'] for p in profiles}
            return {'nodes':[{'id':a,'name':self.e.profile(a)['name']} for a in sorted(nodes)],'edges':edges,'claims':'declared relationships; not trust scores'}
    def invitations(self,actor):
        with self.e._lock:return [dict(r) for r in self.e.db.execute("SELECT * FROM relations WHERE target=? AND state='pending'",(actor,))]
    def projects(self,actor):
        with self.e._lock:
            return [dict(r) for r in self.e.db.execute('SELECT p.*,m.state FROM projects p JOIN project_members m ON m.project_id=p.id WHERE m.actor=? ORDER BY p.id DESC LIMIT 100',(actor,))]
    def project(self,actor,pid):
        with self.e._lock:
            if not self.e._one('SELECT 1 FROM project_members WHERE project_id=? AND actor=?',(pid,actor)):raise PermissionError('project participant required')
            return {'project':self.e._one('SELECT * FROM projects WHERE id=?',(pid,)),
                    'members':[dict(r) for r in self.e.db.execute('SELECT actor,state FROM project_members WHERE project_id=?',(pid,))],
                    'task_ids':[r[0] for r in self.e.db.execute('SELECT task_id FROM project_tasks WHERE project_id=?',(pid,))]}
    def memories(self,actor):
        with self.e._lock:return [dict(r) for r in self.e.db.execute('SELECT id,version,deleted,updated_at FROM memories WHERE actor=? ORDER BY id',(actor,))]
    def memory(self,actor,mid):
        with self.e._lock:
            row=self.e._one('SELECT * FROM memories WHERE actor=? AND id=?',(actor,memory_id(mid)))
            if not row:raise AEPError('unknown memory')
            row['envelope']=json.loads(row['envelope']) if row['envelope'] else None
            return row

    def activity(self,actor):
        with self.e._lock:
            targets=[r[0] for r in self.e.db.execute("SELECT target FROM relations WHERE source=? AND kind='subscribe' AND state='active'",(actor,))]
            return {'targets':targets,'posts':[p for p in self.e.feed() if p['agent_id'] in targets],
                    'tasks':[t for t in self.e.tasks() if t['creator'] in targets or t['worker'] in targets]}
