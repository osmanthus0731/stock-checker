# Purchase Orders

The module is available at `/purchase-orders/` and in the existing black sidebar. It uses a Flask blueprint registered at the end of `app.py`, the existing login session and roles, and a separate `purchase_orders` collection in the configured MongoDB database. No Access table, inventory mapping, inventory record, or pricing record is changed by this module.

## Setup

Install the updated requirements in the same Python environment used to run the app, then restart the app:

```powershell
python -m pip install -r requirements.txt
```

New runtime dependencies are `reportlab==5.0.1` for server-side PDFs and `tzdata>=2025.2` for Kuala Lumpur dates on Windows. No browser executable, external PDF service, or native rendering library is needed in production. Existing `MONGO_URI`/`MONGO_URL`, `MONGO_DB`, and `FLASK_SECRET` settings remain in use. MongoDB credentials need insert/update/read access to the new `purchase_orders` collection. No migration is required; MongoDB creates it on the first save.

Optional performance indexes can be created with:

```powershell
python -m flask --app app po-init-indexes
```

This command only creates indexes on `purchase_orders`. Uniqueness does not depend on running it: the internal PO reference is MongoDB's inherently unique `_id`.

The reference uses **Lucida Fax** regular/bold/italic and **Century Gothic** bold. The renderer detects these installed Windows fonts. On other hosts, set `PO_FONT_DIR` to a directory containing licensed `LFAX.TTF`, `LFAXD.TTF`, `LFAXI.TTF`, and `GOTHICB.TTF` files. Fonts are embedded into generated PDFs; proprietary font files are not distributed with the repository. If absent, built-in Times/Helvetica fonts are used. HTML text lengths use the same PDF font metrics; the browser uses available local fonts. Exported PDF provides the most consistent typography across devices.

Supported currencies default to MYR, USD, SGD, EUR, GBP, CNY, JPY, THB and AUD and can be configured through Flask `app.config['PO_CURRENCIES']`. Values are never converted automatically. PO line amounts and totals use two decimal places for all configured currencies, matching the requested document format.

## Workflow and permissions

1. Open Purchase Orders and select Create Purchase Order.
2. Enter the supplier, dates, terms and currency.
3. Search inventory by UID, description or Mssid. Choose a result, then choose a purchasing tier or enter a unit price. Manual items are also supported.
4. Review the live A4 preview and save the draft. Reopen or edit it from the dashboard.
5. Finalise the draft after review. Print or export PDF from the document view.

Admins can access all POs. Workers can create and access only their own POs, including editing, finalising, cancelling, duplicating and exporting. Unknown roles are denied. Ownership restrictions cover list, view, edit, preview, print, PDF and every mutation endpoint. POST/PUT requests require the session-specific `X-CSRF-Token` supplied by the module's pages. Existing unrelated forms and routes are unchanged.

Draft contents are editable. Finalised contents are immutable. A draft or finalised PO can be cancelled; cancellation is irreversible and retains all contents. Cancelled documents cannot be edited or finalised. Duplicating any accessible PO creates a new draft with a new reference and cleared legacy number. Copies preserve the source dates, delivery date, prices and snapshot details for the user to review.

Every save/transition includes a revision. An atomic MongoDB comparison on revision, ownership and status rejects stale edits with HTTP 409. Each record has creator/updater timestamps, status timestamps, schema/layout versions and an event history. There is no PO delete operation.

## Field mapping and pricing

| PO field | Existing MongoDB field, in priority order | Access origin |
|---|---|---|
| Product ID | `uid`, `Part_id` | ITMMST UID / Part_id |
| Description | `name`, `Desc` | ITMMST Desc |
| Mssid | `readable_id`, `Mssid` | ITMMST Mssid |
| Size | `size`, `Size` | Explicit size field, when already imported |
| Material | `material`, `Material`, `Matl` | Explicit material field, when already imported |
| Purchasing tier | `pricing.part_id` matching product ID or Mssid; `pricecd` or legacy `priced` = `P` | Cus_Price |

The current product importer does **not** copy Size or Material. The editor fills those fields if present in MongoDB; otherwise they stay blank for manual entry. It does not infer them from product names or alter the importer.

Tier options are S12, S100, S500, 1K, 3K, 5K and 10K, including the existing S1000/S3000/S5000/S10000 aliases. Options retain their record, customer, currency and effective-date labels. Only matching purchasing records in the chosen currency effective on/before the order date are offered. Undated P records are allowed. Missing/unknown currency, future-dated records and selling prices are excluded. No price is silently selected; the user chooses the applicable record/tier and may override it. Changing currency/date refreshes choices and asks the user to review existing prices without currency conversion.

Existing imports store tier prices as Mongo doubles. The PO module converts their decimal string representation to Python Decimal; it never uses the existing float-based pricing helper for financial calculations. Existing purchasing/selling screens and calculations are left intact.

Supplier details, company identity, descriptions, product IDs, size, Mssid, material, notes, quantities, unit prices, selected tier labels, discount inputs and calculated totals are copied into each PO. Reopening/printing/exporting uses that snapshot, never current inventory/pricing values.

## Calculations and local references

