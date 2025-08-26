# app.py — Read-only inventory from MS Access (local Windows), users + fallback data in MongoDB (cloud)
#          + Pricing view: Mongo-first (works on Render), Access fallback when available locally
#          + Smooth caching, lazy QR, single-row lookup, Category + Volume + Pict filters
#          + /debug_pricing/<uid> diagnostic endpoint
#          + Cloud-safe: auto-fallback to Mongo when Access not available (Linux/Render)
#          + FIXES: back-button no-cache & logout confirmation
#          + QR -> /item/<uid> (login-gated). Also keeps /scan/<uid> for backward-compat
#          + Search accepts GET ?q=
#          + Hardened Mongo timeouts + graceful error handling + /dbcheck

from flask import (
    Flask, render_template, render_template_string, request, redirect, url_for,
    session, flash, send_file
)
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
from io import BytesIO
import os, qrcode, certifi, time, re

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret")
IS_VERCEL = os.getenv("VERCEL") == "1"

# ---------------- Mongo (users + products + pricing) ----------------
MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGO_URL")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI not set")

# Longer timeouts help on cold/paused clusters; TLS for SRV
_tls = {
    "serverSelectionTimeoutMS": 45000,  # wait up to 45s for primary on cold start
    "connectTimeoutMS": 20000,
    "socketTimeoutMS": 20000,
}
if MONGO_URI.startswith("mongodb+srv://") or "mongodb.net" in MONGO_URI:
    _tls.update({"tls": True, "tlsCAFile": certifi.where()})

client = MongoClient(MONGO_URI, **_tls)
db = client[os.getenv("MONGO_DB", "inventory")]

# Collections
users         = db.get_collection("users")
products_col  = db.get_collection("products")
admin_logs    = db.get_collection("admin_logs")
submissions   = db.get_collection("submissions")
pricing_col   = db.get_collection("pricing")  # Mongo-first pricing

# ---------------- Access config ----------------
ACCESS_DB_PATH     = (os.getenv("ACCESS_DB_PATH") or "").strip()
ACCESS_TABLE       = (os.getenv("ACCESS_TABLE") or "ITMMST").strip()
ACCESS_PRICE_TABLE = (os.getenv("ACCESS_PRICE_TABLE") or "Cus_Price").strip()
FORCE_MONGO_ONLY   = os.getenv("FORCE_MONGO_ONLY", "0") == "1"

# ---------------- ODBC (pyodbc preferred, fallback to pypyodbc) ----------------
ODBC_LIB = None
_odbc = None
if not FORCE_MONGO_ONLY:
    try:
        import pyodbc as _odbc
        ODBC_LIB = "pyodbc"
    except Exception:
        try:
            import pypyodbc as _odbc
            ODBC_LIB = "pypyodbc"
        except Exception:
            _odbc = None
            ODBC_LIB = None

# ---------------- Paging ----------------
ITEMS_PER_PAGE = 5

# ---------------- Simple in-process cache (Access path) ----------------
_PRODUCTS_CACHE = {"ts": 0.0, "data": []}
CACHE_TTL_SEC = 60

# ---------------- Auth helpers ----------------
def login_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if "username" not in session:
            session["post_login_next"] = request.url
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return inner

def role_required(*roles):
    def wrap(fn):
        @wraps(fn)
        def inner(*a, **kw):
            if "username" not in session or session.get("role") not in roles:
                session["post_login_next"] = request.url
                return redirect(url_for("login", next=request.path))
            return fn(*a, **kw)
        return inner
    return wrap

# ---------- No-cache headers ----------
@app.after_request
def add_no_cache_headers(resp):
    if not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

# ---------------- Access availability ----------------
def _should_use_access():
    if FORCE_MONGO_ONLY:
        return False
    return (os.name == "nt") and bool(ACCESS_DB_PATH) and os.path.exists(ACCESS_DB_PATH) and (_odbc is not None)

