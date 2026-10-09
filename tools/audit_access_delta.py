"""Read-only summary of differences between Access stock and the website."""
from collections import Counter
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import certifi
from dotenv import dotenv_values
from pymongo import MongoClient
from inventory_platform.sync import dao_rows
from inventory_platform.domain import access_product,Conflict,Invalid


def main():
    config=dotenv_values(ROOT/'.env')
    client=MongoClient(config['MONGO_URI'],tlsCAFile=certifi.where(),serverSelectionTimeoutMS=15000)
    try:
        db=client[config.get('MONGO_DB','inventory')]
        current={str(row['uid']):row for row in db.products.find({}, {'uid':1,'stock':1,'stock_a':1,'stock_b':1,
            'access_confirmed':1,'stock_revision':1,'sync_status':1}) if row.get('uid')}
        stats=Counter();samples=[]
        for raw in dao_rows(config['ACCESS_DB_PATH'],config.get('ACCESS_TABLE','ITMMST')):
            uid=str(raw.get('Part_id') or '').strip()
            existing=current.get(uid)
            if not uid: stats['no_uid']+=1;continue
            try:
                mapped=access_product(raw)
                stats['valid']+=1
                if existing is None: stats['new_valid']+=1
                elif any(existing.get(k)!=mapped[k] for k in ('stock','stock_a','stock_b')):
                    stats['changed_valid']+=1
                    if len(samples)<10:samples.append({'uid':uid,'website':existing.get('stock'),'access':mapped['stock']})
            except (Conflict,Invalid) as exc:
                stats['invalid']+=1;stats['invalid_'+str(exc)]+=1
                raw_total=raw.get('Stock_office')
                if existing and raw_total is not None and str(existing.get('stock'))!=str(raw_total):
                    stats['changed_invalid_total']+=1
        print('access_mtime',Path(config['ACCESS_DB_PATH']).stat().st_mtime)
        for key,count in sorted(stats.items()):print(key,count)
        print('changed_valid_samples',samples)
        print('pending_events',db.sync_events.count_documents({'status':{'$in':['pending','failed','conflict']}}))
    finally:client.close()


if __name__=='__main__':main()
