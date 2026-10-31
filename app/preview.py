"""Ephemeral browser QA server; no persistent user data or credentials."""
import argparse
from pathlib import Path
from .core import Engine
from .server import make_server

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=3000);parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--strictPort',action='store_true')
    args=parser.parse_args();engine=Engine();server=make_server(engine,args.port,host=args.host)
    # Explicit QA fixtures exist only in this ephemeral preview process.
    from .wallet import Wallet
    pairs=[]
    for name,bio,skills in [('数据质量 Agent（测试）','检查合成数据的缺失、重复和来源。仅供界面验证。',['数据质量','来源核验']),('证据复核 Agent（测试）','独立复核研究证据。仅供界面验证。',['证据复核','研究方法'])]:
        a=engine.register(name);w=Wallet();actor=a['agent_id'];m=server.market
        m.enroll(actor,w.enrollment(actor,m.network));m.execute(actor,w.command(actor,m.network,1,'profile:set',{'bio':bio,'skills':skills}));pairs.append((actor,w))
    a,w=pairs[0];b,v=pairs[1]
    m.execute(a,w.command(a,m.network,2,'relation:set',{'target':b,'kind':'collaborate'}))
    m.execute(b,v.command(b,m.network,2,'relation:accept',{'target':a,'kind':'collaborate'}))
    origin_file=Path('.openai/preview-origin.txt')
    server.preview_origins={origin_file.read_text().strip()} if origin_file.exists() else set()
    print(f'AEP preview http://{args.host}:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close();engine.close()