Quantities must be positive and have at most four decimals; unit prices are non-negative with at most four decimals. Each is capped at 99,999,999. JSON financial inputs must be decimal strings, not floating-point numbers. Amounts are `quantity × unit price`, rounded per line to two decimals using ROUND_HALF_UP. Subtotal sums these rounded line amounts. Discounts can be a fixed amount or percentage (up to two decimals); percentage discount amounts are rounded HALF_UP. Discounts cannot exceed the subtotal. JavaScript uses scaled BigInt arithmetic with the same rounding; the server independently validates and recalculates every saved amount.

Each record receives `LOCAL-PO-` plus 16 random hexadecimal characters, enforced by MongoDB's unique `_id` and retried on collisions. These are temporary application references, **not** linked to the existing purchasing system's sequence. Optional `legacy_number` is a separate manually entered display/search reference, not a uniqueness constraint or sequence reservation. The reference PDF number 34905 is never used as a numbering seed.

## Print and PDF

The measured Titus layout uses A4, its company logo, compact supplier/details header, grey five-column item header, generous whitespace, footer totals and an unsigned signature area. The reference's handwritten signature/stamp is not reused. A subtotal line and visible Draft/Cancelled labels are added for clarity.

One layout engine produces drawing commands for both SVG pages in HTML and ReportLab PDFs. This keeps wrapping, row placement, pagination, header repetition, footer positions and page numbering consistent. Rows stay together. Up to 200 rows are allowed; a header/item too tall for a page returns an actionable validation error. The signature/totals footer appears only on the final page. Header fields expand vertically for long supplier details or local references.

The print route is independent of the application shell. Its dedicated stylesheet sets A4 pages, removes controls, and forces page breaks. For browser printing use A4, 100% scale and disable browser-added headers/footers. Download PDF avoids browser print settings entirely.

ReportLab reference: [PDF canvas documentation](https://docs.reportlab.com/reportlab/userguide/ch2_graphics/).

## Routes

All paths below start with `/purchase-orders`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/`, `/new`, `/<number>`, `/<number>/edit` | Dashboard/editor/detail |
| GET | `/<number>/print`, `/<number>/pdf` | A4 HTML print / downloadable PDF |
| GET, POST | `/api/orders` | Filtered/paginated list / create draft |
| GET, PUT | `/api/orders/<number>` | Read snapshot / update draft |
| POST | `/api/orders/<number>/duplicate` | New draft copy |
| POST | `/api/orders/<number>/finalise` | Lock draft contents |
| POST | `/api/orders/<number>/cancel` | Cancel draft/finalised PO |
| POST | `/api/preview` | Validate and render unsaved A4 pages |
| GET | `/api/products?q=...` | Read-only inventory search |
| GET | `/api/prices?uid=...&currency=...&date=...` | Matching purchasing-tier choices |

The list accepts `q`, `status`, `sort=oldest|newest`, and `page`. It sorts by order date, creation timestamp and unique reference, with 25 results per page.

## Verification

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tests/browser_purchase_orders.py
```

The browser check uses installed Microsoft Edge in headless mode. Tests patch MongoClient with mongomock before importing the app, and never contact live MongoDB or Access. The browser test starts an ephemeral local HTTP server and closes it afterward.

Verified on Windows/Python 3.13: backend tests, worker/admin ownership checks, amount/discount validation, collision retries, concurrent creation, revision conflicts, immutable statuses, inventory/snapshot independence, P-only pricing, search/sort, A4 multipage output, escaped document text, old dashboard/search routes, and the complete desktop/mobile browser workflow. The browser check also compares PDF/page count against browser print output and checks for JavaScript errors. Test results are not a live database deployment check.

An existing missing worker-dashboard include was restored as `templates/parts/product_table.html`. The worker menu's CSS class was scoped to prevent it overriding the shared black sidebar. These are small compatibility repairs; no inventory business logic was changed.

## File manifest

Modified:

- `.gitignore`
- `.env.sample`
- `app.py`
- `requirements.txt`
- `templates/base.html`
- `templates/index.html`

Created:

- `PURCHASE_ORDERS.md`
- `requirements-dev.txt`
- `purchase_orders/__init__.py`
- `purchase_orders/domain.py`
- `purchase_orders/repository.py`
- `purchase_orders/document.py`
- `static/po-logo.png`
- `static/purchase_orders.css`
- `static/purchase_order_print.css`
- `static/purchase_orders.js`
- `templates/parts/product_table.html`
- `templates/purchase_orders/dashboard.html`
- `templates/purchase_orders/editor.html`
- `templates/purchase_orders/view.html`
- `templates/purchase_orders/print.html`
- `templates/purchase_orders/error.html`
- `tests/conftest.py`
- `tests/test_purchase_orders.py`
- `tests/browser_purchase_orders.py`

Ignored local inspection/test outputs: `reference-po.png`, `test-artifacts/logo-inspection.png`, `test-artifacts/po-editor-desktop.png`, `test-artifacts/po-editor-mobile.png`, `test-artifacts/purchase-order.pdf`, `test-artifacts/purchase-order.png`, `test-artifacts/browser-print.pdf`. Downloaded verification dependencies live in ignored `.po-deps/`; production should install `requirements.txt` in its normal environment.