# ---------------- Access helpers ----------------
def _access_conn():
    if not _should_use_access():
        raise RuntimeError("Access not available in this environment")
    conn_str = (
        r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
        rf"DBQ={ACCESS_DB_PATH};"
        r"READONLY=TRUE;"
    )
    return _odbc.connect(conn_str, autocommit=True)

def _table_columns(table: str):
    cn = _access_conn()
    try:
        cur = cn.cursor()
        cur.execute(f"SELECT TOP 1 * FROM [{table}]")
        cols = [c[0] for c in cur.description] if getattr(cur, "description", None) else []
        return cols
    finally:
        cn.close()

def _iter_access_rows():
    cn = _access_conn()
    try:
        cur = cn.cursor()
        cur.execute(f"SELECT * FROM [{ACCESS_TABLE}]")
        cols = [c[0] for c in cur.description] if getattr(cur, "description", None) else []
        for tup in cur.fetchall():
            yield dict(zip(cols, tup))
    finally:
        cn.close()

# --------- Helpers for mapping rows / normalization ----------
_VOL_RE = re.compile(r"(\d{1,5})\s*ml\b", re.IGNORECASE)
def _extract_volume_ml(text: str):
    if not text: return None
    m = _VOL_RE.search(text)
    if not m: return None
    try: return int(m.group(1))
    except Exception: return None

def _to_int_safe(v, default=None):
    try:
        if v in (None, ""): return default
        return int(v)
    except Exception:
        return default

def _map_row(row):
    g = lambda *keys, default="": next((row.get(k) for k in keys if k in row), default)
    uid   = (g("Part_id","part_id","Part-ID","PartID") or "").strip()
    name  = (g("Desc","desc","Description") or "").strip()
    mssid = (g("Mssid","mssid","MSSID") or "").strip()
    cat   = g("Cat","cat","Category") or "Uncategorized"
    pict  = _to_int_safe(g("Pict","pict","PICT"), default=None)

    loc_a = (g("Loc_film_box","loc_film_box","LocA") or "").strip()
    qty_a = _to_int_safe(g("StockA","stocka","QtyA"), default=0)
    loc_b = (g("Loc_wh","loc_wh","LocB") or "").strip()
    qty_b = _to_int_safe(g("StockB","stockb","QtyB"), default=0)

    locs = []
    if loc_a: locs.append({"area": loc_a, "quantity": qty_a})
    if loc_b: locs.append({"area": loc_b, "quantity": qty_b})

    total = _to_int_safe(g("Stock_Office","Stock_office","stock_office","Total"), default=qty_a + qty_b)
    volume_ml = _extract_volume_ml(name) or _extract_volume_ml(mssid)

    return {
        "uid": uid,
        "name": name,
        "readable_id": mssid,
        "category": cat,
        "pict": pict,
        "locations": locs,
        "stock": total,
        "volume_ml": volume_ml
    }

def _all_products_from_access_uncached():
    return [_map_row(r) for r in _iter_access_rows()
            if (r.get("Part_id") or r.get("part_id") or r.get("Part-ID") or r.get("PartID"))]

def _all_products_from_access():
    now = time.time()
    if (now - _PRODUCTS_CACHE["ts"]) > CACHE_TTL_SEC or not _PRODUCTS_CACHE["data"]:
        _PRODUCTS_CACHE["data"] = _all_products_from_access_uncached()
        _PRODUCTS_CACHE["ts"] = now
    return _PRODUCTS_CACHE["data"]

def _detect_pk_column():
    cols = _table_columns(ACCESS_TABLE)
    for c in ["Part_id","Part-ID","part_id","PartID","PartId","partid"]:
        if c in cols: return c
    return cols[0] if cols else None

