"""Local experiment operator; no remote administrative endpoint."""
import argparse
import json
from .core import Engine

def main():
    p=argparse.ArgumentParser();p.add_argument('--db',default='aep_demo.sqlite3')
    sub=p.add_subparsers(dest='command',required=True)
    create=sub.add_parser('event');create.add_argument('question');create.add_argument('--close-at',required=True,type=int)
    resolve=sub.add_parser('resolve');resolve.add_argument('event_id',type=int);resolve.add_argument('outcome',type=int,choices=[0,1]);resolve.add_argument('--evidence',required=True)
    sub.add_parser('scores');a=p.parse_args();e=Engine(a.db)
    try:
        if a.command=='event': result=e.create_event(a.question,a.close_at)
        elif a.command=='resolve': result=e.resolve_event(a.event_id,a.outcome,a.evidence)
        else: result=e.scores()
        print(json.dumps(result,ensure_ascii=False))
    finally: e.close()
if __name__=='__main__':main()
