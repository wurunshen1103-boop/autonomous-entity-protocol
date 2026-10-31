"""Client-only evidence encryption. Private keys and plaintext never go to the API."""
import hashlib
import json
import os
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .cases import context
from .wallet import canonical,b64,unb64

def seal_evidence(private_key,case,author,entry_id,document,*,trusted_recipients,kind='evidence',reply_to=None):
    if case['recipients']!=trusted_recipients:raise ValueError('case recipient keys differ from independently pinned keys')
    # Check the locally held key against the pinned case recipient key before uploading.
    own=private_key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    if case['recipients'].get(author)!=own:raise ValueError('local evidence key does not match case')
    raw=canonical(document)
    if not 1<=len(raw)<=6000:raise ValueError('evidence document must be 1-6000 bytes')
    ctx=context(case,author,entry_id,kind,reply_to);aad=canonical(ctx)
    key=AESGCM.generate_key(bit_length=256);nonce=os.urandom(12)
    keys={a:b64(serialization.load_pem_public_key(pub.encode()).encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=aad))) for a,pub in case['recipients'].items()}
    return {'entry_id':entry_id,'kind':kind,'reply_to':reply_to,'envelope':{'context':ctx,'nonce':b64(nonce),'ciphertext':b64(AESGCM(key).encrypt(nonce,raw,aad)),'wrapped_keys':keys}}

def open_evidence(private_key,case,recipient,entry):
    ctx=context(case,entry['author'],entry['entry_id'],entry['kind'],entry['reply_to']);env=entry['envelope']
    if env['context']!=ctx:raise ValueError('evidence context mismatch')
    aad=canonical(ctx)
    key=private_key.decrypt(unb64(env['wrapped_keys'][recipient]),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=aad))
    return json.loads(AESGCM(key).decrypt(unb64(env['nonce']),unb64(env['ciphertext']),aad))


def verify_history(case,known_hashes=()):
    """Verify record links and previously retained receipts, not server honesty or truth."""
    previous='0'*64;seen=set()
    for entry in case['entries']:
        body={k:entry[k] for k in ('entry_id','kind','reply_to','envelope')}
        digest=hashlib.sha256(canonical(body)).hexdigest()
        record={k:entry[k] for k in ('case_id','entry_id','author','kind','reply_to','digest','created_at','previous_hash')}
        if entry['case_id']!=case['id'] or entry['previous_hash']!=previous or entry['digest']!=digest or hashlib.sha256(canonical(record)).hexdigest()!=entry['record_hash']:
            raise ValueError('case history integrity mismatch')
        previous=entry['record_hash'];seen.add(previous)
    if not set(known_hashes)<=seen:raise ValueError('previously observed evidence missing or changed')
    return previous
