from copy import deepcopy
from datetime import date,timedelta
from pathlib import Path
from uuid import uuid4
import json
import mongomock
import pytest
from inventory_platform.domain import adjustment,access_product,Conflict,Invalid
from inventory_platform.store import Store
from inventory_platform.accounts import migrate_users,save_user
from inventory_platform.forecasting import aggregate,forecast,replenishment
from inventory_platform.sync import Journal,Worker,staging_allowed


def direct(fn): return fn({})


@pytest.fixture
def db(): return mongomock.MongoClient().inventory


@pytest.fixture
def mapped():
    return {'uid':'P1','name':'Product','readable_id':'M1','category':'C','supplier':'S','location_a':'Film','stock_a':8,'location_b':'WH','stock_b':2,'stock':10,
            'locations':[{'slot':'A','area':'Film','quantity':8},{'slot':'B','area':'WH','quantity':2}],
            'access_confirmed':{'a':8,'b':2,'total':10,'location_a':'Film','location_b':'WH'},'access_sync_eligible':True,'stock_revision':0,'sync_status':'synced'}


def request(**kw): return {'event_id':str(uuid4()),'revision':0,'mode':'set','a':'7','b':'3','total':'10','reason':'transfer','reference':'R1','note':'move',**kw}


def test_access_mapping_accepts_independent_and_negative_stock():
    row={'Part_id':'P1','Desc':'Name','Mssid':'M','Stock_office':10,'Loc_film_box':'A','StockA':8,'Loc_wh':'B','StockB':2,'Cat':'C','Supplr':'S'}
    p=access_product(row)
    assert (p['stock_a'],p['stock_b'],p['stock'],p['supplier'])==(8,2,10,'S')
    row.update(Stock_office=-11,StockA=-8,StockB=2)
    changed=access_product(row)
    assert (changed['stock_a'],changed['stock_b'],changed['stock'])==(-8,2,-11)


def test_stock_rules(mapped):
    before,after,changes,reason,note=adjustment(mapped,request())
    assert before['total']==after['total']==10 and [c['change'] for c in changes]==[-1,1]
    with pytest.raises(Invalid): adjustment(mapped,request(a='9',b='3'))
    with pytest.raises(Invalid): adjustment(mapped,request(reason='issued',a='9',b='1'))
    with pytest.raises(Invalid): adjustment(mapped,request(reason='other',note=''))
    before,after,changes,_,_=adjustment(mapped,request(a='8',b='2',total='-5',reason='correction'))
    assert after['total']==-5 and after['a']==8 and changes==[{'slot':'Office','location':'Office total','previous':10,'change':-15,'new':-5}]
    negative={**mapped,'stock_a':-8,'stock_b':2,'stock':-11}
    assert adjustment(negative,request(a='-9',b='2',total='-11',reason='correction'))[1]['a']==-9


def test_atomic_change_idempotency_conflict_and_readonly_sources(db,mapped):
    db.products.insert_one(deepcopy(mapped));store=Store(db,direct);payload=request();actor={'id':'U1','username':'Admin'}
    movement=store.change('P1',payload,actor)
    assert movement['demand_units']==0 and db.products.find_one()['stock']==10
    assert db.stock_movements.count_documents({})==db.sync_events.count_documents({})==1
    assert store.change('P1',payload,actor)['_id']==movement['_id']
    with pytest.raises(Conflict): store.change('P1',{**payload,'note':'different'},actor)
    with pytest.raises(Conflict): store.change('P1',request(),actor)
    assert db.products.find_one()['stock_revision']==1


def test_ineligible_access_product_cannot_be_edited(db,mapped):
    product={**mapped,'access_sync_eligible':False}
    db.products.insert_one(product)
    with pytest.raises(Conflict,match='validation'):
        Store(db,direct).change('P1',request(),{'id':'U1','username':'Admin'})
    assert db.products.find_one({'uid':'P1'})['stock']==10
    assert db.sync_events.count_documents({})==0