def _get_product_by_uid_access(uid: str):
    pk = _detect_pk_column()
    if not pk: return None
    cn = _access_conn()
    try:
        cur = cn.cursor()
        cur.execute(f"SELECT * FROM [{ACCESS_TABLE}] WHERE [{pk}]=?", (uid,))
        row = cur.fetchone()
        if not row: return None
        cols = [c[0] for c in cur.description] if getattr(cur, "description", None) else []
        return _map_row(dict(zip(cols, row)))
    finally:
        cn.close()

# ---------- Mongo helpers (with graceful error handling) ----------
def _normalize_mongo_product(p: dict) -> dict:
    out = {
        "uid": p.get("uid", "") or p.get("Part_id", ""),
        "name": p.get("name", "") or p.get("Desc", ""),
        "readable_id": p.get("readable_id", "") or p.get("Mssid", ""),
        "category": p.get("category", "Uncategorized"),
        "pict": p.get("pict", None),
        "stock": p.get("stock"),
        "volume_ml": p.get("volume_ml")
    }
    locs = p.get("locations") or {}
    if isinstance(locs, dict):
        out["locations"] = [{"area": k, "quantity": (int(v) if v not in (None, "") else 0)} for k, v in locs.items()]
    elif isinstance(locs, list):
        norm = []
        for L in locs:
            area = (L.get("area") or L.get("loc") or "").strip()
            qty  = L.get("quantity")
            try: qty = int(qty)
            except: qty = 0
            if area:
                norm.append({"area": area, "quantity": qty})
        out["locations"] = norm
    else:
        out["locations"] = []
    if out.get("volume_ml") is None:
        out["volume_ml"] = _extract_volume_ml(out.get("name", "")) or _extract_volume_ml(out.get("readable_id", ""))
    if out.get("stock") is None:
        out["stock"] = sum((i.get("quantity") or 0) for i in out["locations"])
    return out

def _all_products_from_mongo():
    try:
        docs = list(products_col.find({}, {"_id": 0}))
        return [_normalize_mongo_product(p) for p in docs]
    except Exception as e:
        app.logger.exception("Mongo read failed in _all_products_from_mongo: %s", e)
        try: flash("Database is unavailable right now. Showing an empty list.", "error")
        except: pass
        return []

def _get_product_by_uid_mongo(uid: str):
    try:
        p = products_col.find_one({"uid": uid}, {"_id": 0})
        if not p:
            p = products_col.find_one({"Part_id": uid}, {"_id": 0})
        return _normalize_mongo_product(p) if p else None
    except Exception as e:
        app.logger.exception("Mongo read failed in _get_product_by_uid_mongo: %s", e)
        try: flash("Database is unavailable right now.", "error")
        except: pass
        return None

def _all_products():
    return _all_products_from_access() if _should_use_access() else _all_products_from_mongo()

def _get_product_by_uid(uid: str):
    return _get_product_by_uid_access(uid) if _should_use_access() else _get_product_by_uid_mongo(uid)

# ---------- Pricing (Mongo-first, Access fallback) ----------
def _coerce_num(x):
    try:
        return float(x) if x not in (None, "") else None
    except Exception:
        return None

def _empty_tiers():
    return {"S12": None, "S100": None, "S500": None, "1K": None, "3K": None, "5K": None, "10K": None}

def _merge_tier(doc, bucket):
    mapping = {
        "S12": "S12", "S100": "S100", "S500": "S500",
        "1K": "1K", "S1000": "1K",
        "3K": "3K", "S3000": "3K",
        "5K": "5K", "S5000": "5K",
        "10K": "10K", "S10000": "10K",
    }
    for k, v in doc.items():
        k_up = str(k).upper()
        if k_up in mapping:
            bucket[mapping[k_up]] = _coerce_num(v)

def _product_part_id(uid: str) -> str:
    prod = _get_product_by_uid(uid)
    if prod:
        rid = (prod.get("readable_id") or "").strip()
        if rid:
            return rid
    return (uid or "").strip()

