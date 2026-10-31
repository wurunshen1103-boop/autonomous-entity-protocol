"""HTTP evidence workflow using scripted clients and an explicitly simulated clock."""
import json
import threading
from unittest.mock import patch
from .core import Engine
from .server import make_server
from .client import Client
from .wallet import Wallet
from .evidence import seal_evidence,open_evidence,verify_history

TERMS={'rule':'evidence-v1','required_fields':['summary'],'accept_seconds':3600,'work_seconds':3600,'review_seconds':60,'evidence_seconds':60,'response_seconds':60}

def run():
    e=Engine();server=make_server(e,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        base='http://127.0.0.1:'+str(server.server_port);client=Client(base);network=server.market.network
        people=[client.request('POST','/agents',{'name':n}) for n in ('Scripted publisher','Scripted worker')]
        wallets=[Wallet(),Wallet()];clients=[Client(base,p['api_token']) for p in people]
        ids=[p['agent_id'] for p in people];pinned={}
        for i in range(2):
            cert=wallets[i].enrollment(ids[i],network);pinned[ids[i]]=cert['message']['encryption_public'];clients[i].request('POST','/wallet/enroll',cert)
        # Both independent local clients have the actual enrollment keys in this fixture.
        def cmd(i,action,body):
            seq=clients[i].request('GET','/wallet')['next_seq']
            return clients[i].request('POST','/commands',wallets[i].command(ids[i],network,seq,action,body))
        tid=cmd(0,'task:create',{'data':{'title':'Synthetic evidence review','description':'Scripted demo, no real model','reward':15,'contract':TERMS}})['task_id']
        cmd(1,'task:accept',{'task_id':tid,'data':{'expected_contract':TERMS}})
        cmd(1,'task:submit',{'task_id':tid,'data':{'submission':json.dumps({'summary':'fixture result'})}})
        cid=cmd(0,'task:dispute',{'task_id':tid,'data':{'reason':'quality_dispute'}})['case_id']
        case=clients[0].request('GET',f'/cases/{cid}')
        evidence=seal_evidence(wallets[0].decrypt,case,ids[0],'evidence_demo_0001',{'text':'PRIVATE-CASE-FIXTURE: cited row missing'},trusted_recipients=pinned)
        receipt=cmd(0,'case:append',{'case_id':cid,'entry':evidence})
        case=clients[1].request('GET',f'/cases/{cid}');verify_history(case,[receipt['record_hash']])
        assert open_evidence(wallets[1].decrypt,case,ids[1],case['entries'][0])['text'].startswith('PRIVATE-CASE-FIXTURE')
        with patch('time.time',return_value=case['evidence_deadline']):
            response=seal_evidence(wallets[1].decrypt,case,ids[1],'response_demo_0001',{'text':'Synthetic counter-evidence'},trusted_recipients=pinned,kind='response',reply_to='evidence_demo_0001')
            cmd(1,'case:append',{'case_id':cid,'entry':response})
        with patch('time.time',return_value=case['settle_after']):
            result=cmd(1,'task:settle',{'task_id':tid})
        closed=clients[0].request('GET',f'/cases/{cid}');verify_history(closed,[receipt['record_hash']])
        assert closed['phase']=='resolved' and result['worker_amount']==7 and result['refund']==8
        assert 'PRIVATE-CASE-FIXTURE' not in '\n'.join(e.db.iterdump())
        print(json.dumps({'mode':'scripted simulation; deadlines advanced in test only','case_phase':closed['phase'],'encrypted_entries':len(closed['entries']),'worker_amount':7,'refund':8,'real_models_called':False}))
    finally:server.shutdown();thread.join();server.server_close();e.close()

if __name__=='__main__':run()
