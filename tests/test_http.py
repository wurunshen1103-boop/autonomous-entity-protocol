import http.client
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from app.core import Engine
from app.server import make_server


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine=Engine()
        cls.server=make_server(cls.engine,0)
        cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.engine.close()

    def request(self, method, path, data=None, token=None, raw=None, extra_headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        headers={'Content-Type':'application/json'}
        if token: headers['Authorization']='Bearer '+token
        if extra_headers: headers.update(extra_headers)
        body=raw if raw is not None else json.dumps(data or {})
        try:
            conn.request(method,path,body if method=='POST' else None,headers)
            response=conn.getresponse()
            return response.status,json.loads(response.read())
        finally: conn.close()

    def agent(self,name):
        status,data=self.request('POST','/agents',{'name':name})
        self.assertEqual(status,201)
        return data['api_token']

    def test_health_auth_and_bad_paths(self):
        status,data=self.request('GET','/health')
        self.assertEqual((status,data['version']),(200,'0.7.0'))
        self.assertEqual(self.request('GET','/me')[0],401)
        self.assertEqual(self.request('GET','/me',token='wrong')[0],401)
        self.assertEqual(self.request('GET','/missing')[0],404)

    def test_request_validation(self):
        for raw in ('{','[]','null','{"name":'):
            self.assertEqual(self.request('POST','/agents',raw=raw)[0],400)
        self.assertEqual(self.request('POST','/agents',raw=b'\xff')[0],400)
        self.assertEqual(self.request('POST','/agents',raw='x'*20001)[0],413)
        self.assertEqual(self.request('POST','/agents',{'name':'Alpha'},extra_headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('POST','/agents',raw='',extra_headers={'Content-Length':'invalid'})[0],400)

    def test_revision_and_settlement_over_http(self):
        a,b=self.agent('Creator'),self.agent('Worker')
        status,data=self.request('POST','/tasks',{'title':'Test','description':'Deliver','reward':25},a)
        self.assertEqual(status,201)
        t=data['task_id']
        for action,token,data in [('accept',b,{}),('submit',b,{'submission':'Draft'}),('revise',a,{}),('submit',b,{'submission':'Final'}),('approve',a,{})]:
            self.assertEqual(self.request('POST',f'/tasks/{t}/{action}',data,token)[0],201)
        self.assertEqual(self.request('POST',f'/tasks/{t}/approve',{},a)[0],400)
        self.assertEqual(self.request('GET','/me',token=b)[1]['balance'],125)
        status,audit=self.request('GET',f'/tasks/{t}/audit',token=a)
        self.assertEqual(status,200)
        self.assertEqual(len(audit),6)
        self.assertEqual(self.request('GET',f'/tasks/{t}/audit',token=self.agent('Outsider'))[0],400)

    def test_concurrent_requests_do_not_overspend(self):
        a=self.agent('Concurrent')
        def create(_):
            return self.request('POST','/tasks',{'title':'Race','description':'Test','reward':75},a)[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses=list(pool.map(create,range(8)))
        self.assertEqual(statuses.count(201),1)
        self.assertEqual(statuses.count(400),7)
        self.assertEqual(self.request('GET','/me',token=a)[1]['balance'],25)

    def test_release_and_refund_over_http(self):
        a,b=self.agent('Refund creator'),self.agent('Releasing worker')
        _,data=self.request('POST','/tasks',{'title':'Refund','description':'Test','reward':30},a)
        t=data['task_id']
        for action,token in [('accept',b),('release',b),('cancel',a)]:
            self.assertEqual(self.request('POST',f'/tasks/{t}/{action}',{},token)[0],201)
        self.assertEqual(self.request('GET','/me',token=a)[1]['balance'],100)

    def test_keyed_create_replay_and_conflict(self):
        a=self.agent('Retry creator')
        headers={'Idempotency-Key':'network-retry-1'}
        data={'title':'Retry','description':'Submit once','reward':20}
        first=self.request('POST','/tasks',data,a,extra_headers=headers)
        replay=self.request('POST','/tasks',data,a,extra_headers=headers)
        self.assertEqual(first,replay)
        self.assertEqual(first[0],201)
        conflict=self.request('POST','/tasks',{**data,'reward':21},a,extra_headers=headers)
        self.assertEqual(conflict[0],409)
        self.assertEqual(self.request('GET','/me',token=a)[1]['balance'],80)
        self.assertEqual(self.request('POST','/tasks',data,'invalid',extra_headers=headers)[0],401)

    def test_concurrent_keyed_http_retries(self):
        a=self.agent('Network retries')
        headers={'Idempotency-Key':'one-job'}
        def call(_):
            return self.request('POST','/tasks',{'title':'Retry','description':'Race','reward':75},a,extra_headers=headers)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses=list(pool.map(call,range(8)))
        self.assertTrue(all(r==responses[0] for r in responses))
        self.assertEqual(responses[0][0],201)
        self.assertEqual(self.request('GET','/me',token=a)[1]['balance'],25)

    def test_keyed_release_cancel_and_approval_replays(self):
        a,b=self.agent('Keyed creator'),self.agent('Keyed worker')
        _,data=self.request('POST','/tasks',{'title':'Task','description':'Work','reward':15},a)
        t=data['task_id']
        for action,token in [('accept',b),('release',b),('cancel',a)]:
            headers={'Idempotency-Key':action+'-one'}
            first=self.request('POST',f'/tasks/{t}/{action}',{},token,extra_headers=headers)
            self.assertEqual(first[0],201)
            self.assertEqual(first,self.request('POST',f'/tasks/{t}/{action}',{},token,extra_headers=headers))
        self.assertEqual(self.request('GET','/me',token=a)[1]['balance'],100)

    def test_invalid_keys_and_unsupported_endpoints(self):
        a=self.agent('Header validation')
        for key in ('','has space','a'*129):
            self.assertEqual(self.request('POST','/tasks',{'title':'Test','description':'Work','reward':1},a,
                                          extra_headers={'Idempotency-Key':key})[0],400)
        self.assertEqual(self.request('POST','/posts',{'body':'Text'},a,
                                      extra_headers={'Idempotency-Key':'unsupported'})[0],400)

    def test_duplicate_idempotency_headers_are_rejected(self):
        a=self.agent('Duplicate headers')
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        data=json.dumps({'title':'Test','description':'Work','reward':10}).encode()
        try:
            conn.putrequest('POST','/tasks')
            conn.putheader('Content-Type','application/json')
            conn.putheader('Content-Length',str(len(data)))
            conn.putheader('Authorization','Bearer '+a)
            conn.putheader('Idempotency-Key','one')
            conn.putheader('Idempotency-Key','two')
            conn.endheaders(data)
            response=conn.getresponse()
            self.assertEqual(response.status,400)
            response.read()
        finally: conn.close()
        self.assertEqual(self.request('GET','/me',token=a)[1]['balance'],100)
