# tools/import_products_from_access.py
# Imports products from Microsoft Access into MongoDB (upsert by uid).
# ✅ Reads multi-location columns like Loc_A/StockA, Loc_B/StockB (extendable to C/D/E…)
# ✅ Writes Mongo field "locations" as a list of {area, quantity}
# ✅ Sets "stock" from Access total if present; otherwise sums location quantities
# ✅ Extracts volume_ml from name/mssid if possible
# ✅ Creates a unique index on uid

import os
import re
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING

load_dotenv()

# --- ODBC driver (pyodbc preferred; fallback to pypyodbc) ---
try:
    import pyodbc as odbc
except Exception:
    import pypyodbc as odbc  # type: ignore

# ---------------- ENV ----------------
ACCESS_TABLE = os.getenv("ACCESS_TABLE", "ITMMST").strip()
ACCESS_DB_PATH = (os.getenv("ACCESS_DB_PATH") or "").strip()
ACCESS_CONN_STR = (os.getenv("ACCESS_CONN_STR") or "").strip()

MONGO_URI = (os.getenv("MONGO_URI") or "mongodb://localhost:27017").strip()
MONGO_DB = (os.getenv("MONGO_DB") or "inventory").strip()

# Optional: If you want to force using ONLY summed location qty as stock
FORCE_STOCK_FROM_LOCATIONS = os.getenv("FORCE_STOCK_FROM_LOCATIONS", "0") == "1"

# ---------------- Helpers ----------------
VOL_RE = re.compile(r"(\d{1,5})\s*ml\b", re.IGNORECASE)

def vol_ml(text: str):
    if not text:
        return None
    m = VOL_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

def to_int(v, default=0):
    """Robust int conversion for Access values that might be float/Decimal/str/None."""
    try:
        if v in (None, ""):
            return default
        s = str(v).strip()
        if s == "":
            return default
        return int(float(s))
    except Exception:
        return default

def access_connect():
    if ACCESS_CONN_STR:
        return odbc.connect(ACCESS_CONN_STR)
    if not ACCESS_DB_PATH:
        raise RuntimeError("Set ACCESS_DB_PATH or ACCESS_CONN_STR in .env")

    # Standard Access ODBC connection string (Windows)
    return odbc.connect(
        r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
        rf"DBQ={ACCESS_DB_PATH};"
        r"READONLY=TRUE;"
    )

def run():
    # --- Mongo setup ---
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    products = db["products"]
    products.create_index([("uid", ASCENDING)], unique=True)

    # --- Access read ---
    cn = access_connect()
    cur = cn.cursor()
    cur.execute(f"SELECT * FROM [{ACCESS_TABLE}]")
    cols = [c[0] for c in cur.description]

    def g(rec: dict, *keys, default=""):
        """Get first non-null value for any matching key name."""
        for k in keys:
            if k in rec and rec.get(k) is not None:
                return rec.get(k)
        return default

    # 🔥 Map your Access schema here
    # Based on your screenshot: Loc_A + StockA, Loc_B + StockB
    # If later you add Loc_C/StockC etc, just extend the list.
    LOCATION_PAIRS = [
        (("Loc_A", "loc_a", "LocA", "LOCA"), ("StockA", "stocka", "QtyA", "qtya")),
        (("Loc_B", "loc_b", "LocB", "LOCB"), ("StockB", "stockb", "QtyB", "qtyb")),
        # Add more when needed:
        # (("Loc_C","loc_c","LocC"), ("StockC","stockc","QtyC")),
    ]

    # Possible total stock field names (if you have one)
    TOTAL_STOCK_KEYS = (
        "Stock_Office", "Stock_office", "stock_office",
        "Stock", "stock", "Total", "TOTAL",
        "QtyTotal", "qty_total"
    )

    # Category keys (adjust as needed)
    CATEGORY_KEYS = ("Cat", "cat", "Category", "ProdGroup", "Group", "Type")

    upserts = 0
    skipped = 0

    rows = cur.fetchall()
    for row in rows:
        rec = dict(zip(cols, row))

        uid = (g(rec, "uid", "UID", "Part_id", "part_id", "Part-ID", "PartID") or "").strip()
        if not uid:
            skipped += 1
            continue

        name = (g(rec, "name", "Name", "Desc", "desc", "Description") or "").strip()
        mssid = (g(rec, "readable_id", "Readable_ID", "Mssid", "mssid", "MSSID") or "").strip()
        cat = (g(rec, *CATEGORY_KEYS) or "").strip() or "Uncategorized"

        pict = to_int(g(rec, "Pict", "pict", "PICT"), default=None)

        # --- Build locations list from Access multi-location columns ---
        locs = []
        for loc_keys, qty_keys in LOCATION_PAIRS:
            loc_val = (g(rec, *loc_keys) or "").strip()
            qty_val = to_int(g(rec, *qty_keys), default=0)
            if loc_val:
                locs.append({"area": loc_val, "quantity": qty_val})

        # --- Stock logic ---
        # Prefer Access total stock if present, unless FORCE_STOCK_FROM_LOCATIONS=1
        total_from_access = None
        for k in TOTAL_STOCK_KEYS:
            if k in rec and rec.get(k) is not None and str(rec.get(k)).strip() != "":
                total_from_access = to_int(rec.get(k), default=None)
                break

        sum_locations = sum((x.get("quantity") or 0) for x in locs)

        if FORCE_STOCK_FROM_LOCATIONS:
            total_stock = sum_locations
        else:
            total_stock = total_from_access if total_from_access is not None else sum_locations

        vml = vol_ml(name) or vol_ml(mssid)

        doc = {
            "uid": uid,
            "name": name,
            "readable_id": mssid,
            "category": cat,
            "pict": pict,
            "locations": locs,          
            "stock": total_stock,
            "volume_ml": vml,
            "_source": "access",
            "_imported_at": int(__import__("time").time()),
        }

        products.update_one({"uid": uid}, {"$set": doc}, upsert=True)
        upserts += 1

    cn.close()
    print(f"Upserted {upserts} product docs into Mongo. Skipped {skipped} rows (missing uid).")

if __name__ == "__main__":
    run()
