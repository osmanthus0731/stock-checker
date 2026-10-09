from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import pymupdf
import pytest
from purchase_orders.domain import snapshot, ValidationError, purchasing_options
from purchase_orders.document import pdf_bytes, svg_pages
from purchase_orders.repository import Repository, Conflict

HEADERS = {"X-CSRF-Token": "test-csrf"}


def create(client, payload):
    response = client.post('/purchase-orders/api/orders', json=payload, headers=HEADERS)
    assert response.status_code == 201, response.data
    return response.json


def test_amounts_discounts_and_rounding(payload):
    result = snapshot(payload, ['MYR'])
    assert (result['subtotal'], result['total'], result['items'][0]['unit_price']) == ('384.00', '384.00', '1.6000')
    payload.update(discount_type='percent', discount_value='12.50')
    result = snapshot(payload, ['MYR'])
    assert (result['discount'], result['total']) == ('48.00', '336.00')
    payload['items'] = [dict(description='Rounding', quantity='1', unit_price='0.0050')] * 3
    payload.update(discount_type='amount', discount_value='0.01')
    assert snapshot(payload, ['MYR'])['total'] == '0.02'


@pytest.mark.parametrize('field,value', [('quantity','0'),('quantity','-1'),('quantity','NaN'),('quantity','1e3'),('quantity',1.2),('unit_price','Infinity'),('unit_price','1.00001'),('unit_price',True),('quantity','99999999999999999')])
def test_bad_financial_inputs(payload, field, value):
    payload['items'][0][field] = value
    with pytest.raises(ValidationError): snapshot(payload, ['MYR'])


@pytest.mark.parametrize('update', [{'discount_value':'384.01'}, {'discount_type':'percent','discount_value':'100.01'}, {'currency':'XYZ'}, {'date':'2026-02-30'}, {'items':[]}, {'items':[None]}, {'supplier':None}])
def test_bad_documents(payload, update):
    payload.update(update)
    with pytest.raises(ValidationError): snapshot(payload, ['MYR'])


def test_number_collision_and_concurrent_creation(application, payload):
    repo = application.app.extensions['purchase_orders']
    data = snapshot(payload, ['MYR'])
    with patch('purchase_orders.repository.uuid4', side_effect=[SimpleNamespace(hex='a'*32), SimpleNamespace(hex='a'*32), SimpleNamespace(hex='b'*32)]):
        first = repo.create(data, 'admin'); second = repo.create(data, 'admin')
    assert first['number'] != second['number']
    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(lambda _: repo.create(data,'admin')['number'], range(50)))
    assert len(set(numbers)) == 50
    assert all(number.startswith('LOCAL-PO-') for number in numbers)


def test_workflow_snapshots_and_conflicts(client, application, payload):
    application.products_col.insert_one({'uid':'PE1L104-313-UK','name':'Original'})
    before = list(application.products_col.find())
    po = create(client, payload); path = '/purchase-orders/api/orders/' + po['number']
    edit = deepcopy(payload); edit.update(revision=1, terms='Cash', total='0.01')
    response = client.put(path, json=edit, headers=HEADERS)
    assert response.status_code == 200 and response.json['total'] == '384.00'
    assert client.put(path, json=edit, headers=HEADERS).status_code == 409
    assert list(application.products_col.find()) == before
    application.products_col.update_one({}, {'$set':{'name':'Inventory changed'}})
    assert client.get(path).json['items'][0]['description'] == payload['items'][0]['description']
    finalised = client.post(path + '/finalise', json={'revision':2}, headers=HEADERS)
    assert finalised.status_code == 200 and finalised.json['status'] == 'finalised'
    edit['revision'] = 3
    assert client.put(path, json=edit, headers=HEADERS).status_code == 200
    assert client.post(path + '/finalise', json={'revision':4}, headers=HEADERS).status_code == 409
    duplicate = client.post(path + '/duplicate', json={}, headers=HEADERS).json
    assert duplicate['number'] != po['number'] and duplicate['status'] == 'draft'
    assert duplicate['items'] == po['items'] and duplicate['legacy_number'] == ''
    cancelled = client.post(path + '/cancel', json={'revision':4}, headers=HEADERS)
    assert cancelled.status_code == 200 and cancelled.json['status'] == 'cancelled'
    assert client.post(path + '/finalise', json={'revision':5}, headers=HEADERS).status_code == 409
    assert client.post(path + '/cancel', json={'revision':5}, headers=HEADERS).status_code == 409


