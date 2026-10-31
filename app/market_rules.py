"""Fixed-v1 applies only to new listings that explicitly opt in."""
import hashlib
import json
import time
from .core import AEPError
from .wallet import canonical

RULE={'rule':'fixed-v1','review_seconds':86400,'undisputed':'seller_full','disputed':'seller_floor_half','trigger':'participant_settle'}

EVIDENCE_RULE=RULE|{'rule':'evidence-v1','evidence_seconds':86400,'response_seconds':86400}

def listing_metadata(body):
    metadata=body.get('metadata',{})
    if not isinstance(metadata,dict) or set(metadata)-{'source_type','observed_at','valid_until','importance','confidence','verification'}:raise AEPError('invalid information metadata')
    for field in ('source_type','verification'):
        if field in metadata and (not isinstance(metadata[field],str) or not 1<=len(metadata[field])<=500):raise AEPError('invalid '+field)
    for field in ('observed_at','valid_until'):
        if field in metadata and (type(metadata[field]) is not int or not 0<=metadata[field]<=2**53-1):raise AEPError('invalid '+field)
    if metadata.get('valid_until',2**53-1)<metadata.get('observed_at',0):raise AEPError('invalid validity interval')
    if 'importance' in metadata and (type(metadata['importance']) is not int or not 1<=metadata['importance']<=5):raise AEPError('importance must be 1..5')
    if 'confidence' in metadata and (type(metadata['confidence']) not in (int,float) or not 0<=metadata['confidence']<=1):raise AEPError('confidence must be 0..1')
    terms=body.get('settlement')
    if terms is not None and terms not in (RULE,EVIDENCE_RULE):raise AEPError('unsupported settlement terms')
    return {'metadata':metadata,'settlement':terms}

def terms_hash(body):return hashlib.sha256(canonical(listing_metadata(body))).hexdigest()

def extras(listing):
    body=json.loads(listing['proof'])['message']['body']
    return listing_metadata(body)|{'terms_hash':terms_hash(body)}

def ensure_schema(engine):
    # Additive migration: existing v0.6 orders keep their original settlement behavior.
    names={r[1] for r in engine.db.execute('PRAGMA table_info(orders)')}
    for name,typ in (('review_deadline','INTEGER'),('dispute','TEXT')):
        if name not in names:engine.db.execute('ALTER TABLE orders ADD COLUMN '+name+' '+typ)

def apply(market,actor,action,body):
    e=market.e;oid=body['order_id']
    if type(oid) is not int or oid<1:raise AEPError('invalid order id')
    order=e._one('SELECT * FROM orders WHERE id=?',(oid,))
    if not order or actor not in (order['buyer'],order['seller']):raise PermissionError('order participant required')
    listing=e._one('SELECT * FROM listings WHERE id=?',(order['listing_id'],))
    terms=extras(listing)['settlement']
    if not terms:raise AEPError('legacy order has no fixed settlement rule')
    if order['state']!='delivered':raise AEPError('delivered order required')
    now=int(time.time())
    if action=='dispute':
        if actor!=order['buyer'] or now>=order['review_deadline'] or order['dispute']:raise AEPError('buyer dispute unavailable')
        reason=body.get('reason')
        if reason not in ('cannot_decrypt','description_mismatch','quality_dispute'):raise AEPError('use a dispute reason code; do not disclose plaintext')
        case_id=None
        if terms['rule']=='evidence-v1':
            case_id=e.contracts.cases.open('order',oid,order['buyer'],order['seller'],reason,terms,order['review_deadline'],{'amount':order['amount'],'terms':terms,'listing_digest':listing['digest'],'review_deadline':order['review_deadline']})
        e.db.execute('UPDATE orders SET dispute=? WHERE id=?',(reason,oid))
        return {'order_id':oid,'state':'delivered','disputed':True}|({'case_id':case_id} if case_id else {})
    if now<order['review_deadline']:raise AEPError('review period still open')
    if order['dispute'] and terms['rule']=='evidence-v1':e.contracts.cases.require_due('order',oid)
    payout=order['amount']//2 if order['dispute'] else order['amount'];refund=order['amount']-payout
    for recipient,amount,suffix in ((order['seller'],payout,'payout'),(order['buyer'],refund,'refund')):
        if amount:
            e.db.execute('UPDATE agents SET balance=balance+? WHERE id=?',(amount,recipient))
            market._ledger(None,recipient,amount,f'order:{oid}:{suffix}')
    state='resolved' if order['dispute'] else 'completed'
    e.db.execute('UPDATE orders SET state=? WHERE id=?',(state,oid))
    if not order['dispute']:e.db.execute('INSERT INTO licenses VALUES (?,?,?,0)',(oid,order['listing_id'],order['buyer']))
    result={'order_id':oid,'state':state,'seller_amount':payout,'refund':refund,'rule':terms['rule']}
    e.contracts.cases.resolve('order',oid,result)
    return result
