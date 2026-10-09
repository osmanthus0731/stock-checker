"""Browser acceptance check against an isolated in-memory app; run directly, not on live data."""
import os
import logging
from copy import deepcopy
from pathlib import Path
from threading import Thread
from unittest.mock import patch
import mongomock
import pymupdf
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

os.environ['MONGO_URI'] = 'mongodb://localhost/po_browser_test'
os.environ['BOOTSTRAP_USERS'] = '0'
with patch('pymongo.MongoClient', return_value=mongomock.MongoClient()):
    import app as module

module.app.config.update(TESTING=True, SECRET_KEY='browser-test-secret', INVENTORY_PLATFORM_ENABLED=False)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
module.products_col.insert_one({'uid':'PE1L104-313-UK','name':'PE Container (Phrm/313/Round/UK) 1kg W/Cap & Insert','readable_id':'313','size':'X','material':'PE','stock':500})
module.pricing_col.insert_many([
    {'part_id':'PE1L104-313-UK','pricecd':'P','currency':'MYR','S12':1.6,'1K':1.5},
    {'part_id':'PE1L104-313-UK','pricecd':'S','currency':'MYR','S12':9.9},
])
server = make_server('127.0.0.1', 0, module.app, threaded=True)
thread = Thread(target=server.serve_forever, daemon=True); thread.start()
url = f'http://127.0.0.1:{server.server_port}'
artifacts = Path('test-artifacts'); artifacts.mkdir(exist_ok=True)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width':1440,'height':1000})
        cookie = module.app.session_interface.get_signing_serializer(module.app).dumps({'username':'browser-worker','role':'worker'})
        context.add_cookies([{'name':'session','value':cookie,'url':url}])
        page = context.new_page(); errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        page.goto(url+'/index')
        expect(page.locator('aside#sidebar')).to_have_css('background-color','rgb(17, 20, 24)')
        page.locator('a[data-tip="Purchase Orders"]').click()
        page.get_by_role('link', name='Create Purchase Order').click()
        page.locator('[name="supplier.name"]').fill('CASH')
        page.locator('[name="supplier.details"]').fill('For purchasing')
        page.locator('#po-search').fill('313')
        page.locator('#po-results button').first.click()
        expect(page.locator('[data-field="product_id"]')).to_have_value('PE1L104-313-UK')
        expect(page.locator('[data-field="size"]')).to_have_value('X')
        expect(page.locator('.po-prices')).to_be_enabled()
        page.locator('.po-prices').select_option('1.6000')
        page.locator('[data-field="quantity"]').fill('240')
        page.locator('[data-field="notes"]').fill('ID:313Z-S101/16; Bottle and Cap')
        expect(page.locator('#po-total')).to_have_text('384.00')
        expect(page.locator('#po-preview svg')).to_have_count(1)
        page.screenshot(path=str(artifacts/'po-editor-desktop.png'), full_page=True)
        assert page.evaluate("POMath.calculate([{quantity:'1',unit_price:'0.0050'},{quantity:'1',unit_price:'0.0050'}], 'percent','25')") == {'amounts':['0.01','0.01'],'subtotal':'0.02','discount':'0.01','total':'0.01'}
        page.get_by_role('button',name='Save draft',exact=True).click()
        page.wait_for_url('**/purchase-orders/LOCAL-PO-*')
        saved_url = page.url
        page.get_by_role('link',name='Edit',exact=True).click()
        expect(page.locator('[data-field="unit_price"]')).to_have_value('1.6000')
        page.locator('[name="discount_value"]').fill('4.00')
        expect(page.locator('#po-total')).to_have_text('380.00')
        page.set_viewport_size({'width':390,'height':844})
        expect(page.locator('#po-preview svg')).to_have_count(1)
        page.screenshot(path=str(artifacts/'po-editor-mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.get_by_role('button',name='Save changes',exact=True).click()
        page.wait_for_url(saved_url)
        page.get_by_role('button',name='Finalise',exact=True).click()
        expect(page.locator('.po-badge')).to_have_text('Finalised')
        expect(page.get_by_role('link',name='Edit',exact=True)).to_have_count(1)
        with page.expect_download() as info: page.get_by_role('link',name='Export PDF',exact=True).click()
        info.value.save_as(artifacts/'purchase-order.pdf')
        doc = pymupdf.open(artifacts/'purchase-order.pdf')
        assert '380.00' in doc[0].get_text() and 'Authorised Signature' in doc[0].get_text()
        doc[0].get_pixmap(matrix=pymupdf.Matrix(1.3,1.3)).save(artifacts/'purchase-order.png')
        page.goto(saved_url+'/print')
        page.pdf(path=str(artifacts/'browser-print.pdf'), prefer_css_page_size=True,print_background=True)
        printed = pymupdf.open(artifacts/'browser-print.pdf')
        assert len(printed)==len(doc) and '380.00' in printed[0].get_text()
        page.goto(saved_url)
        page.get_by_role('button',name='Duplicate',exact=True).click()
        expect(page.locator('.po-badge')).to_have_text('Draft')
        page.get_by_role('button',name='Cancel PO',exact=True).click()
        expect(page.locator('.po-badge')).to_have_text('Cancelled')
        cancelled_url = page.url
        page.goto(url+'/purchase-orders/')
        cancelled_row = page.locator('#po-orders tr').filter(has=page.locator('.po-badge.cancelled'))
        cancelled_row.get_by_role('link',name='Edit',exact=True).click()
        expect(page.locator('.po-badge')).to_have_text('Cancelled')
        page.locator('[name="terms"]').fill('Corrected cancelled record')
        expect(page.locator('#po-preview svg')).to_contain_text('CANCELLED')
        page.get_by_role('button',name='Save changes',exact=True).click()
        page.wait_for_url(cancelled_url)
        expect(page.locator('.po-badge')).to_have_text('Cancelled')
        page.get_by_role('button',name='Delete PO',exact=True).click()
        page.wait_for_url('**/purchase-orders/?deleted=1')
        expect(page.locator('#po-message')).to_contain_text('Purchase order deleted.')
        expect(page.locator('#po-orders tr')).to_have_count(1)
        # The same page boundaries must survive actual browser printing on longer orders.
        token = page.locator('.po-app').get_attribute('data-csrf')
        original = module.app.extensions['purchase_orders'].collection.find_one({'status':'finalised'})
        long_order = deepcopy(original); long_order['items'] *= 32
        response = context.request.post(url+'/purchase-orders/api/orders',data=long_order,headers={'X-CSRF-Token':token})
        assert response.status == 201
        number = response.json()['number']
        pdf_response = context.request.get(url+'/purchase-orders/'+number+'/pdf')
        long_pdf = pymupdf.open(stream=pdf_response.body(),filetype='pdf')
        page.goto(url+'/purchase-orders/'+number+'/print')
        long_browser = pymupdf.open(stream=page.pdf(prefer_css_page_size=True,print_background=True),filetype='pdf')
        assert len(long_pdf) > 1 and len(long_pdf) == len(long_browser)
        for i in range(len(long_pdf)):
            assert ('Authorised Signature' in long_pdf[i].get_text()) == ('Authorised Signature' in long_browser[i].get_text()) == (i == len(long_pdf)-1)
            assert 'Quantity' in long_browser[i].get_text()
        page.goto(url+'/purchase-orders/')
        draft_row = page.locator('#po-orders tr').filter(has_text=number)
        draft_row.get_by_role('button',name='Delete',exact=True).click()
        expect(page.locator('#po-message')).to_contain_text('Purchase order deleted.')
        expect(draft_row).to_have_count(0)
        assert not errors, errors
        print('PASS: worker navigation, inventory selection, purchasing tier, exact decimals, live preview, save/reopen/edit/finalise, PDF, multipage browser print, duplicate/cancel, edit cancelled record, delete from detail/dashboard, mobile layout, zero JavaScript errors.')
        browser.close()
finally:
    server.shutdown()