def _get_prices_for_part_mongo(uid_or_part: str):
    try:
        key_raw = (uid_or_part or "").strip()
        if not key_raw:
            return None
        prod = _get_product_by_uid(uid_or_part) or {}
        uid_candidate   = (prod.get("uid") or key_raw or "").strip()
        mssid_candidate = (prod.get("readable_id") or "").strip()
        candidates = {c for c in [uid_candidate, mssid_candidate, key_raw] if c}
        if not candidates:
            return None
        rows = list(pricing_col.find(
            {
                "part_id": {"$in": list(candidates)},
                "$or": [{"priced": {"$in": ["P","S"]}}, {"pricecd": {"$in": ["P","S"]}}],
            },
            {"_id":0}
        ))
        if not rows:
            return None
        result = {
            "uid": next(iter(candidates)),
            "name": prod.get("name", "") or "",
            "P": _empty_tiers(),
            "S": _empty_tiers(),
            "_source": "mongo",
            "_debug_candidates": list(candidates),
        }
        def key_dt(r): return r.get("eff_date") or 0
        grouped = {"P": [], "S": []}
        for r in rows:
            code = (r.get("priced") or r.get("pricecd") or "").upper()
            if code in grouped: grouped[code].append(r)
        for code in ["P","S"]:
            if grouped[code]:
                latest = sorted(grouped[code], key=key_dt, reverse=True)[0]
                _merge_tier(latest, result[code])
        return result
    except Exception as e:
        app.logger.exception("Mongo read failed in _get_prices_for_part_mongo: %s", e)
        return None

def _get_prices_for_part_access(uid: str):
    if not _should_use_access():
        return None
    part_id = (uid or "").strip()
    if not part_id:
        return None
    out = {"uid": part_id, "name": "", "P": _empty_tiers(), "S": _empty_tiers(), "_source": "access"}
    prod = _get_product_by_uid_access(part_id)
    if prod: out["name"] = prod.get("name", "")
    cn = _access_conn()
    try:
        cur = cn.cursor()
        cur.execute(f"SELECT * FROM [{ACCESS_PRICE_TABLE}] WHERE Trim([Part_id]) = ?", (part_id,))
        rows = cur.fetchall() or []
        cols = [c[0] for c in cur.description] if getattr(cur, "description", None) else []
    finally:
        cn.close()
    def get_key(d, *cands):
        if not d: return None
        lower = {k.lower(): k for k in d.keys()}
        for c in cands:
            k = lower.get(c.lower())
            if k: return k
        return None
    for tup in rows:
        rec = dict(zip(cols, tup))
        t_key = get_key(rec, "Pricecd", "Priced", "Price_cd")
        t_val = (str(rec[t_key]).strip().upper() if t_key and rec.get(t_key) is not None else "")
        if t_val not in ("P","S"): continue
        def tier(*names):
            k = get_key(rec, *names)
            return _coerce_num(rec[k]) if k else None
        bucket = out[t_val]
        bucket["S12"]  = tier("S12","s12")  or bucket["S12"]
        bucket["S100"] = tier("S100","s100") or bucket["S100"]
        bucket["S500"] = tier("S500","s500") or bucket["S500"]
        bucket["1K"]   = tier("1k","1K","S1000","s1000") or bucket["1K"]
        bucket["3K"]   = tier("3k","3K","S3000","s3000") or bucket["3K"]
        bucket["5K"]   = tier("5k","5K","S5000","s5000") or bucket["5K"]
        bucket["10K"]  = tier("10k","10K","S10000","s10000") or bucket["10K"]
    return out

def _get_prices_for_part(uid: str):
    return _get_prices_for_part_mongo(uid) or _get_prices_for_part_access(uid)

# ---------------- Health ----------------
@app.route("/ping")
def ping(): return "pong"

