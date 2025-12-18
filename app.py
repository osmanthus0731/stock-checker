# app.py — Mongo-first (Vercel-safe) inventory app with login + user management + Presence (online users)
from flask import (
    Flask, render_template, render_template_string,
    request, redirect, url_for, session, flash, send_file, jsonify
)
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
from io import BytesIO
import os, qrcode, certifi, time, re

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret")
IS_VERCEL = os.getenv("VERCEL") == "1"

# ---------------- ENV ----------------
MONGO_URI = (os.getenv("MONGO_URI") or os.getenv("MONGO_URL") or "").strip()
MONGO_DB = (os.getenv("MONGO_DB") or "inventory").strip()
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set")

FORCE_MONGO_ONLY = os.getenv("FORCE_MONGO_ONLY", "0") == "1"

# ---------------- Field mapping (UI/form -> DB schema) ----------------
FIELD_MAP = {"location": "Loc_FilmBox"}
def map_field(ui_field: str) -> str:
    return FIELD_MAP.get(ui_field, ui_field)
DB_TO_UI = {v: k for k, v in FIELD_MAP.items()}

# ---------------- Emergency fallback admin (LAST RESORT) ----------------
FALLBACK_ADMIN = {
    "username": os.getenv("FALLBACK_ADMIN_USERNAME", "admin"),
    "password": os.getenv("FALLBACK_ADMIN_PASSWORD", "admin12345"),
    "role": os.getenv("FALLBACK_ADMIN_ROLE", "admin"),
}

BOOTSTRAP_USERS = os.getenv("BOOTSTRAP_USERS", "0") == "1"
BOOTSTRAP_ADMIN = {
    "username": os.getenv("BOOTSTRAP_ADMIN_USERNAME", FALLBACK_ADMIN["username"]),
    "password": os.getenv("BOOTSTRAP_ADMIN_PASSWORD", FALLBACK_ADMIN["password"]),
    "role": "admin",
}

# ---------------- Mongo client ----------------
_tls = {
    "serverSelectionTimeoutMS": 45000,
    "connectTimeoutMS": 20000,
    "socketTimeoutMS": 20000,
}
if MONGO_URI.startswith("mongodb+srv://") or "mongodb.net" in MONGO_URI:
    _tls.update({"tls": True, "tlsCAFile": certifi.where()})

client = MongoClient(MONGO_URI, **_tls)
db = client[MONGO_DB]

users = db.get_collection("users")
products_col = db.get_collection("products")
pricing_col = db.get_collection("pricing")
admin_logs = db.get_collection("admin_logs")
submissions = db.get_collection("submissions")
presence = db.get_collection("presence")  # ✅ presence collection

def _bootstrap_admin_if_needed():
    if not BOOTSTRAP_USERS:
        return
    try:
        client.admin.command("ping")
        if users.estimated_document_count() == 0:
            users.insert_one({
                "username": BOOTSTRAP_ADMIN["username"],
                "password": generate_password_hash(BOOTSTRAP_ADMIN["password"]),
                "role": "admin",
                "created_at": int(time.time()),
                "bootstrap": True,
            })
            app.logger.warning("Bootstrapped admin user '%s' (users empty).", BOOTSTRAP_ADMIN["username"])
    except Exception as e:
        app.logger.exception("Bootstrap admin failed: %s", e)

_bootstrap_admin_if_needed()

# ---------- No-cache headers ----------
@app.after_request
def add_no_cache_headers(resp):
    if not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

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

def _is_hash(s: str) -> bool:
    return isinstance(s, str) and (s.startswith("scrypt:") or s.startswith("pbkdf2:") or s.startswith("argon2:"))

def _password_matches(stored: str, supplied: str) -> bool:
    stored = stored or ""
    supplied = supplied or ""
    if _is_hash(stored):
        return check_password_hash(stored, supplied)
    return stored == supplied

# ---------------- Inventory helpers ----------------
ITEMS_PER_PAGE = 5                 # dashboard
SEARCH_ITEMS_PER_PAGE = 20         # search page

_VOL_RE = re.compile(r"(\d{1,5})\s*ml\b", re.IGNORECASE)

def _extract_volume_ml(text: str):
    if not text:
        return None
    m = _VOL_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

def _coerce_int(x, default=0):
    try:
        if x is None:
            return default
        s = str(x).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default

