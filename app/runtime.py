"""Bounded one-pass worker with durable checkpoints and explicit executor adapter."""
import argparse
import json
import os
import sqlite3
from .client import Client

class DemoExecutor:
    name='deterministic-demo'
    max_cost_units=0
    def execute(self,task):
        return 'SIMULATED RESULT: '+task['title']+'\nInput: '+task['description']+'\nNo model inference or evidence verification was performed.'

class Worker:
    def __init__(self,client,state_path,executor=None,max_jobs=1,budget_units=0):
        self.lock_path=os.path.realpath(state_path)+'.lock'
        self.lock_fd=os.open(self.lock_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.write(self.lock_fd,str(os.getpid()).encode())
        try:
            self._initialize(client,state_path,executor,max_jobs,budget_units)
            os.chmod(state_path,0o600)
        except Exception:
            if hasattr(self,'db'): self.db.close()
            os.close(self.lock_fd);os.unlink(self.lock_path)
            raise

    def _initialize(self,client,state_path,executor=None,max_jobs=1,budget_units=0):
        if type(max_jobs) is not int or max_jobs<1 or type(budget_units) is not int or budget_units<0:
            raise ValueError('invalid job or budget limit')
        self.client=client; self.executor=executor or DemoExecutor()
        cost=self.executor.max_cost_units
        if type(cost) is not int or cost<0: raise ValueError('executor must declare nonnegative integer maximum cost units')
        self.max_jobs=max_jobs; self.budget=budget_units
        self.db=sqlite3.connect(state_path)
        self.db.execute('CREATE TABLE IF NOT EXISTS runs (task_id INTEGER PRIMARY KEY, phase TEXT NOT NULL, result TEXT, cost INTEGER NOT NULL, executor TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS owner (identity TEXT PRIMARY KEY)')
        identity=client.base+'|'+client.request('GET','/me')['id']
        owners=self.db.execute('SELECT identity FROM owner').fetchall()
        if owners and owners!=[(identity,)]: raise ValueError('state file belongs to another agent or server')
        self.db.execute('INSERT OR IGNORE INTO owner VALUES (?)',(identity,));self.db.commit()

    def run(self):
        # Resume persisted submissions first; an uncertain external execution is never rerun automatically.
        for tid,result in self.db.execute("SELECT task_id,result FROM runs WHERE phase='ready'").fetchall():
            self.client.task('submit',{'submission':result},f'worker-{tid}-submit',tid)
            self.db.execute("UPDATE runs SET phase='submitted' WHERE task_id=?",(tid,));self.db.commit()
        count,spent=self.db.execute('SELECT COUNT(*),COALESCE(SUM(cost),0) FROM runs').fetchone()
        actor=self.client.request('GET','/me')['id']
        for task in self.client.request('GET','/tasks'):
            if count>=self.max_jobs or spent+self.executor.max_cost_units>self.budget: break
            tid=task['id']
            if task['status']!='open' or task['creator']==actor or self.db.execute('SELECT 1 FROM runs WHERE task_id=?',(tid,)).fetchone(): continue
            # Reserve budget before claiming/executing; failures conservatively consume reservation.
            cost=self.executor.max_cost_units
            self.db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(tid,'reserved',None,cost,self.executor.name));self.db.commit()
            count+=1;spent+=cost
            try:
                self.client.task('accept',{},f'worker-{tid}-accept',tid)
                self.db.execute("UPDATE runs SET phase='executing' WHERE task_id=?",(tid,));self.db.commit()
                result=self.executor.execute(task)
                if not isinstance(result,str) or not 1<=len(result.strip())<=10000: raise ValueError('invalid executor output')
                self.db.execute("UPDATE runs SET phase='ready',result=? WHERE task_id=?",(result,tid));self.db.commit()
                self.client.task('submit',{'submission':result},f'worker-{tid}-submit',tid)
                self.db.execute("UPDATE runs SET phase='submitted' WHERE task_id=?",(tid,));self.db.commit()
            except Exception:
                # Keep phase: ready can be retried; reserved/executing require operator reconciliation.
                raise
        return self.logs()

    def logs(self):
        return [dict(zip(('task_id','phase','reserved_cost_units','executor'),r)) for r in self.db.execute('SELECT task_id,phase,cost,executor FROM runs ORDER BY task_id')]

    def close(self):
        self.db.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd);os.unlink(self.lock_path);self.lock_fd=None

def main():
    p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8765');p.add_argument('--state',default='worker.sqlite3');p.add_argument('--max-jobs',type=int,default=1)
    args=p.parse_args()
    token=os.environ.get('AEP_AGENT_TOKEN')
    if not token: p.error('set AEP_AGENT_TOKEN')
    worker=Worker(Client(args.url,token),args.state,max_jobs=args.max_jobs)
    try: print(json.dumps(worker.run(),ensure_ascii=False))
    finally: worker.close()
if __name__=='__main__': main()
