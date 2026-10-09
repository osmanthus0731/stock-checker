"""Explicit platform migration/index command. Default is a read-only plan."""
import argparse
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from pymongo import MongoClient
import certifi
from inventory_platform.accounts import migrate_users
from inventory_platform.store import Store
from inventory_platform.domain import business_date,stamp

load_dotenv()
parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true',help='Write reviewed user migration and indexes')
args=parser.parse_args();uri=os.getenv('MONGO_URI') or os.getenv('MONGO_URL');kw={'serverSelectionTimeoutMS':10000}
if uri.startswith('mongodb+srv') or 'mongodb.net' in uri:kw.update(tls=True,tlsCAFile=certifi.where())
with MongoClient(uri,**kw) as client:
    db=client[os.getenv('MONGO_DB','inventory')]
    plan=migrate_users(db,args.apply);print(plan)
    if args.apply:
        Store(db).indexes()
        db.platform_meta.update_one({'_id':'collection_started'},{'$setOnInsert':{'date':business_date(),'at':stamp()}},upsert=True)
        print('Platform indexes and data-collection start marker created. Stock/configuration write gates remain controlled by environment flags.')