def _normalize_locations_from_legacy_fields(p: dict):
    legacy_loc = (p.get("Loc_FilmBox") or p.get("location") or "").strip()
    return legacy_loc or None

def _normalize_mongo_product(p: dict) -> dict:
    if not p:
        return {
            "uid": "",
            "name": "",
            "readable_id": "",
            "category": "Uncategorized",
            "pict": None,
            "stock": 0,
            "volume_ml": None,
            "locations": [],
            "location": "",
        }

    out = {
        "uid": p.get("uid", "") or p.get("Part_id", "") or "",
        "name": p.get("name", "") or p.get("Desc", "") or "",
        "readable_id": p.get("readable_id", "") or p.get("Mssid", "") or "",
        "category": p.get("category", "Uncategorized") or "Uncategorized",
        "pict": p.get("pict", None),
        "stock": p.get("stock"),
        "volume_ml": p.get("volume_ml"),
    }

    locs = p.get("locations") or {}
    normalized_locations = []

    if isinstance(locs, dict):
        normalized_locations = [
            {"area": str(k).strip(), "quantity": _coerce_int(v, 0)}
            for k, v in locs.items()
            if str(k).strip()
        ]
    elif isinstance(locs, list):
        for L in locs:
            if not isinstance(L, dict):
                continue
            area = (L.get("area") or L.get("loc") or "").strip()
            qty = _coerce_int(L.get("quantity"), 0)
            if area:
                normalized_locations.append({"area": area, "quantity": qty})

    if not normalized_locations:
        legacy_loc = _normalize_locations_from_legacy_fields(p)
        if legacy_loc:
            legacy_qty = _coerce_int(p.get("stock"), 0)
            normalized_locations = [{"area": legacy_loc, "quantity": legacy_qty}]

    out["locations"] = normalized_locations

    if out.get("volume_ml") is None:
        out["volume_ml"] = _extract_volume_ml(out.get("name", "")) or _extract_volume_ml(out.get("readable_id", ""))

    if out.get("stock") is None:
        out["stock"] = sum((i.get("quantity") or 0) for i in out["locations"])

    out["location"] = out["locations"][0]["area"] if out["locations"] else ""
    return out

def _all_products_from_mongo():
    try:
        docs = list(products_col.find({}, {"_id": 0}))
        return [_normalize_mongo_product(p) for p in docs]
    except Exception as e:
        app.logger.exception("Mongo read failed in _all_products_from_mongo: %s", e)
        try:
            flash("Database is unavailable right now. Showing an empty list.", "error")
        except Exception:
            pass
        return []

def _get_product_by_uid_mongo(uid: str):
    try:
        p = products_col.find_one({"uid": uid}, {"_id": 0})
        if not p:
            p = products_col.find_one({"Part_id": uid}, {"_id": 0})
        return _normalize_mongo_product(p) if p else None
    except Exception as e:
        app.logger.exception("Mongo read failed in _get_product_by_uid_mongo: %s", e)
        try:
            flash("Database is unavailable right now.", "error")
        except Exception:
            pass
        return None

def _all_products():
    return _all_products_from_mongo()

def _get_product_by_uid(uid: str):
    return _get_product_by_uid_mongo(uid)

def _all_locations_list():
    rows = _all_products()
    loc_set = set()
    for p in rows:
        for L in (p.get("locations") or []):
            area = (L.get("area") or "").strip()
            if area:
                loc_set.add(area)
        legacy = (p.get("Loc_FilmBox") or p.get("location") or "").strip()
        if legacy:
            loc_set.add(legacy)
    return sorted(loc_set)

# ---------------- Pricing ----------------
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
    for k, v in (doc or {}).items():
        k_up = str(k).upper()
        if k_up in mapping:
            bucket[mapping[k_up]] = _coerce_num(v)

