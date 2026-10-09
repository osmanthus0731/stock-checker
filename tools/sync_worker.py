"""Run through the boot task installer. Safe default: SYNC_ENABLED=0, write-back disabled."""
import argparse
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import dotenv_values
from pymongo import MongoClient
import certifi
from inventory_platform.store import Store
from inventory_platform.sync import AccessAdapter, DaoAccessAdapter, Journal, Worker, staging_allowed
from inventory_platform.forecasting import refresh
from inventory_platform.domain import business_date, stamp


def run(config_path,once=False):
    config={**dotenv_values(ROOT/'.env'),**dotenv_values(config_path),**os.environ}
    log_dir=ROOT/'sync-logs';log_dir.mkdir(exist_ok=True)
    handler=RotatingFileHandler(log_dir/'worker.log',maxBytes=2_000_000,backupCount=5,encoding='utf-8')
    logging.basicConfig(level=logging.INFO,handlers=[handler])
    interval=max(1,min(60,int(config.get('SYNC_INTERVAL_SECONDS','3'))))
    scan_interval=max(interval,min(3600,int(config.get('ACCESS_SCAN_INTERVAL_SECONDS','60'))))
    # OS lock has no expiry: another local process cannot outlive a lease and overlap Access writes.
    import msvcrt
    lock=open(log_dir/'worker.lock','a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try: msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError: raise SystemExit('Another sync worker is already running on this computer.')
    if config.get('SYNC_ENABLED','0')!='1':
        logging.info(json.dumps({'event':'disabled','at':stamp()}));return
    uri=config.get('MONGO_URI') or config.get('MONGO_URL')
    client=MongoClient(uri,tlsCAFile=certifi.where(),serverSelectionTimeoutMS=10000,connectTimeoutMS=10000)
    db=client[config.get('MONGO_DB','inventory')]
    adapter=DaoAccessAdapter if config.get('ACCESS_BACKEND','odbc').lower()=='dao' else AccessAdapter
    access=adapter(config['ACCESS_DB_PATH'],staging_allowed(config,config['ACCESS_DB_PATH']))
    worker=Worker(Store(db),access,Journal(log_dir/'receipts.sqlite'))
    job=None
    try:
        last_scan=0
        while True:
            try:
                clock=time.monotonic();do_scan=clock-last_scan>=scan_interval
                worker.cycle(do_scan)
                if do_scan:last_scan=clock
                state=db.inventory_sync_control.find_one({'_id':'forecast_job'}) or {}
                if config.get('FORECAST_REFRESH_ENABLED','0')=='1' and (not job or not job.is_alive()) and state.get('last_date')!=business_date():
                    def job_run():
                        try: refresh(db,config.get('FORECAST_ADVANCED_ENABLED','0')=='1')
                        except Exception as exc: logging.error(json.dumps({'event':'forecast_failed','type':type(exc).__name__,'at':stamp()}))
                    job=threading.Thread(target=job_run,daemon=True);job.start()
                logging.info(json.dumps({'event':'cycle_ok','at':stamp()}))
            except Exception as exc:
                logging.error(json.dumps({'event':'cycle_failed','type':type(exc).__name__,'at':stamp()}))
                try: db.inventory_sync_control.update_one({'_id':'worker'},{'$set':{'heartbeat':stamp(),'access':'Unavailable or conflict','error_type':type(exc).__name__}},upsert=True)
                except Exception: pass
            if once: break
            time.sleep(interval)
    finally:
        client.close();lock.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true')
    args=parser.parse_args();run(str(Path(args.config).resolve()),args.once)
