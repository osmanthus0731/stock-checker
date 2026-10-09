"""Read-only preflight. Never import app.py (its legacy startup can write indexes)."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from datetime import datetime, timezone
from dotenv import dotenv_values
from pymongo import MongoClient
import certifi


def inspect(backup=False):
    env = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
    report = {}
    path = Path(env.get('ACCESS_DB_PATH') or '')
    if path.is_file():
        if backup:
            target = Path('backups') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            target.mkdir(parents=True, exist_ok=True)
            before = path.stat()
            shutil.copy2(path, target / path.name)
            after = path.stat()
            report['backup'] = str(target / path.name)
            report['backup_source_stable'] = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
            report['backup_sha256'] = hashlib.sha256((target/path.name).read_bytes()).hexdigest()
            report['backup_note'] = 'File copy only; close Access and make a quiescent backup before production enablement.'
        try:
            import pyodbc
            read_path = target / path.name if backup else path
            cn = pyodbc.connect('DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ='+str(read_path.resolve())+';READONLY=TRUE;', autocommit=True)
            cur = cn.cursor()
            columns = [r.column_name for r in cur.columns(table='ITMMST')]
            report['access_columns'] = columns
            cur.execute('SELECT COUNT(*) FROM [ITMMST]')
            report['access_count'] = cur.fetchone()[0]
            cur.execute('SELECT [StockA], [StockB], [Stock_office] FROM [ITMMST]')
            values = cur.fetchall()
            report['access_total_mismatches'] = sum(1 for a,b,t in values if a is None or b is None or t is None or a+b != t)
            cn.close()
        except Exception as exc:
            report['access_read_error'] = type(exc).__name__
    try:
        uri = env.get('MONGO_URI') or env.get('MONGO_URL')
        kw = {'serverSelectionTimeoutMS':8000,'connectTimeoutMS':5000}
        if uri.startswith('mongodb+srv') or 'mongodb.net' in uri:
            kw.update(tls=True,tlsCAFile=certifi.where())
        with MongoClient(uri, **kw) as client:
            db = client[env.get('MONGO_DB','inventory')]
            report['mongo_counts'] = {n:db[n].count_documents({}) for n in ('users','products','pricing','purchase_orders')}
            report['product_fields'] = sorted({k for p in db.products.find().limit(20) for k in p})
            users = list(db.users.find({}, {'username':1,'role':1}))
            keys = [str(u.get('username','')).casefold() for u in users]
            report['username_collisions'] = len(keys)-len(set(keys))
            report['initial_users_present'] = [u for u in ('sg','szee foon','tommy','HK','MsT','Admin') if u.casefold() in keys]
    except Exception as exc:
        report['mongo_read_error'] = type(exc).__name__
    print(json.dumps(report,indent=2,default=str))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--backup', action='store_true')
    inspect(parser.parse_args().backup)