def test_issue_is_demand_and_access_observation_is_not(db,mapped):
    db.products.insert_one(deepcopy(mapped));store=Store(db,direct)
    issue=request(a='6',b='2',total='8',reason='issued');m=store.change('P1',issue,{'id':'U','username':'A'})
    assert m['demand_units']==2
    event=db.sync_events.find_one();store.acknowledge(event)
    observed={**mapped,'stock_a':5,'stock_b':3,'stock':8,'locations':[{'slot':'A','area':'Film','quantity':5},{'slot':'B','area':'WH','quantity':3}]}
    assert store.observe(observed)=='observed'
    correction=db.stock_movements.find_one({'source':'access'})
    assert correction['movement_type']=='observed_correction' and correction['demand_units']==0


def test_account_migration_casefold_and_last_admin(db):
    db.users.insert_one({'_id':'old','username':'Admin','role':'admin','active':True})
    plan=migrate_users(db,False);assert 'Admin' not in plan['create'] and 'MsT' in plan['create']
    migrate_users(db,True);assert db.users.count_documents({})==6
    assert db.users.count_documents({'role':'worker'})==0
    store=Store(db,direct);admin=db.users.find_one({'username':'Admin'});actor={'id':'old','username':'Admin'}
    with pytest.raises(Invalid): save_user(store,{'username':'Admin','display_name':'Admin','role':'worker','active':True,'revision':0},actor,'old')
    with pytest.raises(Conflict): save_user(store,{'username':'ADMIN','display_name':'X','role':'admin','active':True},actor)
    saved=save_user(store,{'username':'New admin','display_name':'New','role':'admin','active':True},actor)
    assert saved['role']=='admin'


def test_missing_days_stockouts_forecast_backtest_and_replenishment():
    start=date(2026,1,1);end=start+timedelta(days=39);coverage={(start+timedelta(days=i)).isoformat():True for i in range(40)}
    coverage[(start+timedelta(days=8)).isoformat()]=False
    movements=[]
    for i in range(40):
        d=(start+timedelta(days=i)).isoformat();movements.append({'uid':'P1','business_date':d,'movement_type':'issued','demand_units':2,'before':{'total':10},'after':{'total':8},'timestamp':d,'changes':[]})
    movements[9]['after']['total']=0
    rows=aggregate(movements,coverage,start.isoformat(),end.isoformat(),'P1')
    assert rows[8]['demand'] is None and not rows[9]['usable']
    # A 30-day clean suffix after gaps is sufficient for basic baselines; excluded dates stay visible.
    result=forecast(rows);assert result['stage']==2 and result['exclusions']['missing']==1 and result['exclusions']['censored']==1
    clean=aggregate(movements,{d:True for d in coverage},start.isoformat(),end.isoformat(),'P1')
    clean[9]['censored']=False;clean[9]['usable']=True
    result=forecast(clean);assert result['stage']==2 and result['models'][0]['folds']>0
    plan=replenishment(20,result,{'lead_time':3,'review_period':4,'minimum_order':5,'order_multiple':5,'service_level':95,'reorder_threshold':5})
    assert plan['status']=='Advisory only' and plan['incomplete_position'] is True


class FakeAccess:
    write_enabled=True
    def __init__(self,state,fail=False): self.state=state;self.writes=0;self.fail=fail
    def read(self,uid): return {'uid':uid,'name':'P','readable_id':'','category':'','supplier':'','stock_a':self.state['a'],'stock_b':self.state['b'],'stock':self.state['total'],'location_a':'Film','location_b':'WH'}
    def compare_and_set(self,uid,before,after):
        if self.state!=before: raise Conflict('changed')
        self.state=deepcopy(after);self.writes+=1
        if self.fail: self.fail=False;raise RuntimeError('ack uncertainty')


def test_worker_commit_ack_crash_does_not_repeat(tmp_path,db,mapped):
    db.products.insert_one(deepcopy(mapped));store=Store(db,direct);payload=request()
    store.change('P1',payload,{'id':'U','username':'A'});event=db.sync_events.find_one()
    access=FakeAccess(deepcopy(event['before']),True);worker=Worker(store,access,Journal(tmp_path/'receipts.sqlite'))
    with pytest.raises(RuntimeError): worker.process(event)
    assert access.writes==1
    event=db.sync_events.find_one();assert worker.process(event)=='synced' and access.writes==1


