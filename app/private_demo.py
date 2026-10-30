"""End-to-end HTTP demo. Keys, plaintext and content keys stay in client memory."""
import json
import threading
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .core import Engine
from .server import make_server
from .client import Client
from .wallet import Wallet,encrypt_content,wrap_content_key,public_sign,recovery_request


def main():
    engine=Engine();server=make_server(engine,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    try:
        network=Client(url).request('GET','/.well-known/aep.json')['network']
        parties=[]
        for name in ('Information seller','Information buyer'):
            agent=Client(url).request('POST','/agents',{'name':name})
            wallet=Wallet();freeze_key=Ed25519PrivateKey.generate()
            client=Client(url,agent['api_token'])
            client.request('POST','/wallet/enroll',wallet.enrollment(agent['agent_id'],network,public_sign(freeze_key)))
            parties.append((agent['agent_id'],wallet,client,freeze_key))
        def command(party,action,body):
            actor,wallet,client,_=party
            envelope=wallet.command(actor,network,client.request('GET','/wallet')['next_seq'],action,body)
            # Production callers must persist this envelope before sending and retry unchanged.
            return client.request('POST','/commands',envelope)
        seller,buyer=parties
        blob,content_key=encrypt_content('Private analysis: synthetic benchmark result 42; demo data only.')
        listing=command(seller,'list',{'title':'Synthetic benchmark analysis','price':15,'capacity':2,'blob':blob})
        terms=buyer[2].request('GET','/market/listings')[0]
        order=command(buyer,'buy',{'listing_id':listing['listing_id'],'expected_price':terms['price'],'expected_digest':terms['digest']})
        path='/market/orders/'+str(order['order_id'])
        delivery=seller[2].request('GET',path)
        command(seller,'deliver',{'order_id':order['order_id'],'wrapped_key':wrap_content_key(content_key,delivery)})
        received=buyer[2].request('GET',path)
        plaintext=buyer[1].decrypt_order(received)
        assert '42' in plaintext
        command(buyer,'confirm',{'order_id':order['order_id']})
        backup,offline_secret=buyer[1].backup()
        command(buyer,'backup',{'blob':backup})
        assert Wallet.restore(buyer[2].request('GET','/wallet/backup'),offline_secret).id==buyer[1].id
        buyer[2].request('POST','/wallet/freeze',recovery_request(buyer[3],buyer[0],network,buyer[1].id,'demo-freeze'))
        print(json.dumps({'delivered_and_decrypted':True,'seller_balance':seller[2].request('GET','/wallet')['balance'],
                          'buyer_balance':buyer[2].request('GET','/wallet')['balance'],'encrypted_backup_restored':True,
                          'buyer_frozen_for_new_spending':True,'mode':'simulation'},indent=2))
    finally:
        server.shutdown();thread.join();server.server_close();engine.close()
if __name__=='__main__': main()
