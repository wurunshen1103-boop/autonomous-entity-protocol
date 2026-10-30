import unittest
import time
from app.core import Engine, AEPError

class TestAEP(unittest.TestCase):
    def setUp(self):
        self.e=Engine()
        self.a=self.e.register('Alpha')
        self.b=self.e.register('Beta')
    def tearDown(self):
        self.e.close()
    def test_auth(self):
        self.assertEqual(self.e.authenticate(self.a['api_token']),self.a['agent_id'])
        with self.assertRaises(PermissionError): self.e.authenticate('bad')
    def test_task_escrow_and_permissions(self):
        a,b=self.a['agent_id'],self.b['agent_id']
        t=self.e.create_task(a,'Analyze data','Check a public dataset',15)['task_id']
        self.assertEqual(self.e.profile(a)['balance'],85)
        with self.assertRaises(AEPError): self.e.accept(a,t)
        self.e.accept(b,t)
        self.e.submit(b,t,'Completed analysis')
        with self.assertRaises(AEPError): self.e.approve(b,t)
        self.e.approve(a,t)
        self.assertEqual(self.e.profile(b)['balance'],115)
        with self.assertRaises(AEPError): self.e.approve(a,t)
    def test_insufficient_balance(self):
        with self.assertRaises(AEPError): self.e.create_task(self.a['agent_id'],'Test','Details',101)
    def test_social_and_predictions(self):
        a=self.a['agent_id']
        self.e.post(a,'Hello AI society')
        self.assertEqual(len(self.e.feed()),1)
        event=self.e.create_event('Will the test pass?',int(time.time())+3600)['event_id']
        self.e.predict(a,event,.7,'Based on available tests')
        with self.assertRaises(AEPError): self.e.predict(a,event,.6,'Duplicate')

if __name__=='__main__': unittest.main()
