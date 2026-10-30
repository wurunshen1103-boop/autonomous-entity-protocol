"""Offline, deterministic workflow demonstration; no model calls or API costs."""
from .core import Engine


def main():
    engine=Engine()
    try:
        creator=engine.register('Research coordinator')['agent_id']
        worker=engine.register('Research agent')['agent_id']
        task=engine.create_task(creator,'Compare evidence','Summarize public evidence',15)['task_id']
        engine.accept(worker,task)
        engine.submit(worker,task,'First draft with two sources')
        engine.revise(creator,task)
        engine.submit(worker,task,'Revised draft with limitations')
        engine.approve(creator,task)
        cancelled=engine.create_task(creator,'Optional follow-up','No longer needed',10)['task_id']
        engine.cancel(creator,cancelled)
        for row in engine.task_audit(creator,task):
            print(f"Task {task}: {row['action']}")
        print('Creator balance:',engine.profile(creator)['balance'])
        print('Worker balance:',engine.profile(worker)['balance'])
        print('All credits are simulated. No AI model executed these submissions.')
    finally:
        engine.close()


if __name__=='__main__':
    main()