def _get_prices_for_part_mongo(uid_or_part: str):
    try:
        key_raw = (uid_or_part or "").strip()
        if not key_raw:
            return None

        prod = _get_product_by_uid(uid_or_part) or {}
        uid_candidate = (prod.get("uid") or key_raw).strip()
        mssid_candidate = (prod.get("readable_id") or "").strip()

        candidates = {c for c in [uid_candidate, mssid_candidate, key_raw] if c}

        rows = list(pricing_col.find(
            {
                "part_id": {"$in": list(candidates)},
                "$or": [{"priced": {"$in": ["P", "S"]}}, {"pricecd": {"$in": ["P", "S"]}}],
            },
            {"_id": 0}
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
            if code in grouped:
                grouped[code].append(r)

        for code in ["P", "S"]:
            if grouped[code]:
                latest = sorted(grouped[code], key=key_dt, reverse=True)[0]
                _merge_tier(latest, result[code])

        return result
    except Exception as e:
        app.logger.exception("Mongo read failed in _get_prices_for_part_mongo: %s", e)
        return None

def _get_prices_for_part(uid: str):
    return _get_prices_for_part_mongo(uid)

# ---------------- Health ----------------
@app.route("/ping")
def ping():
    return "pong"

@app.route("/dbcheck")
def dbcheck():
    try:
        client.admin.command("ping")
        n_users = users.estimated_document_count()
        n_products = products_col.estimated_document_count()
        n_presence = presence.estimated_document_count()
        return {"ok": True, "ping": "ok", "users": int(n_users), "products": int(n_products), "presence": int(n_presence)}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 503

@app.route("/favicon.ico")
def favicon():
    return "", 204

# ---------------- Auth ----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "GET" and "username" in session:
        next_url = request.args.get("next")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("admin_dashboard") if session.get("role") == "admin" else url_for("index"))

    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = (request.form.get("password") or "")

        try:
            user = users.find_one({"username": u})
            if user:
                stored_pw = user.get("password", "")
                if _password_matches(stored_pw, p):
                    if not _is_hash(stored_pw):
                        users.update_one(
                            {"_id": user["_id"]},
                            {"$set": {"password": generate_password_hash(p), "upgraded_at": int(time.time())}}
                        )
                    session["username"] = user.get("username", u)
                    session["role"] = user.get("role", "worker")

                    next_url = request.args.get("next") or session.pop("post_login_next", None)
                    if next_url:
                        return redirect(next_url)
                    return redirect(url_for("admin_dashboard") if session.get("role") == "admin" else url_for("index"))
        except Exception as e:
            app.logger.exception("Login Mongo lookup failed: %s", e)

        if u == FALLBACK_ADMIN["username"] and p == FALLBACK_ADMIN["password"]:
            session["username"] = FALLBACK_ADMIN["username"]
            session["role"] = FALLBACK_ADMIN["role"]
            next_url = request.args.get("next") or session.pop("post_login_next", None)
            if next_url:
                return redirect(next_url)
            return redirect(url_for("admin_dashboard"))

        flash("Invalid credentials", "error")

    return render_template("login.html")

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

# ---------------- QR ----------------
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

def _qr_target_url(uid: str) -> str:
    path = url_for("item_detail", uid=uid)
    if BASE_URL:
        return f"{BASE_URL}{path}"
    return url_for("item_detail", uid=uid, _external=True)

@app.route("/qr/<uid>.png")
def qr(uid: str):
    target = _qr_target_url(uid)
    img = qrcode.make(target)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"{uid}.png")

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

