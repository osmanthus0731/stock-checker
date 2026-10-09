"""Refresh live Mongo products/pricing from Access after a local Extended JSON backup."""
import argparse
from datetime import date,datetime,timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bson import json_util
import certifi
from dotenv import dotenv_values
from pymongo import MongoClient,UpdateOne
from inventory_platform.domain import access_product,Conflict,Invalid
from inventory_platform.sync import dao_rows


def norm(row): return {re.sub(r'[^a-z0-9]','',str(k).casefold()):v for k,v in row.items()}
def value(row,*names):
    return next((row[n] for n in names if n in row and row[n] is not None),None)
def text(value): return str(value or '').strip()
def number(value):
    if value in (None,''): return None
    try: return float(Decimal(str(value)))
    except Exception: return None
def when(value):
    if isinstance(value,datetime): return value
    if isinstance(value,date): return datetime(value.year,value.month,value.day)
    return None


def product_doc(raw,stamp):
    mapped=access_product(raw);mapped.update(stock_revision=0,access_confirmed={
        'a':mapped['stock_a'],'b':mapped['stock_b'],'total':mapped['stock'],
        'location_a':mapped['location_a'],'location_b':mapped['location_b']},
        sync_status='synced',stock_total_consistent=mapped['stock']==mapped['stock_a']+mapped['stock_b'],
        access_sync_eligible=True,_source='access',_imported_at=stamp)
    mapped['locations']=[{'slot':'A','area':mapped['location_a'],'quantity':mapped['stock_a']},
                         {'slot':'B','area':mapped['location_b'],'quantity':mapped['stock_b']}]
    return mapped


def price_doc(raw,stamp):
    row=norm(raw);code=text(value(row,'pricecd')).upper()
    part=text(value(row,'partid'))
    if not part or code not in ('P','S'): return None
    supplier=text(value(row,'supplier'));currency=text(value(row,'cur','currency')).upper()
    if currency in ('RM','MYR'): currency='MYR'
    effective=when(value(row,'quotedt','effdate'))
    tiers={'S12':number(value(row,'s12')),'S100':number(value(row,'s100')),'S500':number(value(row,'s500')),
           '1K':number(value(row,'1k','s1000')),'3K':number(value(row,'3k','s3000')),
           '5K':number(value(row,'5k','s5000')),'10K':number(value(row,'10k','s10000'))}
    identity={'part_id':part,'pricecd':code,'customer':supplier,'currency':currency,
              'eff_date':effective.isoformat() if effective else '',**tiers}
    key=sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    return {'_id':'access:'+key,'access_key':key,'part_id':part,'pricecd':code,'customer':supplier,
            'currency':currency,'eff_date':effective,**tiers,'_source':'access','_imported_at':stamp}


def backup(db,folder):
    folder.mkdir(parents=True,exist_ok=False)
    counts={}
    for name in ('products','pricing'):
        docs=list(db[name].find());counts[name]=len(docs)
        (folder/f'{name}.json').write_text(json_util.dumps(docs,indent=2),encoding='utf-8')
    return counts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--products-only',action='store_true');args=parser.parse_args()
    env={**dotenv_values(ROOT/'.env')};stamp=datetime.now(timezone.utc);epoch=int(stamp.timestamp())
    access_path=env['ACCESS_DB_PATH']
    products=[];errors={}
    for raw in dao_rows(access_path,env.get('ACCESS_TABLE','ITMMST')):
        try: products.append(product_doc(raw,epoch))
        except (Conflict,Invalid) as exc: errors[str(exc)]=errors.get(str(exc),0)+1
    price_rows=[] if args.products_only else [doc for raw in dao_rows(access_path,env.get('ACCESS_PRICE_TABLE','Cus_Price')) if (doc:=price_doc(raw,epoch))]
    prices=list({doc['_id']:doc for doc in price_rows}.values())
    report={'access_products_valid':len(products),'access_products_skipped':sum(errors.values()),
            'skip_reasons':errors,'access_price_rows':len(price_rows),'access_prices_unique':len(prices),
            'access_price_duplicates':len(price_rows)-len(prices),'applied':False}
    if not args.apply:
        print(json.dumps(report,indent=2));return
    client=MongoClient(env['MONGO_URI'],tlsCAFile=certifi.where(),serverSelectionTimeoutMS=15000)
    try:
        db=client[env.get('MONGO_DB','inventory')];client.admin.command('ping')
        folder=ROOT/'backups'/('mongo-before-access-refresh-'+stamp.strftime('%Y%m%d-%H%M%S'))
        report['backup']=str(folder);report['before']=backup(db,folder)
        if db.sync_events.count_documents({'status':{'$in':['pending','failed','conflict']}}):
            raise RuntimeError('Unresolved website stock events exist; refusing to replace their Access baseline.')
        changes=0;new=0
        for doc in products:
            existing=db.products.find_one({'uid':doc['uid']},{'_id':1,'uid':1,'stock':1,'stock_a':1,'stock_b':1,
                'stock_revision':1,'sync_status':1})
            changed=existing is None or any(existing.get(k)!=doc[k] for k in ('stock','stock_a','stock_b'))
            fields={k:v for k,v in doc.items() if k!='stock_revision'}
            if existing:
                if existing.get('sync_status') in ('pending','failed'):
                    raise RuntimeError('Product has an unresolved sync state; refusing to overwrite it.')
                update={'$set':fields}
                if changed:update['$inc']={'stock_revision':1}
                result=db.products.update_one({'_id':existing['_id'],'stock_revision':existing.get('stock_revision'),
                    'stock':existing.get('stock'),'stock_a':existing.get('stock_a'),'stock_b':existing.get('stock_b')},update)
                if result.matched_count!=1:raise RuntimeError('Product changed during refresh; retry after review.')
                if changed:changes+=1
            else:
                db.products.insert_one(doc);new+=1
        if not args.products_only:
            # Pricing is a complete Access snapshot. Replacing it avoids stale or collapsed supplier rows.
            db.pricing.delete_many({})
            if prices: db.pricing.insert_many(prices,ordered=False)
            db.pricing.create_index([('part_id',1),('pricecd',1)])
        report.update(changed_products=changes,new_products=new)
        report.update(applied=True,after={'products':db.products.count_documents({}),'pricing':db.pricing.count_documents({})})
        (folder/'refresh-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))
    finally: client.close()


if __name__=='__main__': main()
