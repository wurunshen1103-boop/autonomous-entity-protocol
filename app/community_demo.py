"""Reproducible HTTP scenario with scripted agents, not real model execution."""
import json
import os
import tempfile
import threading
from .core import Engine
from .server import make_server
from .client import Client
from .wallet import Wallet
from .agent_session import AgentSession

TERMS={'rule':'fixed-v1','required_fields':['summary','evidence'],'accept_seconds':604800,'work_seconds':3600,'review_seconds':86400}

def run():
    e=Engine();server=make_server(e,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    try:
        c=Client(url);network=c.request('GET','/.well-known/aep.json')['network']
        human=c.request('POST','/humans',{'name':'Research publisher'});publisher=Client(url,human['api_token'])
        agents=[]
        for name in ('Data specialist','Evidence reviewer'):
            a=c.request('POST','/agents',{'name':name});w=Wallet();Client(url,a['api_token']).request('POST','/wallet/enroll',w.enrollment(a['agent_id'],network))
            agents.append((a,w))
        with tempfile.TemporaryDirectory() as directory:
            secrets=[os.urandom(32),os.urandom(32)];sessions=[]
            try:
                for i,(a,w) in enumerate(agents):
                    sessions.append(AgentSession.create(directory+f'/{i}.vault',secrets[i],url,a['agent_id'],a['api_token'],w,network))
                one,two=sessions
                one.command('profile','profile:set',{'bio':'Checks public dataset structure. Scripted demo.','skills':['data quality']})
                two.command('profile','profile:set',{'bio':'Reviews evidence provenance. Scripted demo.','skills':['evidence review']})
                one.command('connect','relation:set',{'target':agents[1][0]['agent_id'],'kind':'collaborate'})
                two.command('accept-connection','relation:accept',{'target':agents[0][0]['agent_id'],'kind':'collaborate'})
                publisher.request('POST','/subscriptions',{'target':agents[0][0]['agent_id']})
                task=publisher.request('POST','/requests',{'title':'Check a synthetic dataset','description':'Return summary and evidence fields; demo data only.','reward':15,'contract':TERMS},'human-task-1')
                one.command('accept-task','task:accept',{'task_id':task['task_id'],'data':{'expected_contract':TERMS}})
                one.remember('research-note','research_note_0001',{'title':'Private draft','tags':['synthetic'],'text':'MEMORY-SENTINEL: synthetic sample has 3 missing rows'})
                one.close();sessions[0]=AgentSession(directory+'/0.vault',secrets[0],url);one=sessions[0]
                assert len(one.search('3 missing'))==1
                pid=one.command('team','project:create',{'title':'Dataset research','description':'Private coordination; tasks explicitly public.'})['project_id']
                one.command('invite','project:invite',{'project_id':pid,'target':agents[1][0]['agent_id']})
                two.command('join','project:join',{'project_id':pid})
                review=one.command('review-task','project:task',{'project_id':pid,'data':{'title':'Review synthetic result','description':'Independent fixture review','reward':5,'contract':TERMS}})
                two.command('review-accept','task:accept',{'task_id':review['task_id'],'data':{'expected_contract':TERMS}})
                result=json.dumps({'summary':'3 missing rows in synthetic fixture','evidence':'synthetic fixture only; no model called'})
                two.command('review-submit','task:submit',{'task_id':review['task_id'],'data':{'submission':result}})
                one.command('review-approve','task:approve',{'task_id':review['task_id']})
                one.command('submit-task','task:submit',{'task_id':task['task_id'],'data':{'submission':result}})
                publisher.request('POST',f"/requests/{task['task_id']}/approve",{},'human-approve-1')
                assert 'MEMORY-SENTINEL' not in '\n'.join(e.db.iterdump())
                return {'human_task':'completed','project_review':'completed','memory_restored_and_searched':True,'server_contains_memory_plaintext':False,
                        'relationship_edges':len(c.request('GET','/graph')['edges']),
                        'balances':{'publisher':publisher.request('GET','/me')['balance'],'specialist':one.check_binding()['balance'],'reviewer':two.check_binding()['balance']},
                        'mode':'scripted simulation; no LLM or paid provider calls'}
            finally:
                for s in sessions:s.close()
    finally:server.shutdown();thread.join();server.server_close();e.close()
if __name__=='__main__':print(json.dumps(run(),indent=2))
