"""Encrypted local agent state, one writer, durable signed-command replay.

The caller keeps the 32-byte vault secret outside this file. No model provider is
called. A controlling host can still read live process memory.
"""
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .wallet import Wallet,b64,unb64,canonical,seal_memory,open_memory
from .client import Client,APIError

class AgentSession:
    def __init__(self,path,secret,base_url,initial=None):
        if len(secret)!=32:raise ValueError('vault secret must contain 32 bytes')
        self.path=Path(path);self.secret=secret;self.lock=None
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=open(os.open(str(self.path)+'.lock',os.O_CREAT|os.O_RDWR,0o600),'a+')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if self.path.exists():
                if initial is not None:raise ValueError('vault already exists')
                blob=json.loads(self.path.read_text());self.state=json.loads(AESGCM(secret).decrypt(unb64(blob['nonce']),unb64(blob['ciphertext']),b'aep-agent-session-v1'))
            else:
                if initial is None:raise ValueError('vault does not exist')
                self.state=initial|{'pending':None,'receipts':{},'content_keys':{},'memory_versions':{},'memory_intents':{}}
                self.save()
            self.wallet=Wallet.restore(self.state['wallet_blob'],unb64(self.state['wallet_secret']))
            self.client=Client(base_url,self.state['token'])
            self.check_binding()
        except Exception:
            self.close();raise
    @classmethod
    def create(cls,path,secret,base_url,actor,token,wallet,network):
        blob,offline=wallet.backup()
        return cls(path,secret,base_url,{'actor':actor,'token':token,'network':network,'wallet_blob':blob,'wallet_secret':b64(offline)})
    def check_binding(self):
        state=self.client.request('GET','/wallet')
        profile=self.client.request('GET','/me')
        if state['network']!=self.state['network'] or state['wallet_id']!=self.wallet.id or profile['id']!=self.state['actor']:raise ValueError('server or account binding changed')
        return state
    def save(self):
        nonce=os.urandom(12)
        blob={'nonce':b64(nonce),'ciphertext':b64(AESGCM(self.secret).encrypt(nonce,canonical(self.state),b'aep-agent-session-v1'))}
        fd,temp=tempfile.mkstemp(prefix='.aep-vault-',dir=str(self.path.parent))
        try:
            with os.fdopen(fd,'w') as f:json.dump(blob,f);f.flush();os.fsync(f.fileno())
            os.replace(temp,self.path)
            parent=os.open(str(self.path.parent),os.O_RDONLY)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally:
            if os.path.exists(temp):os.unlink(temp)
    def command(self,operation,action,body):
        """Persist caller-chosen logical operation ID before sending. Retry unchanged."""
        if not isinstance(operation,str) or not 1<=len(operation)<=128:raise ValueError('operation id required')
        intent={'action':action,'body':body}
        old=self.state['receipts'].get(operation)
        if old:
            if old['intent']!=intent:raise ValueError('operation id reused')
            return old['result']
        pending=self.state['pending']
        if pending:
            if pending['operation']!=operation or pending['intent']!=intent:raise ValueError('resolve pending command by retrying its original operation first')
        else:
            remote=self.check_binding()
            pending={'operation':operation,'intent':intent,'envelope':self.wallet.command(self.state['actor'],self.state['network'],remote['next_seq'],action,body)}
            self.state['pending']=pending;self.save()
        # Keep pending on transport errors, 5xx AND 4xx. Explicit reconciliation prevents
        # silently dropping an earlier uncertain successful command.
        result=self.client.request('POST','/commands',pending['envelope'])
        self.state['receipts'][operation]={'intent':intent,'result':result}
        self.state['pending']=None
        if action in ('memory:put','memory:delete'):self.state['memory_versions'][body['id']]=result['version']
        self.save()
        return result
    def retry_pending(self):
        p=self.state['pending']
        if not p:return None
        return self.command(p['operation'],p['intent']['action'],p['intent']['body'])
    def discard_rejected(self):
        """Only abandon when the server has not consumed this sequence. Single writer required."""
        p=self.state['pending']
        if not p:return
        if int(time.time())<=p['envelope']['message']['expires_at']:raise ValueError('wait for signature expiry before abandoning an uncertain request')
        remote=self.check_binding()
        if remote['next_seq']!=p['envelope']['message']['seq']:raise ValueError('sequence consumed; replay original command to recover receipt')
        self.state['pending']=None;self.save()
    def remember(self,operation,mid,document):
        intent={'id':mid,'document':document}
        previous=self.state['memory_intents'].get(operation)
        if previous is not None and previous!=intent:raise ValueError('memory operation id reused for different content')
        self.state['memory_intents'][operation]=intent;self.save()
        old=self.state['receipts'].get(operation)
        if old:return self.command(operation,'memory:put',old['intent']['body'])
        p=self.state['pending']
        if p:
            if p['operation']!=operation:raise ValueError('resolve pending command first')
            return self.retry_pending()
        rows=self.client.request('GET','/memories');version=next((r['version'] for r in rows if r['id']==mid),0)
        env=seal_memory(self.wallet,self.state['network'],self.state['actor'],mid,version+1,document)
        result=self.command(operation,'memory:put',{'id':mid,'expected_version':version,'envelope':env})
        self.state['memory_versions'][mid]=result['version'];self.save();return result
    def recall(self,mid):
        row=self.client.request('GET','/memories/'+mid)
        if row['version']<self.state['memory_versions'].get(mid,0):raise ValueError('memory rollback detected')
        if row['deleted']:raise ValueError('memory deleted')
        doc=open_memory(self.wallet,self.state['network'],self.state['actor'],mid,row['version'],row['envelope'])
        self.state['memory_versions'][mid]=row['version'];self.save();return doc
    def search(self,query):
        """Small local scan; query and decrypted document never sent back to the platform."""
        if not isinstance(query,str) or not query:raise ValueError('nonempty query required')
        return [{'id':r['id'],'document':doc} for r in self.client.request('GET','/memories') if not r['deleted']
                for doc in [self.recall(r['id'])] if query.casefold() in json.dumps(doc,ensure_ascii=False).casefold()]
    def close(self):
        if self.lock:self.lock.close();self.lock=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
