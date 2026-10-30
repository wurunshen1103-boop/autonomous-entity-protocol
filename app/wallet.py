"""Client-side keys and encryption. Experimental protocol, not audited custody software."""
import base64
import hashlib
import json
import os
import time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DOMAIN='aep-local-wallet-v1'

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()

def b64(value): return base64.b64encode(value).decode('ascii')
def unb64(value):
    if not isinstance(value,str): raise ValueError('base64 string required')
    return base64.b64decode(value,validate=True)
def public_sign(key):
    return b64(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw))
def wallet_id(public): return hashlib.sha256(unb64(public)).hexdigest()
def verify(public,message,signature):
    try: ed25519.Ed25519PublicKey.from_public_bytes(unb64(public)).verify(unb64(signature),canonical(message))
    except (ValueError,TypeError,InvalidSignature) as exc: raise ValueError('invalid signature') from exc

def encrypt_content(plaintext):
    if not isinstance(plaintext,str) or not 1<=len(plaintext.encode())<=6000: raise ValueError('content must be 1-6000 UTF-8 bytes')
    key=AESGCM.generate_key(bit_length=256);nonce=os.urandom(12)
    return {'nonce':b64(nonce),'ciphertext':b64(AESGCM(key).encrypt(nonce,plaintext.encode(),b'aep-content-v1'))},key

def content_digest(blob): return hashlib.sha256(canonical(blob)).hexdigest()
def delivery_context(order):
    return canonical({'order_id':order['id'],'listing_id':order['listing_id'],'buyer_wallet':order['buyer_wallet'],'digest':order['digest']})
def wrap_content_key(key,order):
    cert=order['buyer_certificate'];cm=cert['message']
    verify(cm['spend_public'],cm,cert['signature'])
    purchase=order['purchase_proof'];pm=purchase['message']
    verify(cm['spend_public'],pm,purchase['signature'])
    if (wallet_id(cm['spend_public'])!=order['buyer_wallet'] or cm['encryption_public']!=order['buyer_encryption_public']
        or cm['actor']!=order['buyer'] or pm['wallet']!=order['buyer_wallet'] or pm['actor']!=order['buyer']
        or pm['action']!='buy' or pm['network']!=cm['network'] or pm['body']['listing_id']!=order['listing_id']
        or pm['body']['expected_digest']!=order['digest'] or pm['body']['expected_price']!=order['amount']):
        raise ValueError('buyer binding or signed purchase mismatch')
    pub=serialization.load_pem_public_key(order['buyer_encryption_public'].encode())
    if not isinstance(pub,rsa.RSAPublicKey) or pub.key_size!=3072: raise ValueError('RSA-3072 key required')
    return b64(pub.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=delivery_context(order))))

class Wallet:
    def __init__(self,spend=None,decrypt=None):
        self.spend=spend or ed25519.Ed25519PrivateKey.generate()
        self.decrypt=decrypt or rsa.generate_private_key(public_exponent=65537,key_size=3072)
    @property
    def id(self): return wallet_id(public_sign(self.spend))
    def enrollment(self,actor,network,recovery_public=None):
        value={'domain':DOMAIN,'network':network,'action':'enroll','actor':actor,
               'spend_public':public_sign(self.spend),
               'encryption_public':self.decrypt.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
               'recovery_public':recovery_public}
        return {'message':value,'signature':b64(self.spend.sign(canonical(value)))}
    def command(self,actor,network,seq,action,body,expires_at=None):
        value={'domain':DOMAIN,'network':network,'wallet':self.id,'actor':actor,'seq':seq,
               'expires_at':expires_at or int(time.time())+600,'action':action,'body':body}
        return {'message':value,'signature':b64(self.spend.sign(canonical(value)))}
    def decrypt_order(self,order):
        if order['buyer_wallet']!=self.id: raise ValueError('wrong recipient')
        listing=order['listing_proof'];lm=listing['message']
        verify(order['seller_spend_public'],lm,listing['signature'])
        if lm['action']!='list' or lm['actor']!=order['seller'] or lm['wallet']!=wallet_id(order['seller_spend_public']) or content_digest(lm['body']['blob'])!=order['digest']:
            raise ValueError('signed listing mismatch')
        envelope=order['delivery_proof'];message=envelope['message']
        verify(order['seller_spend_public'],message,envelope['signature'])
        if message['network']!=lm['network'] or message['actor']!=lm['actor'] or message['wallet']!=lm['wallet'] or message['action']!='deliver' or message['body']!={'order_id':order['id'],'wrapped_key':order['wrapped_key']}:
            raise ValueError('delivery does not match signed order')
        if content_digest(order['blob'])!=order['digest']: raise ValueError('ciphertext digest mismatch')
        key=self.decrypt.decrypt(unb64(order['wrapped_key']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=delivery_context(order)))
        return AESGCM(key).decrypt(unb64(order['blob']['nonce']),unb64(order['blob']['ciphertext']),b'aep-content-v1').decode()
    def backup(self):
        # Return the offline recovery secret separately. Never send it to the server.
        secret=AESGCM.generate_key(bit_length=256);nonce=os.urandom(12)
        data={'spend':b64(self.spend.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())),
              'decrypt':self.decrypt.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()}
        return {'nonce':b64(nonce),'ciphertext':b64(AESGCM(secret).encrypt(nonce,canonical(data),b'aep-backup-v1'))},secret
    @classmethod
    def restore(cls,blob,offline_secret):
        data=json.loads(AESGCM(offline_secret).decrypt(unb64(blob['nonce']),unb64(blob['ciphertext']),b'aep-backup-v1'))
        return cls(ed25519.Ed25519PrivateKey.from_private_bytes(unb64(data['spend'])),serialization.load_pem_private_key(data['decrypt'].encode(),None))

def recovery_request(key,actor,network,wallet,nonce):
    msg={'domain':DOMAIN,'network':network,'wallet':wallet,'actor':actor,'action':'freeze','nonce':nonce,'expires_at':int(time.time())+600}
    return {'message':msg,'signature':b64(key.sign(canonical(msg)))}