def test_worker_offline_reconnect_and_restart(tmp_path,db,mapped):
    db.products.insert_one(deepcopy(mapped));store=Store(db,direct);store.change('P1',request(),{'id':'U','username':'A'})
    event=db.sync_events.find_one()
    class Offline(FakeAccess):
        def read(self,uid): raise OSError('offline')
    with pytest.raises(OSError): Worker(store,Offline(deepcopy(event['before'])),Journal(tmp_path/'j.sqlite')).process(event)
    failed=db.sync_events.find_one();assert failed['status']=='failed'
    access=FakeAccess(deepcopy(event['before']))
    assert Worker(store,access,Journal(tmp_path/'j.sqlite')).process(failed)=='synced'
    # A restarted worker sees the absolute target and never applies the stock delta twice.
    synced=db.sync_events.find_one();assert Worker(store,access,Journal(tmp_path/'j.sqlite')).process(synced)=='synced'
    assert access.writes==1


def test_worker_control_does_not_collide_with_existing_sync_state(tmp_path,db,mapped):
    db.sync_state.create_index('uid',unique=True)
    db.sync_state.insert_one({'uid':'legacy-product','stock':3})
    Worker(Store(db,direct),FakeAccess(mapped['access_confirmed']),Journal(tmp_path/'j.sqlite')).cycle(False)
    assert db.sync_state.count_documents({})==1
    assert db.inventory_sync_control.find_one({'_id':'worker'})['access']=='Connected'


def test_worker_conflict_and_staging_gate(tmp_path,db,mapped):
    db.products.insert_one(deepcopy(mapped));store=Store(db,direct);store.change('P1',request(),{'id':'U','username':'A'});event=db.sync_events.find_one()
    other={**event['before'],'a':9,'b':1}
    worker=Worker(store,FakeAccess(other),Journal(tmp_path/'j.sqlite'))
    assert worker.process(event)=='conflict' and db.sync_events.find_one()['observed_access']==other
    with pytest.raises(Invalid): staging_allowed({'ACCESS_WRITEBACK_ENABLED':'1','SYNC_ENVIRONMENT':'staging'},'x.mdb')
    assert staging_allowed({'ACCESS_WRITEBACK_ENABLED':'0'},'x.mdb') is False


def test_platform_routes_are_gated_and_do_not_mutate_stock(application):
    application.app.config.update(INVENTORY_PLATFORM_ENABLED=True,STOCK_WRITES_ENABLED=False,PLATFORM_CONFIG_WRITES=False)
    application.db.users.insert_one({'_id':'U1','username':'Admin','username_key':'admin','display_name':'Admin','role':'admin','active':True})
    application.db.inventory_settings.insert_one({'_id':'permissions','roles':{'admin':['stock.edit','users.manage','sync.manage','settings.manage'],'worker':[]}})
    application.products_col.insert_one({'uid':'P1','name':'P','readable_id':'','stock':10,'stock_a':8,'stock_b':2,'location_a':'Film','location_b':'WH','stock_revision':0,'access_sync_eligible':True,'access_confirmed':{'a':8,'b':2,'total':10,'location_a':'Film','location_b':'WH'}})
    client=application.app.test_client()
    with client.session_transaction() as s:s.update(username='Admin',role='admin',user_id='U1',platform_csrf='csrf')
    assert client.get('/platform/history').status_code==200
    assert client.get('/platform/api/stock/P1').json['writable'] is False
    response=client.post('/platform/api/stock/P1',json=request(),headers={'X-CSRF-Token':'csrf'})
    assert response.status_code==409 and application.products_col.find_one({'uid':'P1'})['stock']==10
    assert application.db.stock_movements.count_documents({})==0
    assert client.post('/platform/api/sync/wake',json={},headers={'X-CSRF-Token':'csrf'}).status_code==409


def test_password_free_login_and_worker_permissions(application):
    application.app.config.update(INVENTORY_PLATFORM_ENABLED=True)
    application.db.users.insert_one({'_id':'W','username':'MsT','display_name':'Ms T','role':'worker','active':True})
    client=application.app.test_client();page=client.get('/')
    assert page.status_code==200 and b'Who are you?' in page.data and b'password' in page.data.lower() # explanatory text only
    with client.session_transaction() as s:token=s['platform_csrf']
    response=client.post('/',data={'username':'MsT','platform_csrf':token})
    assert response.status_code==302
    assert client.get('/platform/users').status_code==403
    assert client.get('/platform/history').status_code==200
