/* Decimal strings on the wire; fixed-point BigInt for all financial arithmetic. */
(() => {
  'use strict';
  const root = document.querySelector('.po-app');
  if (!root) return;
  const base = '/purchase-orders';
  const $ = (s, context = document) => context.querySelector(s);
  const message = $('#po-message');
  const notify = (text, error = false) => { message.textContent = text; message.className = text ? (error ? 'po-error' : 'po-success') : ''; };
  const scaled = (value, places) => {
    const raw = String(value);
    if (raw.length > 30 || !new RegExp('^\\d+(?:\\.\\d{1,' + places + '})?$').test(raw)) throw Error('Enter non-negative decimal values with the allowed precision.');
    const [whole, fraction = ''] = raw.split('.');
    return BigInt(whole) * (10n ** BigInt(places)) + BigInt(fraction.padEnd(places, '0'));
  };
  const money = cents => `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`;
  const calculate = (items, type, value) => {
    let subtotal = 0n;
    const amounts = items.map(item => {
      const q = scaled(item.quantity, 4), p = scaled(item.unit_price, 4);
      if (q <= 0n || q > 999999990000n || p > 999999990000n) throw Error('Quantity must be positive; quantity and price must be at most 99,999,999.');
      const amount = (q * p + 500000n) / 1000000n;
      subtotal += amount;
      return money(amount);
    });
    const input = scaled(value, 2);
    if (type === 'percent' && input > 10000n) throw Error('Percentage discount cannot exceed 100.');
    const discount = type === 'percent' ? (subtotal * input + 5000n) / 10000n : input;
    if (discount > subtotal) throw Error('Discount cannot exceed the subtotal.');
    return {amounts, subtotal: money(subtotal), discount: money(discount), total: money(subtotal - discount)};
  };
  window.POMath = {calculate};
  async function api(path, options = {}) {
    const response = await fetch(base + path, {...options, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': root.dataset.csrf, ...(options.headers || {})}});
    let data;
    try { data = await response.json(); } catch { throw Error(response.status === 404 ? 'Purchase order not found or unavailable to your account.' : 'Request failed. Please sign in again or retry.'); }
    if (!response.ok) throw Error(data.error || 'Request failed.');
    return data;
  }
  function link(text, path) { const a = document.createElement('a'); a.textContent = text; a.href = base + path; return a; }
  async function action(number, revision, name) {
    const prompts = {finalise: 'Mark this purchase order as finalised?', cancel: 'Cancel this purchase order? Cancellation cannot be undone.', duplicate: 'Create a new draft copy of this purchase order?', delete: 'Permanently delete this purchase order and its history? This cannot be undone.'};
    if (!confirm(prompts[name])) return;
    if (name === 'delete') {
      await api(`/api/orders/${encodeURIComponent(number)}`, {method:'DELETE', body:JSON.stringify({revision})});
      return true;
    }
    const data = await api(`/api/orders/${encodeURIComponent(number)}/${name}`, {method:'POST', body:JSON.stringify({revision})});
    window.location.assign(base + '/' + encodeURIComponent(data.number));
  }
  if (root.dataset.page === 'view') {
    root.addEventListener('click', async event => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      button.disabled = true;
      try {
        const deleted = await action(root.dataset.number, Number(root.dataset.revision), button.dataset.action);
        if (deleted) window.location.assign(base + '/?deleted=1');
      }
      catch (error) { notify(error.message, true); }
      finally { button.disabled = false; }
    });
  }
  if (root.dataset.page === 'dashboard') {
    let page = 1, generation = 0, deletedNotice = new URLSearchParams(location.search).get('deleted') === '1';
    async function load() {
      const current = ++generation;
      notify('Loading purchase orders…');
      const params = new URLSearchParams(new FormData($('#po-filters'))); params.set('page', page);
      try {
        const data = await api('/api/orders?' + params);
        if (current !== generation) return;
        if (!data.orders.length && page > 1) { page--; return load(); }
        const body = $('#po-orders'); body.replaceChildren();
        for (const order of data.orders) {
          const tr = document.createElement('tr');
          const cell = value => { const td = document.createElement('td'); if (value instanceof Node) td.append(value); else td.textContent = value; tr.append(td); return td; };
          const path = '/' + encodeURIComponent(order.number);
          cell(link(order.legacy_number || order.number, path)); cell(order.supplier.name); cell(order.date); cell(order.currency + ' ' + order.total);
          const badge = document.createElement('span'); badge.className = 'po-badge ' + order.status; badge.textContent = order.status; cell(badge);
          const actions = document.createElement('div'); actions.className = 'po-row-actions';
          actions.append(link('View', path));
          actions.append(link('Edit', path + '/edit'));
          const duplicate = document.createElement('button'); duplicate.textContent = 'Duplicate';
          duplicate.onclick = async () => { duplicate.disabled = true; try { await action(order.number, order.revision, 'duplicate'); } catch(error) { notify(error.message, true); } finally { duplicate.disabled = false; } };
          const remove = document.createElement('button'); remove.textContent = 'Delete'; remove.style.color = '#b42318';
          remove.onclick = async () => {
            remove.disabled = true;
            try { if (await action(order.number, order.revision, 'delete')) { deletedNotice = true; await load(); } }
            catch(error) { notify(error.message, true); }
            finally { remove.disabled = false; }
          };
          actions.append(duplicate, link('Print', path + '/print'), link('PDF', path + '/pdf'), remove); cell(actions); body.append(tr);
        }
        notify((deletedNotice ? 'Purchase order deleted. ' : '') + (data.total ? '' : 'No purchase orders found. Create your first PO or adjust the filters.'));
        deletedNotice = false;
        $('#po-page').textContent = `Page ${page} · ${data.total} orders`;
        $('#po-prev').disabled = page <= 1; $('#po-next').disabled = page * 25 >= data.total;
      } catch(error) { if (current === generation) notify(error.message, true); }
    }
    $('#po-filters').onsubmit = event => { event.preventDefault(); page = 1; load(); };
    $('#po-prev').onclick = () => { page--; load(); }; $('#po-next').onclick = () => { page++; load(); };
    load();
  }
  if (root.dataset.page !== 'editor') return;
  let state = JSON.parse($('#po-data').textContent), dirty = false, previewTimer, previewGeneration = 0, searchGeneration = 0, searchTimer;
  const form = $('#po-editor'), rows = $('#po-items');
  function read() {
    const values = Object.fromEntries(new FormData(form));
    const supplier = {};
    for (const key of ['name','details','attention','fax']) supplier[key] = values['supplier.' + key];
    const items = [...rows.children].map(row => Object.fromEntries([...row.querySelectorAll('[data-field]')].map(input => [input.dataset.field, input.value])));
    return {...values, supplier, items, number: state.number, revision: state.revision};
  }
  function update() {
    dirty = true; clearTimeout(previewTimer); previewGeneration++;
    $('#po-preview-status').textContent = 'Preview updating…';
    $('#po-preview').setAttribute('aria-busy', 'true');
    try {
      const data = read(), totals = calculate(data.items, data.discount_type, data.discount_value);
      for (const key of ['subtotal','discount','total']) $('#po-' + key).textContent = totals[key];
      [...rows.children].forEach((row, index) => $('.po-amount', row).textContent = totals.amounts[index]);
    } catch(error) {
      for (const key of ['subtotal','discount','total']) $('#po-' + key).textContent = '—';
      $('#po-preview-status').textContent = error.message;
      $('#po-preview').replaceChildren();
      $('#po-preview').removeAttribute('aria-busy');
      return;
    }
    previewTimer = setTimeout(preview, 500);
  }
  async function preview() {
    const current = previewGeneration;
    if (!form.checkValidity() || !rows.children.length) {
      $('#po-preview-status').textContent = 'Complete supplier and item details to preview.';
      $('#po-preview').replaceChildren(); $('#po-preview').removeAttribute('aria-busy'); return;
    }
    try {
      const result = await api('/api/preview', {method:'POST', body:JSON.stringify(read())});
      if (current !== previewGeneration) return;
      $('#po-preview').replaceChildren();
      for (const svg of result.pages) { const sheet = document.createElement('div'); sheet.className = 'po-sheet'; sheet.innerHTML = svg; $('#po-preview').append(sheet); }
      $('#po-preview-status').textContent = `${result.pages.length} A4 page${result.pages.length === 1 ? '' : 's'} · preview up to date`;
    } catch(error) { if (current === previewGeneration) { $('#po-preview-status').textContent = error.message; $('#po-preview').replaceChildren(); } }
    finally { if (current === previewGeneration) $('#po-preview').removeAttribute('aria-busy'); }
  }
  async function prices(row) {
    const select = $('.po-prices', row), status = $('.po-price-status', row);
    const uid = $('[data-field="product_id"]', row).value;
    const requestId = String((Number(row.dataset.priceRequest) || 0) + 1); row.dataset.priceRequest = requestId;
    select.replaceChildren(new Option('Manual / keep current price', '')); select.disabled = true;
    if (!uid) { status.textContent = 'Manual item: enter a unit price.'; return; }
    status.textContent = 'Loading purchasing tiers…';
    try {
      const params = new URLSearchParams({uid, currency:form.elements.currency.value, date:form.elements.date.value});
      const options = await api('/api/prices?' + params);
      if (row.dataset.priceRequest !== requestId) return;
      for (const option of options) {
        const label = `${option.tier} · ${option.currency} ${option.unit_price}${option.customer ? ' · ' + option.customer : ''}${option.effective_date ? ' · ' + option.effective_date : ''}`;
        const node = new Option(label, option.unit_price); node.dataset.tier = option.tier; select.append(node);
      }
      select.disabled = !options.length;
      status.textContent = options.length ? 'Purchasing (P) only. Choose the applicable record and tier; prices remain editable.' : 'No matching purchasing price for this product, currency and date. Enter the agreed price.';
    } catch(error) { if (row.dataset.priceRequest === requestId) status.textContent = error.message; }
  }
  function addItem(item = {}) {
    if (rows.children.length >= 200) { notify('A PO can have at most 200 items.', true); return; }
    const row = document.createElement('section'); row.className = 'card po-item';
    const head = document.createElement('div'); head.className = 'po-toolbar po-item-head';
    const title = document.createElement('h3'); title.textContent = 'Item';
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn po-secondary'; remove.textContent = 'Remove item';
    remove.onclick = () => { if (confirm('Remove this item?')) { row.remove(); renumber(); update(); } };
    head.append(title, remove); row.append(head);
    const grid = document.createElement('div'); grid.className = 'po-item-grid';
    for (const [key, label, span, max] of [['description','Description',4,600],['product_id','UID / Part_id',2,120],['mssid','Mssid',2,120],['size','Size',2,100],['material','Material',2,100],['quantity','Quantity (up to 4 decimals)',2,30],['unit_price','Unit price (4 decimals)',2,30],['notes','Additional item notes',4,1000]]) {
      const wrapper = document.createElement('label'); wrapper.className = 'span' + span; wrapper.append(document.createTextNode(label));
      const input = document.createElement(['description','notes'].includes(key) ? 'textarea' : 'input');
      input.dataset.field = key; input.maxLength = max; input.value = item[key] ?? (key === 'quantity' ? '1' : key === 'unit_price' ? '0.0000' : '');
      if (['description','quantity','unit_price'].includes(key)) input.required = true;
      if (['quantity','unit_price'].includes(key)) input.inputMode = 'decimal';
      if (key === 'product_id') input.addEventListener('change', () => { $('[data-field="price_tier"]', row).value = ''; prices(row); });
      if (key === 'unit_price') input.addEventListener('input', () => { $('[data-field="price_tier"]', row).value = ''; $('.po-prices', row).value = ''; });
      wrapper.append(input); grid.append(wrapper);
    }
    const tier = document.createElement('input'); tier.type = 'hidden'; tier.dataset.field = 'price_tier'; tier.value = item.price_tier || ''; grid.append(tier);
    const label = document.createElement('label'); label.className = 'span4'; label.textContent = 'Purchasing price tier';
    const select = document.createElement('select'); select.className = 'po-prices'; label.append(select);
    const priceStatus = document.createElement('small'); priceStatus.className = 'po-price-status'; label.append(priceStatus); grid.append(label);
    select.onchange = () => { const selected = select.selectedOptions[0]; if (select.value) { $('[data-field="unit_price"]', row).value = select.value; tier.value = selected.dataset.tier; } else tier.value = ''; update(); };
    const amount = document.createElement('strong'); amount.className = 'span4'; amount.append(document.createTextNode('Amount: ')); const output = document.createElement('output'); output.className = 'po-amount'; amount.append(output); grid.append(amount);
    row.append(grid); rows.append(row); renumber(); prices(row); update();
  }
  function renumber() { [...rows.children].forEach((row,index) => $('h3',row).textContent = `Item ${index + 1}`); }
  $('#po-add').onclick = () => addItem();
  for (const item of state.items) addItem(item);
  if (!state.items.length) addItem();
  dirty = false;
  form.addEventListener('input', event => { if (event.target.id !== 'po-search') update(); });
  for (const name of ['currency','date']) form.elements[name].addEventListener('change', () => {
    [...rows.children].forEach(row => { $('[data-field="price_tier"]',row).value = ''; prices(row); });
    notify('Currency/date changed. Review all existing unit prices; they have not been converted or replaced.');
  });
  $('#po-search').oninput = () => {
    clearTimeout(searchTimer); const current = ++searchGeneration; const query = $('#po-search').value.trim();
    $('#po-results').replaceChildren(); $('#po-search-status').textContent = query ? 'Searching…' : '';
    if (!query) return;
    searchTimer = setTimeout(async () => {
      try {
        const result = await api('/api/products?q=' + encodeURIComponent(query));
        if (current !== searchGeneration) return;
        $('#po-search-status').textContent = result.length ? 'Select a product to add it.' : 'No products found. Use Add manual item.';
        for (const item of result) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = `${item.product_id} · ${item.description} · ${item.mssid}`;
          button.onclick = () => {
            if (rows.children.length === 1 && !$('[data-field="description"]',rows).value && !$('[data-field="product_id"]',rows).value) rows.replaceChildren();
            addItem(item); $('#po-results').replaceChildren(); $('#po-search').value = ''; $('#po-search-status').textContent = 'Product added. Select a purchasing tier or enter the agreed price.';
          }; $('#po-results').append(button);
        }
      } catch(error) { if (current === searchGeneration) $('#po-search-status').textContent = error.message; }
    }, 250);
  };
  form.onsubmit = async event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    const button = $('#po-save'); button.disabled = true; notify('Saving purchase order…');
    const submitted = JSON.stringify(read());
    try {
      const result = await api('/api/orders' + (state.number ? '/' + encodeURIComponent(state.number) : ''), {method:state.number ? 'PUT' : 'POST', body:submitted});
      if (JSON.stringify(read()) === submitted) { dirty = false; window.location.assign(base + '/' + encodeURIComponent(result.number)); }
      else { state = result; dirty = true; notify('Purchase order saved. You made further changes while saving; save again to include them.'); $('#po-save-state').textContent = state.number; $('#po-save').textContent = 'Save changes'; }
    } catch(error) { notify(error.message, true); }
    finally { button.disabled = false; }
  };
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
})();
