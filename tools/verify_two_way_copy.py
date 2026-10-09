"""Exercise both sync directions using only an Access copy and isolated Mongo memory."""
from copy import deepcopy
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import mongomock
from inventory_platform.domain import stock_state
from inventory_platform.store import Store
from inventory_platform.sync import DaoAccessAdapter,Journal,Worker


def main():
    copy=sorted((ROOT/'backups').glob('access-staging-*/ORDER_Data.mdb'))[-1].resolve()
    from dotenv import dotenv_values
    production=Path(dotenv_values(ROOT/'.env')['ACCESS_DB_PATH']).resolve()
    if copy==production:raise RuntimeError('Refusing to test the production database.')
    uid='PP-PM05W-24-T1';access=DaoAccessAdapter(copy,True)
    original=stock_state(access.read(uid));current=deepcopy(original)
    if original['a']<1 or original['b']<1:raise RuntimeError('Test item needs stock in both locations.')
    db=mongomock.MongoClient().inventory
    mapped=access.read(uid);mapped.update(stock_revision=0,access_confirmed=original,access_sync_eligible=True,sync_status='synced')
    db.products.insert_one(mapped)
    store=Store(db,lambda fn:fn({}))
    try:
        after={**original,'a':original['a']-1,'b':original['b']+1,'total':-1}
        store.change(uid,{'event_id':str(uuid4()),'revision':0,'mode':'set','a':str(after['a']),
            'b':str(after['b']),'total':str(after['total']),'reason':'correction','reference':'staging','note':''},
            {'id':'staging','username':'Staging'})
        event=db.sync_events.find_one()
        journal=Journal(copy.parent/'two-way-receipts.sqlite')
        if Worker(store,access,journal).process(event)!='synced':raise AssertionError('Cloud to Access failed.')
        current=stock_state(access.read(uid))
        if current!=after:raise AssertionError('Cloud to Access readback failed.')
        if Worker(store,access,Journal(copy.parent/'two-way-receipts.sqlite')).process(event)!='synced':
            raise AssertionError('Restart idempotency failed.')
        external={**after,'b':after['b']-1,'total':-2}
        access.compare_and_set(uid,after,external);current=external
        if store.observe(access.read(uid))!='observed':raise AssertionError('Access to cloud observation failed.')
        if stock_state(db.products.find_one({'uid':uid}))!=external:raise AssertionError('Cloud stock did not match Access change.')
        if db.stock_movements.find_one({'source':'access'})['demand_units']!=0:
            raise AssertionError('Unknown Access change was incorrectly counted as confirmed demand.')
        print('cloud_to_access=passed; access_to_cloud=passed; restart=passed')
    finally:
        if current!=original:access.compare_and_set(uid,current,original)
        if stock_state(access.read(uid))!=original:raise AssertionError('Staging stock was not restored.')
        print('staging_stock_restored=passed')


if __name__=='__main__':main()
