import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from app.core import Engine,AEPError
from app.server import make_server
from app.client import Client
from app.runtime import Worker,DemoExecutor

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.e=Engine();self.s=make_server(self.e,0)
        self.t=threading.Thread(target=self.s.serve_forever,daemon=True);self.t.start()
        self.url='http://127.0.0.1:'+str(self.s.server_port)
        self.a=self.e.register('Creator');self.b=self.e.register('Worker')
        self.client=Client(self.url,self.b['api_token'])
        self.temp=tempfile.TemporaryDirectory();self.state=os.path.join(self.temp.name,'worker.sqlite3')
    def tearDown(self):
        self.s.shutdown();self.t.join();self.s.server_close();self.e.close();self.temp.cleanup()
    def task(self):return self.e.create_task(self.a['agent_id'],'Summarize','Public data',15)['task_id']
    def test_discovery_worker_submission_restart_and_settlement(self):
        self.assertEqual(self.client.request('GET','/.well-known/aep.json')['version'],'0.9.0')
        tid=self.task();w=Worker(self.client,self.state)
        self.assertEqual(w.run()[0]['phase'],'submitted');w.close()
        w=Worker(self.client,self.state);self.assertEqual(len(w.run()),1);w.close()
        self.assertEqual(self.e.tasks()[0]['status'],'submitted')
        self.assertIn('SIMULATED',self.e.tasks()[0]['submission'])
        self.e.approve(self.a['agent_id'],tid)
        self.assertEqual(self.e.profile(self.b['agent_id'])['balance'],115)
    def test_budget_and_job_limits(self):
        class Paid(DemoExecutor):max_cost_units=5
        self.task();self.task()
        w=Worker(self.client,self.state,Paid(),max_jobs=2,budget_units=4)
        self.assertEqual(w.run(),[]);w.close()
        w=Worker(self.client,self.state,Paid(),max_jobs=2,budget_units=5)
        self.assertEqual(len(w.run()),1);self.assertEqual(len(w.run()),1);w.close()
    def test_uncertain_execution_never_reruns(self):
        class Failing(DemoExecutor):
            def execute(self,task):raise RuntimeError('external failure')
        self.task();w=Worker(self.client,self.state,Failing())
        with self.assertRaises(RuntimeError):w.run()
        self.assertEqual(w.logs()[0]['phase'],'executing');w.close()
        w=Worker(self.client,self.state);self.assertEqual(w.run()[0]['phase'],'executing');w.close()
        self.assertEqual(self.e.tasks()[0]['status'],'accepted')
    def test_ready_submission_resumes_without_execution(self):
        tid=self.task();w=Worker(self.client,self.state)
        self.e.accept(self.b['agent_id'],tid)
        w.db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(tid,'ready','Saved output',0,'demo'));w.db.commit()
        self.assertEqual(w.run()[0]['phase'],'submitted');w.close()
        self.assertEqual(self.e.tasks()[0]['submission'],'Saved output')
    def test_prediction_closing_resolution_and_score(self):
        eid=self.e.create_event('Will experiment succeed?',int(time.time())+60)['event_id']
        self.e.predict(self.b['agent_id'],eid,.8,'Experiment plan')
        with self.assertRaises(AEPError):self.e.resolve_event(eid,1,'Too early')
        with patch('app.core.time.time',return_value=time.time()+120):
            with self.assertRaises(AEPError):self.e.predict(self.a['agent_id'],eid,.5,'Late')
            self.e.resolve_event(eid,1,'Verified experiment log')
            with self.assertRaises(AEPError):self.e.resolve_event(eid,0,'Rewrite')
        score=self.client.request('GET','/scores')[0]
        self.assertAlmostEqual(score['brier_loss'],.04)
        self.assertEqual(score['resolved_predictions'],1)
    def test_client_rejects_insecure_remote_transport(self):
        with self.assertRaises(ValueError):Client('http://example.com')
    def test_worker_exclusive_state_and_identity_binding(self):
        w=Worker(self.client,self.state)
        with self.assertRaises(FileExistsError):Worker(self.client,self.state)
        w.close()
        with self.assertRaises(ValueError):Worker(Client(self.url,self.a['api_token']),self.state)
        w=Worker(self.client,self.state);w.close()
