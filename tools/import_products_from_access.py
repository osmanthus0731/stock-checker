# tools/import_products_from_access.py
#  Imports products from Access (ITMMST) into MongoDB (upsert by uid)
#  Supports BOTH location schemas:
#    1) Single-location column: Loc_Film_box (+ qty from Stock / StockA / Total...)
#    2) Multi-location columns: Loc_A/StockA and Loc_B/StockB (extendable)
#  Normalizes Access column names using strip().lower() to avoid ODBC weirdness
#  Stores Mongo field: "locations" as a list of {area, quantity}
#  Sets "stock" from Access total if available; otherwise sums location quantities
#  Optional debug: set IMPORT_DEBUG=1 and optionally IMPORT_DEBUG_UID=<uid>

import os
import re
import time
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING

load_dotenv()

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

DEBUG = os.getenv("IMPORT_DEBUG", "0") == "1"
DEBUG_UID = (os.getenv("IMPORT_DEBUG_UID") or "").strip()

VOL_RE = re.compile(r"(\d{1,5})\s*ml\b", re.IGNORECASE)

# ---------------- Helpers ----------------
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
    """Robust int conversion for Access values that may be float/Decimal/str/None."""
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
    return odbc.connect(
        r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
        rf"DBQ={ACCESS_DB_PATH};"
        r"READONLY=TRUE;"
    )

def run():
    # ---------------- Mongo ----------------
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    products = db["products"]
    products.create_index([("uid", ASCENDING)], unique=True)

    # ---------------- Access ----------------
    cn = access_connect()
    cur = cn.cursor()
    cur.execute(f"SELECT * FROM [{ACCESS_TABLE}]")

    raw_cols = [c[0] for c in cur.description]
    cols_norm = [str(c).strip().lower() for c in raw_cols]

    if DEBUG:
        print("ACCESS_DB_PATH:", ACCESS_DB_PATH)
        print("ACCESS_TABLE:", ACCESS_TABLE)
        print("=== ACCESS COLUMNS (normalized) ===")
        print(cols_norm)

    def g(rec: dict, *keys, default=""):
        """Get first non-null value from rec using normalized keys (lowercase/strip)."""
        for k in keys:
            k2 = str(k).strip().lower()
            if k2 in rec and rec.get(k2) is not None:
                return rec.get(k2)
        return default

    # UID / NAME / MSSID candidate keys (normalized)
    UID_KEYS = ("uid", "part_id", "partid", "part-id", "part id")
    NAME_KEYS = ("desc", "description", "name")
    MSSID_KEYS = ("mssid", "readable_id", "readable id", "mss id")

    # Category candidates (adjust anytime)
    CATEGORY_KEYS = ("cat", "category", "prodgroup", "group", "type")

    # Total stock candidates (if present in table)
    TOTAL_KEYS = ("stock_office", "stock", "total", "qtytotal", "qty_total", "stock_office")

    # Location A is Loc_film_box / StockA; Location B is Loc_wh / StockB.
    # Keep legacy alternatives for older copies, but never discard B merely because A exists.
    MULTI_LOCATION_PAIRS = [
        (("loc_film_box", "loc_filmbox", "loc_film box", "loc_a", "loca"), ("stocka", "qtya"), "A"),
        (("loc_wh", "locwh", "loc_wharehouse", "loc_b", "locb"), ("stockb", "qtyb"), "B"),
    ]

    upserts = 0
    skipped = 0

    rows = cur.fetchall()
    for row in rows:
        rec_raw = dict(zip(raw_cols, row))
        # Normalize keys once per row to avoid Access/ODBC spacing/case issues
        rec = {str(k).strip().lower(): v for k, v in rec_raw.items()}

        uid = (g(rec, *UID_KEYS) or "").strip()
        if not uid:
            skipped += 1
            continue

        if DEBUG_UID and uid != DEBUG_UID:
            continue

        name = (g(rec, *NAME_KEYS) or "").strip()
        mssid = (g(rec, *MSSID_KEYS) or "").strip()
        cat = (g(rec, *CATEGORY_KEYS) or "").strip() or "Uncategorized"
        pict = to_int(g(rec, "pict"), default=None)

        # ---------------- Build locations (supports both schemas) ----------------
        locations = []

        slots = {}
        for loc_keys, qty_keys, slot in MULTI_LOCATION_PAIRS:
            loc_val = str(g(rec, *loc_keys) or "").strip()
            qty_val = to_int(g(rec, *qty_keys), default=0)
            slots[slot] = {"location": loc_val, "quantity": qty_val}
            if loc_val:
                locations.append({"slot": slot, "area": loc_val, "quantity": qty_val})

        # ---------------- Total stock ----------------
        total_from_access = None
        for k in TOTAL_KEYS:
            v = rec.get(k)
            if v is not None and str(v).strip() != "":
                total_from_access = to_int(v, default=None)
                break

        sum_locations = slots["A"]["quantity"] + slots["B"]["quantity"]
        total_stock = total_from_access if total_from_access is not None else sum_locations
        total_consistent = total_stock == sum_locations

        vml = vol_ml(name) or vol_ml(mssid)

        if DEBUG:
            print("\n--- DEBUG ROW ---")
            print("uid:", uid)
            print("location_a:", slots["A"])
            print("location_b:", slots["B"])
            print("loc_a:", g(rec, "loc_a"), "stocka:", g(rec, "stocka"))
            print("loc_b:", g(rec, "loc_b"), "stockb:", g(rec, "stockb"))
            print("locations built:", locations)
            print("total_from_access:", total_from_access, "sum_locations:", sum_locations, "final_stock:", total_stock)

        doc = {
            "uid": uid,
            "name": name,
            "readable_id": mssid,
            "category": cat,
            "pict": pict,
            "locations": locations,          # read from here
            "location_a": slots["A"]["location"],
            "stock_a": slots["A"]["quantity"],
            "location_b": slots["B"]["location"],
            "stock_b": slots["B"]["quantity"],
            "stock": total_stock,
            "stock_total_consistent": total_consistent,
            "access_confirmed": ({
                "a": slots["A"]["quantity"], "b": slots["B"]["quantity"], "total": total_stock,
                "location_a": slots["A"]["location"], "location_b": slots["B"]["location"],
            } if total_consistent else None),
            "stock_revision": 0,
            "sync_status": "synced" if total_consistent else "conflict",
            "supplier": str(g(rec, "supplr", "supplier") or "").strip(),
            "volume_ml": vml,
            "_source": "access",
            "_imported_at": int(time.time()),
        }

        products.update_one({"uid": uid}, {"$set": doc}, upsert=True)
        upserts += 1

        if DEBUG_UID:
            # If debugging one UID, stop after importing it
            break

    cn.close()
    print(f"Upserted {upserts} product docs into Mongo. Skipped {skipped} rows (missing uid).")

if __name__ == "__main__":
    run()