# ---------- Create user ----------
@app.route("/users/new", methods=["GET", "POST"])
@role_required("admin")
def create_user():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        role = (request.form.get("role") or "worker").strip().lower()
        role = "admin" if role == "admin" else "worker"

        if not u or not p:
            flash("Username and password are required.", "error")
            return render_template("new_user.html", username=u, role=role)

        if users.find_one({"username": u}):
            flash("Username already exists. Choose another.", "error")
            return render_template("new_user.html", username=u, role=role)

        users.insert_one({
            "username": u,
            "password": generate_password_hash(p),
            "role": role,
            "full_name": (request.form.get("full_name") or "").strip(),
            "email": (request.form.get("email") or "").strip(),
            "phone": (request.form.get("phone") or "").strip(),
            "created_at": int(time.time()),
        })
        flash(f"User '{u}' created ({role}).", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("new_user.html")

# ---------- Profile ----------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    me = users.find_one({"username": session["username"]}) or {}

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_profile":
            users.update_one(
                {"username": session["username"]},
                {"$set": {
                    "full_name": (request.form.get("full_name") or "").strip(),
                    "email": (request.form.get("email") or "").strip(),
                    "phone": (request.form.get("phone") or "").strip(),
                }}
            )
            flash("Profile updated.", "success")
            return redirect(url_for("profile"))

        if action == "change_password":
            current = request.form.get("current_password") or ""
            new1 = request.form.get("new_password") or ""
            new2 = request.form.get("confirm_password") or ""

            if not _password_matches(me.get("password", ""), current):
                flash("Current password is incorrect.", "error")
                return redirect(url_for("profile"))

            if len(new1) < 6:
                flash("New password must be at least 6 characters.", "error")
                return redirect(url_for("profile"))

            if new1 != new2:
                flash("New passwords do not match.", "error")
                return redirect(url_for("profile"))

            users.update_one(
                {"username": session["username"]},
                {"$set": {"password": generate_password_hash(new1), "pw_changed_at": int(time.time())}}
            )
            flash("Password changed.", "success")
            return redirect(url_for("profile"))

    me = users.find_one({"username": session["username"]}) or {}
    return render_template("profile.html", me=me)

# ---------------- Dashboard pagination + filters ----------------
def _matches_location(p, q: str) -> bool:
    q = (q or "").strip().lower()
    if not q:
        return True
    for L in (p.get("locations") or []):
        area = (L.get("area") or "").strip().lower()
        if q in area:
            return True
    legacy = (p.get("Loc_FilmBox") or p.get("location") or "").strip().lower()
    return bool(legacy) and (q in legacy)

def _filter_sort_paginate(products_all):
    selected_cat = (request.args.get("filter_category") or "All").strip()
    selected_vol = (request.args.get("volume") or "").strip()
    location_q = (request.args.get("location") or "").strip()

    if selected_cat != "All":
        products_all = [p for p in products_all if (p.get("category") or "Uncategorized") == selected_cat]

    if selected_vol:
        try:
            v = int(selected_vol)
            products_all = [p for p in products_all if p.get("volume_ml") == v]
        except Exception:
            pass

    if location_q:
        products_all = [p for p in products_all if _matches_location(p, location_q)]

    page = request.args.get("page", 1, type=int)
    total_items = len(products_all)
    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    paginated = products_all[start:end]

    window = 5
    half = window // 2
    start_p = max(1, page - half)
    end_p = min(total_pages, start_p + window - 1)
    start_p = max(1, end_p - window + 1)
    pages = list(range(start_p, end_p + 1))

    return paginated, total_items, total_pages, pages, page

def _inventory_ctx_from_mongo():
    products_all = list(_all_products_from_mongo())
    paginated, total_items, total_pages, pages, page = _filter_sort_paginate(products_all)

    cats = sorted({p.get("category") or "Uncategorized" for p in products_all})
    vols = sorted({int(p["volume_ml"]) for p in products_all if p.get("volume_ml")})

    return {
        "products": paginated,
        "categories": ["All"] + cats,
        "volumes": vols,

        "selected_category": (request.args.get("filter_category") or "All").strip(),
        "selected_volume": (request.args.get("volume") or "").strip(),
        "location": (request.args.get("location") or "").strip(),

        "page": page,
        "total_pages": total_pages,
        "pages": pages,
        "per_page": ITEMS_PER_PAGE,
        "total_items": total_items,

        "nopict_count": sum(1 for p in paginated if not p.get("pict")),
    }

def _inventory_ctx():
    return _inventory_ctx_from_mongo()

# ---------------- Pricing page ----------------
@app.route("/pricing/<uid>")
@login_required
def view_pricing(uid):
    prices = _get_prices_for_part(uid)
    if not prices:
        flash("No pricing found for this item.", "info")
        return redirect(url_for("admin_dashboard") if session.get("role") == "admin" else url_for("index"))
    return render_template("pricing.html", prices=prices)

@app.route("/debug_pricing/<uid>")
@login_required
def debug_pricing(uid):
    prices = _get_prices_for_part(uid)
    return jsonify(prices or {"ok": False, "msg": "No pricing found in Mongo"})

# ✅ Calculator route
@app.route("/calculator", methods=["GET"])
@login_required
def calculator_page():
    uid = (request.args.get("uid") or "").strip()
    prices = _get_prices_for_part(uid) if uid else None
    if uid and not prices:
        flash("No pricing found for that UID/MSSID.", "info")
    return render_template("calculator.html", prices=prices, uid=uid)

# ---------------- API endpoints for product suggest and pricing ----------------
@app.route("/api/products/suggest")
@login_required
def api_products_suggest():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    rx = re.compile(re.escape(q), re.IGNORECASE)

    cursor = products_col.find(
        {"$or": [
            {"uid": rx},
            {"name": rx},
            {"readable_id": rx},
            {"Part_id": rx},
            {"Desc": rx},
            {"Mssid": rx},
        ]},
        {"_id": 0, "uid": 1, "name": 1, "readable_id": 1, "Part_id": 1, "Desc": 1, "Mssid": 1}
    ).limit(20)

    out = []
    for d in cursor:
        uid = d.get("uid") or d.get("Part_id") or ""
        name = d.get("name") or d.get("Desc") or ""
        rid = d.get("readable_id") or d.get("Mssid") or ""
        if uid or name:
            out.append({"uid": uid, "name": name, "readable_id": rid})
    return jsonify(out)

@app.route("/api/pricing/<uid>")
@login_required
def api_pricing(uid):
    prices = _get_prices_for_part(uid)
    return jsonify(prices or {})

# =========================
# Presence (Online users)
# =========================
PRESENCE_TTL_SECONDS = 90  # online if pinged within last 90s

def _ensure_presence_indexes():
    try:
        presence.create_index("username", unique=True)
    except Exception:
        pass
    try:
        presence.create_index("last_seen_at")
    except Exception:
        pass

_ensure_presence_indexes()

@app.route("/api/presence/ping", methods=["POST"])
@login_required
def api_presence_ping():
    now = int(time.time())
    username = (session.get("username") or "").strip()
    role = (session.get("role") or "worker").strip()
    if not username:
        return jsonify({"ok": False}), 401

    presence.update_one(
        {"username": username},
        {"$set": {"username": username, "role": role, "last_seen_at": now}},
        upsert=True
    )

    # optional: also write last_seen in users collection (nice for auditing)
    try:
        users.update_one({"username": username}, {"$set": {"last_seen_at": now}})
    except Exception:
        pass

    return jsonify({"ok": True, "ts": now})

@app.route("/api/presence/status", methods=["GET"])
@role_required("admin")
def api_presence_status():
    now = int(time.time())
    cutoff = now - PRESENCE_TTL_SECONDS

    pres_docs = list(presence.find({}, {"_id": 0, "username": 1, "role": 1, "last_seen_at": 1}))
    last_map = {}
    role_map = {}
    for d in pres_docs:
        u = (d.get("username") or "").strip()
        if not u:
            continue
        last_map[u] = int(d.get("last_seen_at") or 0)
        role_map[u] = d.get("role") or ""

    all_users = list(users.find({}, {"_id": 0, "username": 1, "role": 1}).sort("username", 1))

    online = []
    offline = []
    for u in all_users:
        uname = (u.get("username") or "").strip()
        if not uname:
            continue

        role = (u.get("role") or role_map.get(uname) or "worker").strip()
        last_seen = last_map.get(uname, 0)
        seconds_ago = (now - last_seen) if last_seen else None

        if last_seen and last_seen >= cutoff:
            online.append({"username": uname, "role": role, "seconds_ago": seconds_ago})
        else:
            offline.append({"username": uname, "role": role, "seconds_ago": seconds_ago})

    online.sort(key=lambda x: (x.get("seconds_ago") if x.get("seconds_ago") is not None else 10**9))
    offline.sort(key=lambda x: (x.get("username") or "").lower())

    return jsonify({
        "ok": True,
        "window": PRESENCE_TTL_SECONDS,
        "online": online,
        "offline": offline
    })

# ---------------- Dashboards ----------------
@app.route("/admin_dashboard", methods=["GET"])
@role_required("admin")
def admin_dashboard():
    return render_template("admin_dashboard.html", role="admin", **_inventory_ctx())

@app.route("/index", methods=["GET"])
@role_required("worker")
def index():
    ctx = _inventory_ctx()
    return render_template("index.html", role="worker", **ctx)

# ---------------- Read-only stubs ----------------
@app.route("/update_stock/<uid>", methods=["GET", "POST"])
@role_required("admin")
def update_stock(uid):
    flash("Editing is disabled in this build.", "info")
    return redirect(url_for("admin_dashboard"))

@app.route("/requests")
@role_required("admin")
def requests_page():
    flash("Requests are disabled in this build.", "info")
    return redirect(url_for("admin_dashboard"))

@app.route("/my_requests")
@role_required("worker")
def my_requests():
    flash("Requests are disabled in this build.", "info")
    return redirect(url_for("index"))

@app.route("/submit_request/<uid>/<location>", methods=["GET", "POST"])
@login_required
def submit_request(uid, location):
    flash("Submitting requests is disabled in this build.", "info")
    return redirect(url_for("search_admin" if session.get("role") == "admin" else "search_worker"))

@app.route("/delete_product/<uid>", methods=["POST"])
@role_required("admin")
def delete_product(uid):
    flash("Delete is disabled in this build.", "info")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin_logs")
@role_required("admin")
def admin_logs_stub():
    flash("Admin logs are disabled in this build.", "info")
    return redirect(url_for("admin_dashboard"))

# ---------------- Search ----------------
def _safe_int(v, default=0):
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default

def _sort_rows(rows, sort_by: str):
    sort_by = (sort_by or "name_asc").strip()

    if sort_by == "name_asc":
        return sorted(rows, key=lambda p: (p.get("name") or "").lower())
    if sort_by == "name_desc":
        return sorted(rows, key=lambda p: (p.get("name") or "").lower(), reverse=True)

    if sort_by == "uid_asc":
        return sorted(rows, key=lambda p: (p.get("uid") or "").lower())
    if sort_by == "uid_desc":
        return sorted(rows, key=lambda p: (p.get("uid") or "").lower(), reverse=True)

    if sort_by == "volume_asc":
        return sorted(rows, key=lambda p: (_safe_int(p.get("volume_ml"), 10**9), (p.get("name") or "").lower()))
    if sort_by == "volume_desc":
        return sorted(rows, key=lambda p: (_safe_int(p.get("volume_ml"), -1), (p.get("name") or "").lower()), reverse=True)

    if sort_by == "stock_asc":
        return sorted(rows, key=lambda p: (_safe_int(p.get("stock"), 10**9), (p.get("name") or "").lower()))
    if sort_by == "stock_desc":
        return sorted(rows, key=lambda p: (_safe_int(p.get("stock"), -1), (p.get("name") or "").lower()), reverse=True)

    return sorted(rows, key=lambda p: (p.get("name") or "").lower())

def _paginate(rows, page: int, per_page: int):
    total_items = len(rows)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    paginated = rows[start:end]

    window = 7
    half = window // 2
    start_p = max(1, page - half)
    end_p = min(total_pages, start_p + window - 1)
    start_p = max(1, end_p - window + 1)
    pages = list(range(start_p, end_p + 1))

    return paginated, total_items, total_pages, pages, page

def _render_search(role):
    result = []
    message = None

    q_get = (request.args.get("q") or "").strip().lower()
    q_form = (request.form.get("search_uid") or "").strip().lower()
    q = (q_form or q_get or "").strip()

    selected_cat = (request.values.get("filter_category") or "All").strip()
    selected_vol = (request.values.get("volume") or "").strip()
    selected_location = (request.values.get("location") or "").strip().lower()
    sort_by = (request.values.get("sort") or "name_asc").strip()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", SEARCH_ITEMS_PER_PAGE, type=int)
    per_page = max(5, min(per_page, 200))

    locations_list = _all_locations_list()

    if request.method == "POST" or q_get or selected_cat != "All" or selected_vol or selected_location:
        rows = _all_products()

        if q:
            keys = q.split()
            rows = [
                p for p in rows
                if all(k in " ".join([
                    p.get("uid", ""),
                    p.get("name", ""),
                    p.get("readable_id", ""),
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

        if selected_location:
            rows = [p for p in rows if _matches_location(p, selected_location)]

        rows = _sort_rows(rows, sort_by)
        result, total_items, total_pages, pages, page = _paginate(rows, page, per_page)

        if total_items == 0:
            message = "No matching products found."
    else:
        total_items, total_pages, pages = 0, 1, [1]

    all_rows = _all_products()
    cats = sorted({p.get("category") or "Uncategorized" for p in all_rows})
    vols = sorted({int(p["volume_ml"]) for p in all_rows if p.get("volume_ml")})

    return render_template(
        "search.html",
        result=result,
        message=message,
        role=role,
        categories=["All"] + cats,
        volumes=vols,
        locations_list=locations_list,

        sort_by=sort_by,
        selected_category=selected_cat,
        selected_volume=selected_vol,
        selected_location=(request.values.get("location") or "").strip(),

        page=page,
        total_pages=total_pages,
        pages=pages,
        per_page=per_page,
        total_items=total_items,
        q=q,
    )

@app.route("/search/admin", methods=["GET", "POST"])
@role_required("admin")
def search_admin():
    return _render_search("admin")

@app.route("/search/worker", methods=["GET", "POST"])
@role_required("worker")
def search_worker():
    return _render_search("worker")

# ---------------- 404 ----------------
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
