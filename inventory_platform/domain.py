"""Stock invariants shared by the website and Windows worker."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re

REASONS = {'received':'Stock received', 'issued':'Stock sold / issued', 'damaged':'Damaged stock',
           'correction':'Stock correction', 'transfer':'Stock transfer', 'return':'Customer return', 'other':'Other'}
PERMISSIONS = ('stock.edit','users.manage','sync.manage','settings.manage','forecast.manage')


class Invalid(ValueError): pass
class Conflict(ValueError): pass


def now(): return datetime.now(timezone.utc)
def stamp(): return now().isoformat()
def business_date(value=None): return (value or now()).astimezone(ZoneInfo('Asia/Kuala_Lumpur')).date().isoformat()


def bounded_text(value, label, limit=200, required=False):
    if not isinstance(value,str) or len(value.strip()) > limit or (required and not value.strip()):
        raise Invalid(f'{label}: enter {"nonempty " if required else ""}text, at most {limit} characters.')
    if any(ord(c)<32 and c not in '\n\t' for c in value): raise Invalid(f'{label}: invalid characters.')
    return value.strip()


def integer(value, label, minimum=0, maximum=2_000_000_000):
    if isinstance(value,bool) or not re.fullmatch(r'-?\d{1,12}',str(value)):
        raise Invalid(f'{label} must be a whole number.')
    result=int(value)
    if not minimum<=result<=maximum: raise Invalid(f'{label} must be between {minimum} and {maximum}.')
    return result


def username(value):
    value=bounded_text(value,'Username',60,True)
    if any(c in value for c in '\n\t'): raise Invalid('Username must be on one line.')
    return value, value.casefold()


def stock_state(product):
    required=('stock_a','stock_b','stock','location_a','location_b')
    if any(k not in product for k in required):
        raise Conflict('Access stock fields are not confirmed. Run the staged sync baseline before editing.')
    a=integer(product['stock_a'],'StockA',-2_000_000_000); b=integer(product['stock_b'],'StockB',-2_000_000_000)
    total=integer(product['stock'],'Stock_office',-2_000_000_000)
    return {'a':a,'b':b,'total':total,'location_a':str(product['location_a'] or ''),'location_b':str(product['location_b'] or '')}


def cloud_fields(state):
    return {'stock_a':state['a'],'stock_b':state['b'],'stock':state['total'],
            'location_a':state['location_a'],'location_b':state['location_b'],
            'locations':[{'slot':'A','area':state['location_a'],'quantity':state['a']},
                         {'slot':'B','area':state['location_b'],'quantity':state['b']}]}


def adjustment(product, data):
    before=stock_state(product)
    reason=data.get('reason')
    if reason not in REASONS: raise Invalid('Select a stock movement reason.')
    note=bounded_text(data.get('note',''),'Note',1000,reason=='other')
    mode=data.get('mode','set')
    if mode not in ('set','adjust'): raise Invalid('Select new quantities or adjustments.')
    a=integer(data.get('a'),'StockA',-2_000_000_000)
    b=integer(data.get('b'),'StockB',-2_000_000_000)
    total=integer(data.get('total',0 if mode=='adjust' else before['total']),'Stock_office',-2_000_000_000)
    if mode=='adjust': a+=before['a']; b+=before['b']; total+=before['total']
    a=integer(a,'New StockA',-2_000_000_000); b=integer(b,'New StockB',-2_000_000_000)
    total=integer(total,'New Stock_office',-2_000_000_000)
    after={**before,'a':a,'b':b,'total':total}
    deltas=[a-before['a'],b-before['b'],total-before['total']]
    if deltas==[0,0,0]: raise Invalid('No stock quantities changed.')
    if reason=='transfer' and (deltas[0]+deltas[1]!=0 or not deltas[0] or not deltas[1] or deltas[2]!=0):
        raise Invalid('A transfer must move the same quantity between A and B without changing Stock_office.')
    if reason in ('received','return') and any(d<0 for d in deltas): raise Invalid('Receipts/returns cannot reduce stock.')
    if reason in ('issued','damaged') and any(d>0 for d in deltas): raise Invalid('Issues/damage cannot increase stock.')
    changes=[{'slot':slot,'location':before['location_'+slot.lower()], 'previous':before[key],
              'change':after[key]-before[key], 'new':after[key]} for slot,key in (('A','a'),('B','b')) if before[key]!=after[key]]
    if any(not c['location'] for c in changes): raise Invalid('A changed stock slot must have a confirmed location.')
    if before['total']!=after['total']:
        changes.append({'slot':'Office','location':'Office total','previous':before['total'],
                        'change':after['total']-before['total'],'new':after['total']})
    return before,after,changes,reason,note


def access_product(row):
    """Explicit mapping; never infer missing quantities or collapse the warehouse slot."""
    r={str(k).strip().casefold():v for k,v in row.items()}
    def required(k):
        if k not in r or r[k] is None: raise Invalid(f'Access is missing {k}.')
        return r[k]
    a=integer(required('stocka'),'StockA',-2_000_000_000)
    b=integer(required('stockb'),'StockB',-2_000_000_000)
    total=integer(required('stock_office'),'Stock_office',-2_000_000_000)
    state={'a':a,'b':b,'total':total,'location_a':str(r.get('loc_film_box') or ''), 'location_b':str(r.get('loc_wh') or '')}
    return {'uid':str(required('part_id')).strip(),'name':str(r.get('desc') or ''),'readable_id':str(r.get('mssid') or ''),
            'category':str(r.get('cat') or 'Uncategorized'),'supplier':str(r.get('supplr') or ''), **cloud_fields(state)}
