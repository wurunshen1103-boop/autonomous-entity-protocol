"""Dependency-free client. Persist logical request keys before invoking task()."""
import json
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None

class APIError(RuntimeError):
    def __init__(self,status,payload):
        super().__init__(str(payload)); self.status=status; self.payload=payload

class Client:
    def __init__(self,base_url='http://127.0.0.1:8765',token=None):
        parsed=urlparse(base_url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid API base URL')
        if parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
            raise ValueError('remote API requires HTTPS')
        self.base=base_url.rstrip('/'); self.token=token
        self.opener=build_opener(NoRedirect())

    def request(self,method,path,data=None,key=None):
        if not path.startswith('/') or path.startswith('//'): raise ValueError('relative API path required')
        body=None if data is None else json.dumps(data,allow_nan=False).encode()
        headers={'Content-Type':'application/json'}
        if self.token: headers['Authorization']='Bearer '+self.token
        if key is not None: headers['Idempotency-Key']=key
        attempts=3 if key is not None else 1
        for attempt in range(attempts):
            try:
                with self.opener.open(Request(self.base+path,body,headers,method=method),timeout=15) as response:
                    return json.load(response)
            except HTTPError as e:
                payload=e.read().decode('utf-8',errors='replace')
                if e.code<500 or attempt==attempts-1: raise APIError(e.code,payload) from e
            except (URLError,TimeoutError,ConnectionError):
                if attempt==attempts-1: raise
            time.sleep(.1*(attempt+1))

    def task(self,action,data,key,task_id=None):
        if not key: raise ValueError('persist a request key before sending')
        path='/tasks' if action=='create' else f'/tasks/{int(task_id)}/{action}'
        return self.request('POST',path,data,key)
