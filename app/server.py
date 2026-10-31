"""Local demo HTTP API. Bind to 127.0.0.1 only; not production hardened."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path
from .protocol import CAPABILITIES
from .market import Market
from .core import Engine, AEPError, IdempotencyConflict


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send(self, status, payload):
        data=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path=urlparse(self.path).path
        engine=self.server.engine
        try:
            if path in ('/','/portal','/portal.js','/portal.css'):
                return self.asset(path)
            if path=='/cases': result=engine.contracts.cases.list(engine.authenticate(self.bearer()))
            elif path.startswith('/cases/') and path.rsplit('/',1)[-1].isdigit(): result=engine.contracts.cases.view(engine.authenticate(self.bearer()),int(path.rsplit('/',1)[-1]))
            elif path=='/experts': result=self.server.market.community.experts()
            elif path=='/graph': result=self.server.market.community.graph()
            elif path=='/subscriptions': result=self.server.market.community.activity(engine.authenticate(self.bearer()))
            elif path=='/relations/invitations': result=self.server.market.community.invitations(engine.authenticate(self.bearer()))
            elif path=='/projects': result=self.server.market.community.projects(engine.authenticate(self.bearer()))
            elif path.startswith('/projects/') and path.rsplit('/',1)[-1].isdigit():
                result=self.server.market.community.project(engine.authenticate(self.bearer()),int(path.rsplit('/',1)[-1]))
            elif path=='/memories': result=self.server.market.community.memories(engine.authenticate(self.bearer()))
            elif path.startswith('/memories/'):
                result=self.server.market.community.memory(engine.authenticate(self.bearer()),path.rsplit('/',1)[-1])
            elif path=='/requests':
                with engine._lock: result=[t|{'contract':engine.contracts.view(t['id'])} for t in engine.tasks()]
            elif path=='/publisher':
                actor=engine.authenticate(self.bearer()); result=engine.profile(actor)|{'kind':(engine._one('SELECT kind FROM identities WHERE actor=?',(actor,)) or {'kind':'agent'})['kind']}
            elif path=='/health': result={'status':'ok','version':'0.8.0','mode':'simulation'}
            elif path=='/.well-known/aep.json': result=CAPABILITIES|{'network':self.server.market.network}
            elif path=='/market/listings': result=self.server.market.listings()
            elif path=='/wallet': result=self.server.market.wallet(engine.authenticate(self.bearer()))
            elif path=='/wallet/backup': result=self.server.market.backup(engine.authenticate(self.bearer()))
            elif path.startswith('/market/orders/') and path.rsplit('/',1)[-1].isdigit():
                result=self.server.market.order(engine.authenticate(self.bearer()),int(path.rsplit('/',1)[-1]))
            elif path=='/scores': result=engine.scores()
            elif path=='/posts': result=engine.feed()
            elif path=='/tasks': result=engine.tasks()
            elif path=='/events': result=engine.events()
            elif path=='/me': result=engine.profile(engine.authenticate(self.bearer()))
            elif path.startswith('/tasks/') and path.endswith('/audit'):
                parts=path.strip('/').split('/')
                if len(parts)!=3 or not parts[1].isdigit(): return self.send(404,{'error':'not found'})
                result=engine.task_audit(engine.authenticate(self.bearer()),int(parts[1]))
            else: return self.send(404,{'error':'not found'})
            self.send(200,result)
        except PermissionError as e: self.send(401,{'error':str(e)})
        except AEPError as e: self.send(400,{'error':str(e)})
        except Exception: self.send(500,{'error':'internal error'})

    def asset(self,path):
        name={'/':'portal.html','/portal':'portal.html','/portal.js':'portal.js','/portal.css':'portal.css'}[path]
        data=(Path(__file__).parent/'web'/name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.rsplit('.',1)[1]])
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers();self.wfile.write(data)

    def bearer(self):
        h=self.headers.get('Authorization','')
        return h[7:] if h.startswith('Bearer ') else ''

    def do_POST(self):
        engine=self.server.engine
        try:
            origin=self.headers.get('Origin')
            allowed={'http://127.0.0.1:'+str(self.server.server_port),'http://localhost:'+str(self.server.server_port)}|getattr(self.server,'preview_origins',set())
            if origin and origin not in allowed:return self.send(403,{'error':'cross-origin write denied'})
            if self.headers.get('Transfer-Encoding'):
                return self.send(400,{'error':'transfer encoding unsupported'})
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)>1: raise AEPError('duplicate Content-Length')
            length=int(lengths[0]) if lengths else 0
            if length>20000 or length<0: return self.send(413,{'error':'payload too large'})
            if length and self.headers.get_content_type()!='application/json':
                return self.send(415,{'error':'Content-Type must be application/json'})
            raw=self.rfile.read(length)
            if len(raw)!=length: raise AEPError('incomplete request body')
            data=json.loads(raw or '{}')
            if not isinstance(data,dict): raise AEPError('JSON object required')
            path=urlparse(self.path).path
            keys=self.headers.get_all('Idempotency-Key',[])
            if len(keys)>1: raise AEPError('duplicate Idempotency-Key')
            key=keys[0] if keys else None
            if key is not None and not (path in ('/tasks','/requests') or path.startswith(('/tasks/','/requests/'))):
                raise AEPError('Idempotency-Key is supported only for task operations')
            with engine._lock:
                if path=='/agents': result=engine.register(data.get('name'))
                elif path=='/humans': result=engine.register(data.get('name'),kind='human')
                elif path=='/wallet/freeze': result=self.server.market.freeze(data['message']['actor'],data)
                else:
                    actor=engine.authenticate(self.bearer())
                    if path=='/wallet/enroll': result=self.server.market.enroll(actor,data)
                    elif path=='/commands': result=self.server.market.execute(actor,data)
                    elif path=='/subscriptions' and not self.server.market.enrolled(actor):
                        identity=engine._one('SELECT kind FROM identities WHERE actor=?',(actor,))
                        if not identity or identity['kind']!='human':raise PermissionError('human publisher required')
                        action='relation:remove' if data.get('remove') is True else 'relation:set'
                        result=engine._tx(lambda:self.server.market.community.apply(actor,action,{'target':data['target'],'kind':'subscribe'}))
                    elif path=='/case-key' or path.startswith('/cases/'):
                        identity=engine._one('SELECT kind FROM identities WHERE actor=?',(actor,))
                        if not identity or identity['kind']!='human':raise PermissionError('human publisher required; agents use signed case:append')
                        if path=='/case-key':result=engine.contracts.cases.register_key(actor,data['public_key'])
                        else:
                            parts=path.strip('/').split('/')
                            if len(parts)!=3 or not parts[1].isdigit() or parts[2]!='entries':raise AEPError('invalid case route')
                            result=engine.contracts.cases.append(actor,int(parts[1]),data)
                    elif path=='/requests' or path.startswith('/requests/'):
                        identity=engine._one('SELECT kind FROM identities WHERE actor=?',(actor,))
                        if not identity or identity['kind']!='human' or self.server.market.enrolled(actor):raise PermissionError('unenrolled human publisher required')
                        if path=='/requests':
                            if 'contract' not in data:raise AEPError('fixed-v1 contract required')
                            result=engine.task_request(actor,'create',data,request_key=key)
                        else:
                            parts=path.strip('/').split('/')
                            if len(parts)!=3 or not parts[1].isdigit() or parts[2] not in ('approve','dispute','settle','cancel'):raise AEPError('invalid publisher action')
                            result=engine.task_request(actor,parts[2],data,task_id=int(parts[1]),request_key=key)
                    elif self.server.market.enrolled(actor):
                        return self.send(403,{'error':'wallet enrolled: signed /commands required for mutations'})
                    elif path=='/posts': result=engine.post(actor,data.get('body'))
                    elif path=='/tasks': result=engine.task_request(actor,'create',data,request_key=key)
                    elif path.startswith('/tasks/'):
                        parts=path.strip('/').split('/')
                        if len(parts)!=3 or not parts[1].isdigit(): return self.send(404,{'error':'not found'})
                        tid=int(parts[1]); action=parts[2]
                        if action not in ('accept','submit','approve','cancel','release','revise','dispute','settle'):
                            return self.send(404,{'error':'not found'})
                        result=engine.task_request(actor,action,data,task_id=tid,request_key=key)
                    elif path=='/predictions': result=engine.predict(actor,data.get('event_id'),data.get('probability'),data.get('rationale'))
                    else: return self.send(404,{'error':'not found'})
            self.send(201,result)
        except PermissionError as e: self.send(401,{'error':str(e)})
        except IdempotencyConflict as e: self.send(409,{'error':str(e)})
        except (AEPError,ValueError,TypeError,UnicodeError,KeyError) as e: self.send(400,{'error':str(e)})
        except Exception: self.send(500,{'error':'internal error'})


def make_server(engine, port=8765, host='127.0.0.1'):
    server=ThreadingHTTPServer((host,port),Handler)
    server.engine=engine
    server.market=Market(engine)
    return server


def main():
    engine=Engine(os.getenv('AEP_DB','aep_demo.sqlite3'))
    server=make_server(engine)
    print('AEP local demo API: http://127.0.0.1:8765')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.close()


if __name__=='__main__':
    main()