@app.route("/dbcheck")
def dbcheck():
    """Simple DB health: returns ok + estimated product count, or error."""
    try:
        client.admin.command("ping")
        n = products_col.estimated_document_count()
        return {"ok": True, "ping": "ok", "products": int(n)}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 503

@app.route("/favicon.ico")
def favicon(): return "", 204

# ---------------- Auth ----------------
@app.route("/", methods=["GET","POST"])
def login():
    if request.method == "GET" and "username" in session:
        next_url = request.args.get("next")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("admin_dashboard") if session.get("role")=="admin" else url_for("index"))

    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = (request.form.get("password") or "")
        user = users.find_one({"username": u})
        if user and check_password_hash(user["password"], p):
            session["username"], session["role"] = user["username"], user["role"]
            next_url = request.args.get("next") or session.pop("post_login_next", None)
            if next_url: return redirect(next_url)
            return redirect("/admin_dashboard" if user["role"] == "admin" else "/index")
        flash("Invalid credentials", "error")
    return render_template("login.html")

@app.route("/seed_users")
def seed_users():
    users.delete_many({})
    users.insert_many([
        {"username":"admin","password":generate_password_hash("adminpass"),"role":"admin"},
        {"username":"worker1","password":generate_password_hash("workerpass"),"role":"worker"},
    ])
    return "seeded"

@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    if request.method == "POST":
        session.clear()
        return redirect(url_for("login"))
    return render_template_string("""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Confirm Logout</title>
    <style>
      body { font-family: system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; background:#fafafa; margin:0; display:grid; place-items:center; height:100vh; }
      .card { background:#fff; padding:24px; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,.08); width:min(420px,90vw); }
      h2 { margin:0 0 8px; }
      p { color:#555; margin:0 0 16px; }
      .row { display:flex; gap:12px; }
      .btn { appearance:none; border:0; padding:10px 14px; border-radius:8px; cursor:pointer; font-weight:600; }
      .btn-primary { background:#6d28d9; color:#fff; }
      .btn-ghost { background:#f3f4f6; color:#111; }
      a { text-decoration:none; }
    </style>
  </head>
  <body>
    <div class="card">
      <h2>Log out?</h2>
      <p>Do you want to log out of your Mizitco account <strong>{{ session.get('username') }}</strong>?</p>
      <form method="post" class="row">
        <button class="btn btn-primary" type="submit">Yes, log out</button>
        <a class="btn btn-ghost" href="{{ url_for('admin_dashboard') if session.get('role')=='admin' else url_for('index') }}">Cancel</a>
      </form>
    </div>
  </body>
</html>
    """)

# ---------------- QR (now points to /item/<uid>) ----------------
BASE_URL = os.getenv("BASE_URL", "https://mizitco-system.onrender.com").rstrip("/")

def _qr_target_url(uid: str) -> str:
    """
    Build absolute URL to /item/<uid>. Uses BASE_URL if provided, else Flask _external.
    """
    path = url_for("item_detail", uid=uid)
    if BASE_URL:
        return f"{BASE_URL}{path}"
    return url_for("item_detail", uid=uid, _external=True)

@app.route("/qr/<uid>.png")
def qr(uid: str):
    target = _qr_target_url(uid)
    img = qrcode.make(target)
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"{uid}.png")

# Backward-compat: older codes that hit /scan/<uid>
@app.route("/scan/<uid>")
def scan_qr(uid: str):
    if "username" not in session:
        session["post_login_next"] = url_for("item_detail", uid=uid)
        return redirect(url_for("login", next=url_for("item_detail", uid=uid)))
    return redirect(url_for("item_detail", uid=uid))

# Item detail page (login-gated)
@app.route("/item/<uid>")
@login_required
def item_detail(uid: str):
    product = _get_product_by_uid(uid)
    if not product:
        flash("Product not found.", "error")
        if session.get("role") == "admin":
            return redirect(url_for("search_admin", q=uid))
        return redirect(url_for("search_worker", q=uid))
    has_pricing = bool(_get_prices_for_part(uid))
    return render_template("item.html", role=session.get("role"), product=product, has_pricing=has_pricing)

