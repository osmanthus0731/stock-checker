# tools/import_products_from_access.py
import os, re
from dotenv import load_dotenv
load_dotenv()
from pymongo import MongoClient, ASCENDING
try:
    import pyodbc as odbc
except Exception:
    import pypyodbc as odbc

ACCESS_TABLE = os.getenv("ACCESS_TABLE", "ITMMST")
ACCESS_DB_PATH = os.getenv("ACCESS_DB_PATH")
ACCESS_CONN_STR = os.getenv("ACCESS_CONN_STR")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "inventory")

VOL_RE = re.compile(r"(\d{1,5})\s*ml\b", re.IGNORECASE)

def vol_ml(text):
    if not text: return None
    m = VOL_RE.search(text)
    if not m: return None
    try: return int(m.group(1))
    except: return None

def to_int(v, default=None):
    try:
        if v in (None, ""): return default
        return int(v)
    except:
        return default

def access_connect():
    if ACCESS_CONN_STR:
        return odbc.connect(ACCESS_CONN_STR)
    if not ACCESS_DB_PATH:
        raise RuntimeError("Set ACCESS_DB_PATH or ACCESS_CONN_STR in .env")
    return odbc.connect(
        r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
        rf"DBQ={ACCESS_DB_PATH};READONLY=TRUE;"
    )

def run():
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    products = db["products"]
    products.create_index([("uid", ASCENDING)], unique=True)

    cn = access_connect()
    cur = cn.cursor()
    cur.execute(f"SELECT * FROM [{ACCESS_TABLE}]")
    cols = [c[0] for c in cur.description]

    def g(rec, *keys, default=""):
        return next((rec.get(k) for k in keys if k in rec and rec.get(k) is not None), default)

    upserts = 0
    rows = cur.fetchall()
    for row in rows:
        rec = dict(zip(cols, row))

        uid   = (g(rec, "Part_id","part_id","Part-ID","PartID") or "").strip()
        name  = (g(rec, "Desc","desc","Description") or "").strip()
        mssid = (g(rec, "Mssid","mssid","MSSID") or "").strip()

        # 🔧 CATEGORY: add/replace column names here to match your Access file
        cat   = (g(rec, "Cat","cat","Category","ProdGroup","Group","Type") or "").strip() or "Uncategorized"

        pict  = to_int(g(rec, "Pict","pict","PICT"), default=None)

        loc_a = (g(rec, "Loc_film_box","loc_film_box","LocA") or "").strip()
        qty_a = to_int(g(rec, "StockA","stocka","QtyA"), default=0)
        loc_b = (g(rec, "Loc_wh","loc_wh","LocB") or "").strip()
        qty_b = to_int(g(rec, "StockB","stockb","QtyB"), default=0)

        locs = []
        if loc_a: locs.append({"area": loc_a, "quantity": qty_a})
        if loc_b: locs.append({"area": loc_b, "quantity": qty_b})

        total = to_int(g(rec,"Stock_Office","Stock_office","stock_office","Total"), default=qty_a + qty_b)
        vml   = vol_ml(name) or vol_ml(mssid)

        if not uid:
            continue

        doc = {
            "uid": uid,
            "name": name,
            "readable_id": mssid,
            "category": cat,
            "pict": pict,
            "locations": locs,
            "stock": total,
            "volume_ml": vml
        }
        products.update_one({"uid": uid}, {"$set": doc}, upsert=True)
        upserts += 1

    cn.close()
    print(f"Upserted {upserts} product docs into Mongo.")

if __name__ == "__main__":
    run()
