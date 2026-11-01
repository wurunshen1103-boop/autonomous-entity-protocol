"""Two scripted HTTP task outcomes; no real model or payment provider is called."""
import json
import threading
from .core import Engine
from .server import make_server
from .client import Client
from .wallet import Wallet

TERMS={'rule':'failure-v1','required_fields':['summary'],'accept_seconds':3600,'work_seconds':3600,'review_seconds':86400,'evidence_seconds':86400,'response_seconds':86400}

def run():
    engine=Engine();server=make_server(engine,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        base='http://127.0.0.1:'+str(server.server_port);client=Client(base)
        accounts=[client.request('POST','/agents',{'name':n}) for n in ('Scripted publisher','Scripted worker')]
        wallets=[Wallet(),Wallet()];clients=[Client(base,a['api_token']) for a in accounts]
        network=client.request('GET','/.well-known/aep.json')['network']
        for a,w,c in zip(accounts,wallets,clients):c.request('POST','/wallet/enroll',w.enrollment(a['agent_id'],network))
        def cmd(i,action,data,tid=None):
            body={'data':data}
            if tid is not None:body['task_id']=tid
            seq=clients[i].request('GET','/wallet')['next_seq']
            return clients[i].request('POST','/commands',wallets[i].command(accounts[i]['agent_id'],network,seq,action,body))
        def task():
            tid=cmd(0,'task:create',{'title':'Synthetic review','description':'Scripted result; no actual model','reward':15,'contract':TERMS})['task_id']
            cmd(1,'task:accept',{'expected_contract':TERMS},tid)
            return tid
        first=task();failure=cmd(1,'task:fail',{},first)
        assert failure['refund']==15 and failure['worker_amount']==0
        second=task();cmd(1,'task:submit',{'submission':'{"summary":"Only part of the fixture was reviewed"}'},second)
        cmd(0,'task:dispute',{'reason':'incomplete_delivery'},second)
        offer=cmd(1,'task:offer_partial',{'worker_amount':3},second)
        assert clients[1].request('GET','/me')['balance']==100
        partial=cmd(0,'task:accept_partial',{'expected_worker_amount':offer['worker_amount']},second)
        assert partial['refund']==12 and partial['worker_amount']==3
        balances=[c.request('GET','/me')['balance'] for c in clients]
        assert balances==[97,103]
        print(json.dumps({'mode':'scripted HTTP simulation','failed_task_refund':15,'partial_task_refund':12,'accepted_partial_work_payment':3,'balances':balances,'real_model_calls':0}))
    finally:server.shutdown();thread.join();server.server_close();engine.close()

if __name__=='__main__':run()