# ---------------- Inventory context builder (shared) ----------------
def _filter_sort_paginate(products_all):
    selected_cat  = (request.values.get("filter_category") or "All").strip()
    selected_vol  = (request.values.get("volume") or "").strip()
    selected_pict = (request.values.get("pict") or "").strip()

    if selected_cat and selected_cat != "All":
        products_all = [p for p in products_all if (p.get("category") or "Uncategorized") == selected_cat]

    if selected_vol:
        try:
            v = int(selected_vol)
            products_all = [p for p in products_all if p.get("volume_ml") == v]
        except Exception:
            pass

    if selected_pict:
        try:
            pv = int(selected_pict)
            products_all = [p for p in products_all if (p.get("pict") is not None and p.get("pict") == pv)]
        except Exception:
            pass

    page = request.args.get("page", 1, type=int)
    total_items = len(products_all)
    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(1, min(page, total_pages))
    start, end = (page-1)*ITEMS_PER_PAGE, page*ITEMS_PER_PAGE
    paginated = products_all[start:end]

    window = 5
    half = window // 2
    start_p = max(1, page - half)
    end_p = min(total_pages, start_p + window - 1)
    start_p = max(1, end_p - window + 1)
    pages = list(range(start_p, end_p + 1))

    return paginated, total_items, total_pages, pages, page

def _inventory_ctx_from_access():
    products_all = list(_all_products_from_access())
    paginated, total_items, total_pages, pages, page = _filter_sort_paginate(products_all)

    cats = sorted({p.get("category") or "Uncategorized" for p in products_all})
    vols = sorted({int(p["volume_ml"]) for p in products_all if p.get("volume_ml")})
    pict_values = sorted({p["pict"] for p in products_all if p.get("pict") is not None})

    return {
        "products": paginated,
        "categories": ["All"] + cats,
        "volumes": vols,
        "pict_values": pict_values,
        "selected_category": (request.values.get("filter_category") or "All").strip(),
        "selected_volume": (request.values.get("volume") or "").strip(),
        "selected_pict": (request.values.get("pict") or "").strip(),
        "page": page,
        "total_pages": total_pages,
        "pages": pages,
        "per_page": ITEMS_PER_PAGE,
        "total_items": total_items,
    }

def _inventory_ctx_from_mongo():
    products_all = list(_all_products_from_mongo())
    paginated, total_items, total_pages, pages, page = _filter_sort_paginate(products_all)

    cats = sorted({p.get("category") or "Uncategorized" for p in products_all})
    vols = sorted({int(p["volume_ml"]) for p in products_all if p.get("volume_ml")})
    pict_values = sorted({p["pict"] for p in products_all if p.get("pict") is not None})

    return {
        "products": paginated,
        "categories": ["All"] + cats,
        "volumes": vols,
        "pict_values": pict_values,
        "selected_category": (request.values.get("filter_category") or "All").strip(),
        "selected_volume": (request.values.get("volume") or "").strip(),
        "selected_pict": (request.values.get("pict") or "").strip(),
        "page": page,
        "total_pages": total_pages,
        "pages": pages,
        "per_page": ITEMS_PER_PAGE,
        "total_items": total_items,
    }

def _inventory_ctx():
    return _inventory_ctx_from_access() if _should_use_access() else _inventory_ctx_from_mongo()

# ---------------- Pricing page + debug ----------------
@app.route("/pricing/<uid>")
@login_required
def view_pricing(uid):
    prices = _get_prices_for_part(uid)
    if not prices:
        flash("No pricing found for this item (neither in Mongo nor Access).", "info")
        return redirect(url_for("admin_dashboard") if session.get("role")=="admin" else url_for("index"))
    return render_template("pricing.html", prices=prices)

