"""Destructive only to an explicitly named staging copy; emits a production enablement receipt."""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inventory_platform.sync import AccessAdapter,DaoAccessAdapter
from inventory_platform.domain import stock_state

parser=argparse.ArgumentParser();parser.add_argument('--staging-copy',required=True);parser.add_argument('--production-path',required=True);parser.add_argument('--uid',required=True);parser.add_argument('--output',required=True);parser.add_argument('--backend',choices=('odbc','dao'),default='odbc')
args=parser.parse_args();staging=Path(args.staging_copy).resolve();production=Path(args.production_path).resolve();output=Path(args.output).resolve()
if staging==production:raise SystemExit('Refusing to test against the production Access path.')
if not staging.is_file():raise SystemExit('Staging copy not found.')
adapter=(DaoAccessAdapter if args.backend=='dao' else AccessAdapter)(staging,True);original=stock_state(adapter.read(args.uid));test=deepcopy(original)
if original['a']>0:test['a']-=1;test['b']+=1
elif original['b']>0:test['a']+=1;test['b']-=1
else:raise SystemExit('Choose a staging UID with stock in A or B for a reversible transfer test.')
adapter.compare_and_set(args.uid,original,test);assert stock_state(adapter.read(args.uid))==test
adapter.compare_and_set(args.uid,test,original);assert stock_state(adapter.read(args.uid))==original
try:
    adapter.compare_and_set(args.uid,test,original)
    raise AssertionError('A stale conditional update unexpectedly succeeded.')
except Exception as exc:
    if 'changed before' not in str(exc): raise
assert stock_state(adapter.read(args.uid))==original
receipt={'generated_at':datetime.now(timezone.utc).isoformat(),'staging_copy':str(staging),'production_path':str(production),
         'uid':args.uid,'access_roundtrip':True,'crash_recovery':False,'conflicts':True,'offline_reconnect':False,'restart_recovery':False,
         'note':'Roundtrip and stale-write conflict checks passed; original staging values were restored. Remaining worker recovery checks are still required.'}
output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt,indent=2))
