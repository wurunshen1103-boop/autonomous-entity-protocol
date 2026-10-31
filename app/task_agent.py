"""Provider-neutral task runner: caller supplies decide(task) and perform(task).

Never executes task text as code. A crash during an external callback is marked
uncertain, requiring explicit reconciliation instead of repeating a paid call.
"""
import json

class TaskAgent:
    def __init__(self,session,decide,perform):
        self.s=session;self.decide=decide;self.perform=perform
        self.s.state.setdefault('workflows',{})
    def run(self,task):
        tid=task['id'];key=str(tid);flows=self.s.state['workflows']
        flow=flows.get(key)
        if flow and flow['phase'] in ('deciding','performing'):
            raise RuntimeError('external callback outcome uncertain; reconcile before resuming')
        if flow is None:
            if task['status']!='open' or task['creator']==self.s.state['actor']:raise ValueError('eligible open task required')
            flow={'phase':'deciding','task':task};flows[key]=flow;self.s.save()
            decision=self.decide(task)
            if type(decision) is not bool:raise ValueError('decision must be boolean')
            flow['phase']='accepted_decision' if decision else 'declined';self.s.save()
        if flow['phase']=='declined':return {'task_id':tid,'status':'declined'}
        if flow['phase']=='accepted_decision':
            contract=flow['task'].get('contract')
            data={'expected_contract':contract['terms']} if contract else {}
            self.s.command(f'task-{tid}-accept','task:accept',{'task_id':tid,'data':data})
            flow['phase']='accepted';self.s.save()
        if flow['phase']=='accepted':
            flow['phase']='performing';self.s.save()
            result=self.perform(flow['task'])
            if not isinstance(result,str):result=json.dumps(result,ensure_ascii=False,allow_nan=False)
            flow['submission']=result;flow['phase']='ready';self.s.save()
        if flow['phase']=='ready':
            result=self.s.command(f'task-{tid}-submit','task:submit',{'task_id':tid,'data':{'submission':flow['submission']}})
            flow['phase']='submitted';self.s.save();return result
        return {'task_id':tid,'status':flow['phase']}
    def reconcile(self,task_id,value):
        flow=self.s.state['workflows'][str(task_id)]
        if flow['phase']=='deciding':
            if type(value) is not bool:raise ValueError('decision must be boolean')
            flow['phase']='accepted_decision' if value else 'declined'
        elif flow['phase']=='performing':
            flow['submission']=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,allow_nan=False)
            flow['phase']='ready'
        else:raise ValueError('workflow not awaiting external callback reconciliation')
        self.s.save()
