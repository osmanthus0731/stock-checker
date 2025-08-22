# tools/import_pricing_from_access.py
# Robust Access -> Mongo importer for pricing (no index conflict)
import os, re
from datetime import datetime, date
from decimal import Decimal
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import OperationFailure

load_dotenv()

try:
    import pyodbc as odbc
except Exception:
    import pypyodbc as odbc

ACCESS_PRICE_TABLE = os.getenv("ACCESS_PRICE_TABLE", "Cus_Price")
ACCESS_DB_PATH     = os.getenv("ACCESS_DB_PATH")
ACCESS_CONN_STR    = os.getenv("ACCESS_CONN_STR")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "inventory")

TIER_KEYS = ("S12","S100","S500","1K","3K","5K","10K")

def nz(s):
    if s is None: return None
    s = str(s).strip()
    return s or None

def to_number(x):
    if x in (None, "", "NULL"): return None
    if isinstance(x, (int, float, Decimal)): return float(x)
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None

def to_date(x):
    if x in (None, "", "NULL"): return None
    if isinstance(x, datetime): return x
    if isinstance(x, date): return datetime(x.year, x.month, x.day)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(x), fmt)
        except Exception:
            pass
    return None

def normalize_key(k: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", k or "").upper()

def access_connect():
    if ACCESS_CONN_STR:
        return odbc.connect(ACCESS_CONN_STR)
    if not ACCESS_DB_PATH:
        raise RuntimeError("Set ACCESS_DB_PATH or ACCESS_CONN_STR in .env")
    return odbc.connect(
        r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
        rf"DBQ={ACCESS_DB_PATH};READONLY=TRUE;"
    )

def ensure_unique_index(coll):
    """Create (part_id, pricecd) unique index only if needed; fix non-unique."""
    wanted = "part_id_1_pricecd_1"
    info = coll.index_information()
    if wanted not in info:
        try:
            coll.create_index([("part_id", ASCENDING), ("pricecd", ASCENDING)],
                              name=wanted, unique=True)
        except OperationFailure as e:
            print(f"[info] index create skipped: {getattr(e, 'details', {}) or str(e)}")
    else:
        if not info[wanted].get("unique", False):
            try:
                coll.drop_index(wanted)
                coll.create_index([("part_id", ASCENDING), ("pricecd", ASCENDING)],
                                  name=wanted, unique=True)
                print("[fix] replaced non-unique index with unique one")
            except OperationFailure as e:
                print(f"[warn] could not replace index: {getattr(e, 'details', {}) or str(e)}")

def migrate_legacy_field(db):
    """Rename 'priced' -> 'pricecd' if it exists."""
    res = db["pricing"].update_many(
        {"priced": {"$exists": True}, "pricecd": {"$exists": False}},
        {"$rename": {"priced": "pricecd"}}
    )
    if res.modified_count:
        print(f"[migrate] Renamed 'priced' -> 'pricecd' in {res.modified_count} docs.")

def run():
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    pricing = db["pricing"]

    # SAFE index handling (prevents IndexKeySpecsConflict)
    ensure_unique_index(pricing)

    # Legacy field rename if needed
    migrate_legacy_field(db)

    cn = access_connect()
    cur = cn.cursor()
    cur.execute(f"SELECT * FROM [{ACCESS_PRICE_TABLE}]")
    raw_cols = [c[0] for c in cur.description]
    norm_cols = [normalize_key(c) for c in raw_cols]

    print(f"[info] Access table = {ACCESS_PRICE_TABLE}")
    print(f"[info] Columns ({len(raw_cols)}): {raw_cols}")
    print(f"[info] Normalized   : {norm_cols}")

    rows = cur.fetchall()
    print(f"[info] Fetched {len(rows)} rows from Access.")

    upserts = 0
    examined = 0

    KEYMAP_CANDIDATES = {
        "PART_ID": ("PART_ID", "PARTID", "PART", "PID", "ITEM", "ITEMCODE"),
        "PRICECD": ("PRICECD", "PRICE_CD", "PRICECODE", "PRICE_CODE"),
        "CURRENCY": ("CURRENCY",),
        "CUSTOMER": ("CUSTOMER", "CUST", "CUSTCODE", "CUSTOMERCODE"),
        "EFF_DATE": ("EFF_DATE", "EFFDATE", "EFFECTIVEDATE", "EFFECTIVE"),
        "S12": ("S12",),
        "S100": ("S100",),
        "S500": ("S500",),
        "S1000": ("S1000", "1K", "K1"),
        "S3000": ("S3000", "3K", "K3"),
        "S5000": ("S5000", "5K", "K5"),
        "S10000": ("S10000", "10K", "K10"),
    }

    norm_index = {normalize_key(k): i for i, k in enumerate(raw_cols)}

    def pick(row, *cands):
        for c in cands:
            idx = norm_index.get(normalize_key(c))
            if idx is not None:
                return row[idx]
        return None

    for row in rows:
        examined += 1

        part_id = nz(pick(row, *KEYMAP_CANDIDATES["PART_ID"]))
        pricecd = nz(pick(row, *KEYMAP_CANDIDATES["PRICECD"]))
        if pricecd:
            pricecd = pricecd.upper()
        if not part_id or pricecd not in ("P", "S"):
            continue

        s12    = to_number(pick(row, *KEYMAP_CANDIDATES["S12"]))
        s100   = to_number(pick(row, *KEYMAP_CANDIDATES["S100"]))
        s500   = to_number(pick(row, *KEYMAP_CANDIDATES["S500"]))
        s1000  = to_number(pick(row, *KEYMAP_CANDIDATES["S1000"]))
        s3000  = to_number(pick(row, *KEYMAP_CANDIDATES["S3000"]))
        s5000  = to_number(pick(row, *KEYMAP_CANDIDATES["S5000"]))
        s10000 = to_number(pick(row, *KEYMAP_CANDIDATES["S10000"]))
        currency = nz(pick(row, *KEYMAP_CANDIDATES["CURRENCY"]))
        customer = nz(pick(row, *KEYMAP_CANDIDATES["CUSTOMER"]))
        eff_date = to_date(pick(row, *KEYMAP_CANDIDATES["EFF_DATE"]))

        doc = {
            "part_id": part_id,
            "pricecd": pricecd,
            "S12":  s12,
            "S100": s100,
            "S500": s500,
            "1K":   s1000,
            "3K":   s3000,
            "5K":   s5000,
            "10K":  s10000,
            "currency": currency,
            "customer": customer,
            "eff_date": eff_date,
        }

        set_doc = {k: v for k, v in doc.items() if v is not None}

        existing = pricing.find_one({"part_id": part_id, "pricecd": pricecd}, {"eff_date": 1})
        should_update = False
        if not existing:
            should_update = True
        else:
            old_eff = existing.get("eff_date")
            if (old_eff is None and eff_date is not None) or (eff_date and old_eff and eff_date > old_eff):
                should_update = True

        if should_update:
            pricing.update_one(
                {"part_id": part_id, "pricecd": pricecd},
                {"$set": set_doc},
                upsert=True
            )
            upserts += 1

    cn.close()
    print(f"[done] Examined {examined} rows. Upserted {upserts} pricing docs.")

    non_null_any = pricing.count_documents({
        "$or": [
            {"S12": {"$ne": None}}, {"S100": {"$ne": None}}, {"S500": {"$ne": None}},
            {"1K": {"$ne": None}}, {"3K": {"$ne": None}}, {"5K": {"$ne": None}},
            {"10K": {"$ne": None}}
        ]
    })
    print(f"[diag] Docs with at least one non-null tier: {non_null_any}")

if __name__ == "__main__":
    run()