@app.route("/debug_pricing/<uid>")
@login_required
def debug_pricing(uid):
    prices = _get_prices_for_part(uid)
    return prices or {"ok": False, "msg": "No pricing found in Mongo or Access"}

# ---------------- Dashboards ----------------
@app.route("/admin_dashboard", methods=["GET","POST"])
@role_required("admin")
def admin_dashboard():
    return render_template("admin_dashboard.html", role="admin", **_inventory_ctx())

@app.route("/index", methods=["GET","POST"])
@role_required("worker")
def index():
    return render_template("index.html", role="worker", **_inventory_ctx())

# ---------------- Read-only stubs ----------------
@app.route("/update_stock/<uid>", methods=["GET","POST"])
@role_required("admin")
def update_stock(uid):
    flash("Editing is disabled in this read-only build.", "info")
    return redirect(url_for("admin_dashboard"))

@app.route("/requests")
@role_required("admin")
def requests_page():
    flash("Requests are disabled in this read-only build.", "info")
    return redirect(url_for("admin_dashboard"))

@app.route("/my_requests")
@role_required("worker")
def my_requests():
    flash("Requests are disabled in this read-only build.", "info")
    return redirect(url_for("index"))

@app.route("/submit_request/<uid>/<location>", methods=["GET","POST"])
@login_required
def submit_request(uid, location):
    flash("Submitting requests is disabled in this read-only build.", "info")
    return redirect(url_for("search_admin" if session.get("role")=="admin" else "search_worker"))

@app.route("/delete_product/<uid>", methods=["POST"])
@role_required("admin")
def delete_product(uid):
    flash("Delete is disabled in this read-only build.", "info")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin_logs")
@role_required("admin")
def admin_logs_stub():
    flash("Admin logs are disabled in this read-only build.", "info")
    return redirect(url_for("admin_dashboard"))

# ---------------- Search ----------------
def _render_search(role):
    result, message = [], None

    # Support GET ?q=... for deep links / QR fallbacks
    q_get = (request.args.get("q") or "").strip().lower()

    if request.method == "POST" or q_get:
        q = (request.form.get("search_uid") or q_get or "").strip().lower()
        selected_cat  = (request.values.get("filter_category") or "All").strip()
        selected_vol  = (request.values.get("volume") or "").strip()
        selected_pict = (request.values.get("pict") or "").strip()

        rows = _all_products()
        if q:
            keys = q.split()
            rows = [
                p for p in rows
                if all(k in " ".join([
                        p.get("uid",""),
                        p.get("name",""),
                        p.get("readable_id","")
                    ]).lower() for k in keys)
            ]
        if selected_cat != "All":
            rows = [p for p in rows if (p.get("category") or "Uncategorized") == selected_cat]
        if selected_vol:
            try:
                v = int(selected_vol)
                rows = [p for p in rows if p.get("volume_ml") == v]
            except Exception:
                pass
        if selected_pict:
            try:
                pv = int(selected_pict)
                rows = [p for p in rows if (p.get("pict") is not None and p.get("pict") == pv)]
            except Exception:
                pass

        result = rows
        if not result:
            message = "No matching products found."

    all_rows = _all_products()
    cats = sorted({p.get("category") or "Uncategorized" for p in all_rows})
    vols = sorted({int(p["volume_ml"]) for p in all_rows if p.get("volume_ml")})
    pict_values = sorted({p["pict"] for p in all_rows if p.get("pict") is not None})

    return render_template(
        "search.html",
        result=result,
        message=message,
        role=role,
        categories=["All"] + cats,
        volumes=vols,
        pict_values=pict_values
    )

@app.route("/search/admin", methods=["GET","POST"])
@role_required("admin")
def search_admin(): return _render_search("admin")

@app.route("/search/worker", methods=["GET","POST"])
@role_required("worker")
def search_worker(): return _render_search("worker")

# ---------------- 404 ----------------
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
