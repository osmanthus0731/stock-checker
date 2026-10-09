"""Headless acceptance test; isolated mongomock, no Access connection or live writes."""
import logging,os,sys
from datetime import date,timedelta
from pathlib import Path
from threading import Thread
from unittest.mock import patch
import mongomock
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server

os.environ['MONGO_URI']='mongodb://localhost/platform_browser';os.environ['BOOTSTRAP_USERS']='0'
fake=mongomock.MongoClient()
with patch('pymongo.MongoClient',return_value=fake):import app as module
module.app.config.update(TESTING=True,SECRET_KEY='platform-browser',INVENTORY_PLATFORM_ENABLED=True,STOCK_WRITES_ENABLED=False,PLATFORM_CONFIG_WRITES=True)
module.app.extensions['inventory_platform'].transact=lambda fn:fn({})
db=module.db;logging.getLogger('werkzeug').setLevel(logging.ERROR)
db.platform_meta.insert_many([{'_id':'users_ready','ready':True},{'_id':'collection_started','date':'2026-09-01'}])
db.users.insert_many([{'_id':'A','username':'Admin','username_key':'admin','display_name':'Admin','role':'admin','active':True,'account_revision':0,'created_at':'2026-01-01'},
                      {'_id':'W','username':'MsT','username_key':'mst','display_name':'Ms T','role':'worker','active':True,'account_revision':0,'created_at':'2026-01-01'}])
db.inventory_settings.insert_one({'_id':'permissions','roles':{'admin':['stock.edit','users.manage','sync.manage','settings.manage','forecast.manage'],'worker':[]}})
state={'a':8,'b':2,'total':10,'location_a':'Film','location_b':'WH'}
db.products.insert_one({'uid':'P1','name':'Container','readable_id':'M1','category':'Packing','supplier':'Supplier','stock':10,'stock_a':8,'stock_b':2,'location_a':'Film','location_b':'WH','locations':[{'slot':'A','area':'Film','quantity':8},{'slot':'B','area':'WH','quantity':2}],'access_confirmed':state,'access_sync_eligible':True,'stock_revision':0,'sync_status':'synced'})
for i in range(15):
    d=(date(2026,9,1)+timedelta(days=i)).isoformat();db.business_days.insert_one({'_id':d,'complete':True})
    db.stock_movements.insert_one({'_id':f'E{i}','event_id':f'E{i}','uid':'P1','product_name':'Container','category':'Packing','supplier':'Supplier','business_date':d,'timestamp':d+'T01:00:00+00:00','user_id':'A','username':'Admin','source':'website','movement_type':'issued','reason':'issued','changes':[{'location':'Film','previous':10,'change':-1,'new':9}],'before':{'total':10},'after':{'total':9},'demand_units':1,'reference':'SO1','note':'','sync_status':'synced'})
db.forecasts.insert_one({'_id':'F','uid':'P1','generated_at':'2026-10-08','data_cutoff':'2026-10-07','analysis':{'stage':1,'status':'Descriptive estimates','days_recorded':15,'valid_demand_events':15,'coverage':1,'average_daily_demand':1,'models':[]},'replenishment':{'status':'Advisory only','coverage_days':10,'explanation':'Confirmed demand averages 1 unit/day.'}})
db.inventory_sync_control.insert_one({'_id':'worker','heartbeat':'2026-10-09','access':'Connected','last_success':'2026-10-09','duration_seconds':1})
server=make_server('127.0.0.1',0,module.app,threaded=True);Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_port}'
try:
  with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True);page=browser.new_page(viewport={'width':1280,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(url+'/');expect(page.get_by_role('heading',name='Who are you?')).to_be_visible();expect(page.locator('input[type=password]')).to_have_count(0)
    page.get_by_role('button',name='Admin').click();page.get_by_role('button',name='Login').click();page.wait_for_url('**/admin_dashboard')
    page.get_by_role('button',name='Edit Stock').first.click();expect(page.get_by_role('heading',name='Edit Stock')).to_be_visible();expect(page.locator('#stock-form [name=total]')).to_have_value('10');page.locator('#stock-form [name=total]').fill('-5');expect(page.locator('#stock-review')).to_contain_text('Stock_office 10 → -5');expect(page.get_by_role('button',name='Confirm stock update')).to_be_disabled();page.get_by_label('Close').click()
    for label,heading in [('Stock History','Stock History'),('Inventory Analytics','Inventory Analytics'),('Stock Forecasting','Stock Forecasting'),('Inventory Sync','Inventory Sync'),('Users','Users'),('Settings','Settings')]:
        page.locator(f'a[data-tip="{label}"]').click();expect(page.get_by_role('heading',name=heading,exact=True)).to_be_visible();page.wait_for_timeout(150)
    page.locator('a[data-tip="Users"]').click();page.locator('#user-form [name=username]').fill('New Person');page.locator('#user-form [name=display_name]').fill('New Person');page.get_by_role('button',name='Save account').click();expect(page.locator('#users-body')).to_contain_text('New Person')
    page.set_viewport_size({'width':390,'height':844});page.goto(url+'/platform/forecasting');expect(page.locator('.forecast-card')).to_contain_text('Descriptive estimates');assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert db.products.find_one({'uid':'P1'})['stock']==10 and db.stock_movements.count_documents({})==15
    assert not errors,errors
    print('PASS: password-free selector, role navigation, staged stock dialog, history, analytics, forecasting, sync, user management, settings, mobile layout, zero JS errors; stock unchanged.')
    browser.close()
finally:server.shutdown()