def test_roles_and_csrf(client, application, payload):
    assert client.post('/purchase-orders/api/orders',json=payload).status_code == 400
    admin_po = create(client,payload)
    with client.session_transaction() as s: s.update(username='worker1',role='worker')
    worker_po = create(client,payload)
    for suffix in ['', '/edit','/print','/pdf']:
        assert client.get('/purchase-orders/' + admin_po['number'] + suffix).status_code == 404
    path = '/purchase-orders/api/orders/' + admin_po['number']
    assert client.get(path).status_code == 404
    assert client.put(path,json={**payload,'revision':1},headers=HEADERS).status_code == 404
    assert client.delete(path,json={'revision':1},headers=HEADERS).status_code == 404
    for action in ('finalise','cancel','duplicate'):
        assert client.post(path+'/'+action,json={'revision':1},headers=HEADERS).status_code == 404
    assert client.post('/purchase-orders/api/preview',json={**payload,'number':admin_po['number']},headers=HEADERS).status_code == 404
    assert client.get('/purchase-orders/api/orders').json['total'] == 1
    assert client.post('/purchase-orders/api/orders/'+worker_po['number']+'/finalise',json={'revision':1},headers=HEADERS).status_code == 200
    with client.session_transaction() as s: s.update(username='worker2')
    assert client.get('/purchase-orders/api/orders/'+worker_po['number']).status_code == 404
    assert client.delete('/purchase-orders/api/orders/'+worker_po['number'],json={'revision':2},headers=HEADERS).status_code == 404
    with client.session_transaction() as s: s.update(role='unknown')
    assert client.get('/purchase-orders/').status_code == 403
    with client.session_transaction() as s: s.clear()
    assert client.get('/purchase-orders/').status_code == 302
    assert client.get('/purchase-orders/api/orders').status_code == 401


def test_inventory_mapping_and_purchasing_only(client, application):
    application.products_col.insert_one({'Part_id':'P-1','Desc':'Container','Mssid':'M-1','Size':'1L','Material':'PE'})
    application.pricing_col.insert_many([
        {'part_id':'P-1','pricecd':'S','currency':'MYR','S12':99},
        {'part_id':'P-1','pricecd':'P','currency':'MYR','S12':1.2345,'1K':0.9,'eff_date':datetime(2026,1,1)},
        {'part_id':'M-1','priced':'P','currency':'MYR','S500':0},
        {'part_id':'P-1','pricecd':'P','currency':'USD','S12':2},
        {'part_id':'P-1','pricecd':'P','S12':3},
        {'part_id':'P-1','pricecd':'P','currency':'MYR','S12':4,'eff_date':datetime(2030,1,1)},
    ])
    for query in ('P-1','Container','M-1'):
        result = client.get('/purchase-orders/api/products?q='+query).json[0]
        assert result == {'product_id':'P-1','description':'Container','mssid':'M-1','size':'1L','material':'PE'}
    options = client.get('/purchase-orders/api/prices?uid=P-1&currency=MYR&date=2026-10-09').json
    assert {v['unit_price'] for v in options} == {'1.2345','0.9000','0.0000'}
    assert client.get('/purchase-orders/api/prices?uid=missing').json == []
    assert client.get('/purchase-orders/api/products?q=%5B').status_code == 200


def test_search_sort_pagination(client, payload):
    first = create(client,payload)
    payload['date'] = '2026-01-01'; payload['supplier']['name'] = 'Old Supplier'
    second = create(client,payload)
    assert client.get('/purchase-orders/api/orders?sort=oldest').json['orders'][0]['number'] == second['number']
    assert client.get('/purchase-orders/api/orders?sort=newest').json['orders'][0]['number'] == first['number']
    assert client.get('/purchase-orders/api/orders?q=Old').json['total'] == 1
    assert client.get('/purchase-orders/api/orders?status=finalised').json['total'] == 0
    assert client.get('/purchase-orders/api/orders?page=2').json['orders'] == []


def test_pdf_a4_pagination_and_svg_escaping(payload):
    doc = snapshot(payload,['MYR']); doc.update(number='TEST-PO',status='finalised')
    doc['items'] = [deepcopy(doc['items'][0]) for _ in range(32)]
    doc['items'][0]['description'] = '<script>alert("unsafe")</script> & test'
    pdf = pymupdf.open(stream=pdf_bytes(doc),filetype='pdf')
    assert len(pdf) > 1
    assert all(abs(page.rect.width-595.32)<.1 and abs(page.rect.height-841.92)<.1 for page in pdf)
    for i, page in enumerate(pdf):
        text = page.get_text()
        assert 'PURCHASE ORDER' in text and 'Quantity' in text
        assert f'Page {i+1} of {len(pdf)}' in text
        assert ('Authorised Signature' in text) == (i == len(pdf)-1)
    assert sum(page.get_text().count('Bottle and Cap') for page in pdf) == 32
    pages = svg_pages(doc)
    assert len(pages) == len(pdf) and '<script>' not in pages[0] and '&lt;script&gt;' in pages[0]


