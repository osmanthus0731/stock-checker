# tools/import_products_from_access.py
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

ACCESS_TABLE = os.getenv("ACCESS_TABLE", "ITMMST").strip()
ACCESS_DB_PATH = (os.getenv("ACCESS_DB_PATH") or "").strip()
ACCESS_CONN_STR = (os.getenv("ACCESS_CONN_STR") or "").strip()

MONGO_URI = (os.getenv("MONGO_URI") or "mongodb://localhost:27017").strip()
MONGO_DB  = (os.getenv("MONGO_DB")  or "inventory").strip()

DEBUG = os.getenv("IMPORT_DEBUG", "0") == "1"
DEBUG_UID = (os.getenv("IMPORT_DEBUG_UID") or "").strip()

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
    # Mongo
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    products = db["products"]
    products.create_index([("uid", ASCENDING)], unique=True)

    # Access
    cn = access_connect()
    cur = cn.cursor()
    cur.execute(f"SELECT * FROM [{ACCESS_TABLE}]")

    raw_cols = [c[0] for c in cur.description]
    # Normalize col names: strip spaces + lowercase
    cols_norm = [str(c).strip().lower() for c in raw_cols]

    if DEBUG:
        print("ACCESS_DB_PATH:", ACCESS_DB_PATH)
        print("ACCESS_TABLE:", ACCESS_TABLE)
        print("First 30 columns from Access (normalized):")
        print(cols_norm[:30])

    def g(rec_norm: dict, *keys, default=""):
        """keys must be normalized (lowercase)"""
        for k in keys:
            k2 = str(k).strip().lower()
            if k2 in rec_norm and rec_norm.get(k2) is not None:
                return rec_norm.get(k2)
        return default

    # Location + qty columns (normalized!)
    LOCATION_PAIRS = [
        (("loc_a", "loca"), ("stocka", "qtya")),
        (("loc_b", "locb"), ("stockb", "qtyb")),
        # extend if needed:
        # (("loc_c", "locc"), ("stockc", "qtyc")),
    ]

    CATEGORY_KEYS = ("cat", "category", "prodgroup", "group", "type")
    TOTAL_KEYS = ("stock_office", "stock", "total", "qtytotal", "qty_total")

    upserts = 0
    skipped = 0

    rows = cur.fetchall()
    for row in rows:
        rec_raw = dict(zip(raw_cols, row))
        # Normalize keys once per row
        rec = {str(k).strip().lower(): v for k, v in rec_raw.items()}

        uid   = (g(rec, "uid", "part_id", "partid", "part-id") or "").strip()
        if not uid:
            skipped += 1
            continue

        name  = (g(rec, "desc", "description", "name") or "").strip()
        mssid = (g(rec, "mssid", "readable_id", "mss id") or "").strip()

        cat   = (g(rec, *CATEGORY_KEYS) or "").strip() or "Uncategorized"
        pict  = to_int(g(rec, "pict"), default=None)

        # Build locations
        locs = []
        for loc_keys, qty_keys in LOCATION_PAIRS:
            loc_val = (g(rec, *loc_keys) or "").strip()
            qty_val = to_int(g(rec, *qty_keys), default=0)
            if loc_val:
                locs.append({"area": loc_val, "quantity": qty_val})

        # Total stock
        total_from_access = None
        for k in TOTAL_KEYS:
            v = rec.get(k)
            if v is not None and str(v).strip() != "":
                total_from_access = to_int(v, default=None)
                break

        sum_locations = sum((x.get("quantity") or 0) for x in locs)
        total = total_from_access if total_from_access is not None else sum_locations

        vml = vol_ml(name) or vol_ml(mssid)

        if DEBUG and (not DEBUG_UID or uid == DEBUG_UID):
            print("\n--- DEBUG ROW ---")
            print("uid:", uid)
            print("loc_a:", g(rec, "loc_a"), "stocka:", g(rec, "stocka"))
            print("loc_b:", g(rec, "loc_b"), "stockb:", g(rec, "stockb"))
            print("locs built:", locs)
            if DEBUG_UID and uid == DEBUG_UID:
                # stop early for single-item debug
                pass

        doc = {
            "uid": uid,
            "name": name,
            "readable_id": mssid,
            "category": cat,
            "pict": pict,
            "locations": locs,         
            "stock": total,
            "volume_ml": vml,
            "_source": "access",
            "_imported_at": int(time.time()),
        }

        products.update_one({"uid": uid}, {"$set": doc}, upsert=True)
        upserts += 1

        if DEBUG_UID and uid == DEBUG_UID:
            # If debugging a specific UID, stop after importing it
            break

    cn.close()
    print(f"Upserted {upserts} product docs into Mongo. Skipped {skipped} rows (missing uid).")

if __name__ == "__main__":
    run()