def test_document_routes_and_existing_pages(client, application, payload):
    application.products_col.insert_one({'uid':'P1','name':'Test product','stock':10})
    po = create(client,payload); path = '/purchase-orders/' + po['number']
    for url in ['/purchase-orders/', '/purchase-orders/new', path, path+'/edit',path+'/print', '/admin_dashboard','/calculator','/search/admin']:
        response = client.get(url)
        assert response.status_code == 200, (url,response.data)
    printed = client.get(path+'/print').data.decode()
    assert 'sidebar' not in printed and '<svg' in printed
    response = client.get(path+'/pdf')
    assert response.status_code == 200 and response.data.startswith(b'%PDF')
    assert 'attachment' in response.headers['Content-Disposition']
    preview = client.post('/purchase-orders/api/preview',json=payload,headers=HEADERS)
    assert preview.status_code == 200 and preview.json['total'] == '384.00'
    with client.session_transaction() as s: s.update(role='worker',username='worker')
    assert client.get('/index').status_code == 200
    assert client.get('/search/worker').status_code == 200


def test_request_validation_and_storage_failure(client, application, payload):
    assert client.post('/purchase-orders/api/orders',json=[],headers=HEADERS).status_code == 400
    doc = create(client,payload)
    assert client.post('/purchase-orders/api/orders/'+doc['number']+'/cancel',json={'revision':True},headers=HEADERS).status_code == 400
    from pymongo.errors import ServerSelectionTimeoutError
    with patch.object(application.app.extensions['purchase_orders'].collection,'find',side_effect=ServerSelectionTimeoutError('test')):
        response = client.get('/purchase-orders/api/orders')
        assert response.status_code == 503 and 'unavailable' in response.json['error']


def test_indexes_only_touch_po_collection(application):
    before = set(application.db.list_collection_names())
    result = application.app.test_cli_runner().invoke(args=['po-init-indexes'])
    assert result.exit_code == 0
    assert set(application.db.list_collection_names()) - before == {'purchase_orders'}
    assert len(application.db.purchase_orders.index_information()) == 4


def test_oversize_rows_are_rejected_before_persistence(client, application, payload):
    payload['items'][0]['notes'] = '\n'.join(['x'] * 100)
    response = client.post('/purchase-orders/api/orders',json=payload,headers=HEADERS)
    assert response.status_code == 400 and 'too tall' in response.json['error']
    assert application.db.purchase_orders.count_documents({}) == 0


@pytest.mark.parametrize('status', ['draft', 'finalised', 'cancelled'])
@pytest.mark.parametrize('role', ['admin', 'worker'])
def test_edit_and_delete_all_statuses(client, application, payload, status, role):
    with client.session_transaction() as s: s.update(username='owner',role='worker')
    po = create(client,payload)
    path = '/purchase-orders/api/orders/' + po['number']
    if status != 'draft':
        action = 'finalise' if status == 'finalised' else 'cancel'
        po = client.post(path+'/'+action,json={'revision':po['revision']},headers=HEADERS).json
    with client.session_transaction() as s:
        s.update(username='admin' if role == 'admin' else 'owner',role=role)
    application.products_col.insert_one({'uid':'untouched','stock':123})
    application.pricing_col.insert_one({'part_id':'untouched','S12':4})
    inventory_before = list(application.products_col.find())
    pricing_before = list(application.pricing_col.find())
    assert client.get('/purchase-orders/'+po['number']+'/edit').status_code == 200
    changed = {**payload,'revision':po['revision'],'terms':'Updated terms','status':'draft'}
    response = client.put(path,json=changed,headers=HEADERS)
    assert response.status_code == 200
    edited = response.json
    assert edited['status'] == status and edited['terms'] == 'Updated terms'
    assert edited['number'] == po['number'] and edited['created_by'] == 'owner'
    assert edited['history'][-1]['action'] == 'edited'
    assert edited['revision'] == po['revision'] + 1
    assert client.put(path,json=changed,headers=HEADERS).status_code == 409
    preview = client.post('/purchase-orders/api/preview',json={**changed,'number':po['number']},headers=HEADERS)
    assert preview.status_code == 200
    assert ('CANCELLED' in ''.join(preview.json['pages'])) == (status == 'cancelled')
    assert client.delete(path,json={'revision':po['revision']},headers=HEADERS).status_code == 409
    assert client.delete(path,json={'revision':edited['revision']}).status_code == 400
    assert client.delete(path,json={},headers=HEADERS).status_code == 400
    assert client.get(path).status_code == 200
    deleted = client.delete(path,json={'revision':edited['revision']},headers=HEADERS)
    assert deleted.status_code == 200 and deleted.json['deleted'] is True
    assert application.db.purchase_orders.find_one({'_id':po['number']}) is None
    assert client.get(path).status_code == 404
    assert client.get('/purchase-orders/api/orders').json['total'] == 0
    for suffix in ['', '/edit', '/print', '/pdf']:
        assert client.get('/purchase-orders/'+po['number']+suffix).status_code == 404
    assert list(application.products_col.find()) == inventory_before
    assert list(application.pricing_col.find()) == pricing_before
