"""Existing-app extension. No migration, stock mutation or service startup at import time."""
from collections import defaultdict
from datetime import date, timedelta
from functools import wraps
from io import StringIO
import csv
import hmac
import os
import re
import secrets
from uuid import uuid4
from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, session, url_for, Response
from pymongo.errors import PyMongoError
from .accounts import find_user, public_user, permissions, migrate_users, save_user
from .domain import Invalid, Conflict, PERMISSIONS, bounded_text, integer, stamp, business_date, stock_state
from .store import Store
from .forecasting import dates, policy, forecast, replenishment


def init_app(app,db):
    app.config.setdefault('INVENTORY_PLATFORM_ENABLED',os.getenv('INVENTORY_PLATFORM_ENABLED','1')=='1')
    app.config.setdefault('STOCK_WRITES_ENABLED',os.getenv('STOCK_WRITES_ENABLED','1' if os.getenv('VERCEL')=='1' else '0')=='1')
    app.config.setdefault('PLATFORM_CONFIG_WRITES',os.getenv('PLATFORM_CONFIG_WRITES','0')=='1')
    store=Store(db); app.extensions['inventory_platform']=store
    bp=Blueprint('platform',__name__,url_prefix='/platform')

    def csrf():
        if not session.get('platform_csrf'): session['platform_csrf']=secrets.token_urlsafe(32)
        return session['platform_csrf']

    def actor():
        user=getattr(g,'platform_user',None)
        if not user: abort(401)
        return public_user(user)

    def require(permission=None, config_write=False):
        def decorate(fn):
            @wraps(fn)
            def wrapped(*a,**kw):
                actor()
                if permission and permission not in g.platform_permissions: abort(403)
                if config_write and not app.config['PLATFORM_CONFIG_WRITES']:
                    return jsonify(error='Administrative writes are disabled until the deployment checklist is approved.'),409
                return fn(*a,**kw)
            return wrapped
        return decorate

    def data():
        value=request.get_json(silent=True)
        if not isinstance(value,dict): raise Invalid('Send a JSON object.')
        return value

    def json_doc(doc):
        if doc is None: return None
        out=dict(doc)
        if '_id' in out: out['_id']=str(out['_id'])
        out.pop('request',None)
        return out

    @app.before_request
    def identify():
        g.platform_user=None; g.platform_permissions=set()
        if not app.config['INVENTORY_PLATFORM_ENABLED'] or request.path.startswith('/static/'): return
        if 'username' in session:
            try: user=find_user(db,session.get('user_id'),session.get('username'))
            except PyMongoError:
                return render_template('platform/unavailable.html'),503
            if not user or user.get('active') is False:
                session.clear()
                if '/api/' in request.path: return jsonify(error='Account unavailable. Select an active user.'),401
                return redirect(url_for('login'))
            session.update(username=user['username'],role=user.get('role','worker'),user_id=str(user['_id']))
            g.platform_user=user; g.platform_permissions=permissions(db,user)
        # Legacy profile changes now use the same CSRF boundary as new features.
        if request.endpoint=='profile' and request.method=='POST':
            if not hmac.compare_digest(request.form.get('platform_csrf','').encode(),csrf().encode()): abort(400)
            if request.form.get('action')=='change_password':
                return jsonify(error='Password changes are disabled because this installation uses password-free account selection.'),400

    @app.context_processor
    def context():
        return {'platform_enabled':app.config['INVENTORY_PLATFORM_ENABLED'],
                'platform_permissions':getattr(g,'platform_permissions',set()),
                'platform_csrf':csrf() if app.config['INVENTORY_PLATFORM_ENABLED'] else '',
                'stock_writes_enabled':app.config['STOCK_WRITES_ENABLED']}

    @bp.before_request
    def secure():
        if not app.config['INVENTORY_PLATFORM_ENABLED']: abort(404)
        if not getattr(g,'platform_user',None):
            if '/api/' in request.path: return jsonify(error='Please select an active user.'),401
            return redirect(url_for('login'))
        if request.method not in ('GET','HEAD','OPTIONS'):
            if request.content_length and request.content_length>131072: abort(413)
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token','').encode(),csrf().encode()): abort(400)

    @bp.errorhandler(Invalid)
    def invalid(exc): return jsonify(error=str(exc)),400
    @bp.errorhandler(Conflict)
    def conflict(exc): return jsonify(error=str(exc)),409
    @bp.errorhandler(PyMongoError)
    def database_error(exc):
        app.logger.exception('Inventory platform database operation failed')
        return jsonify(error='Database unavailable. Retry with the same request ID; check history before resubmitting.'),503
    @bp.errorhandler(NotImplementedError)
    def transactions_required(exc): return jsonify(error='MongoDB replica-set transactions are required. No fallback writes are allowed.'),503

    def account_login():
        if request.method=='GET' and getattr(g,'platform_user',None):
            return redirect(url_for('admin_dashboard' if session['role']=='admin' else 'index'))
        error=None
        if request.method=='POST':
            if not hmac.compare_digest(request.form.get('platform_csrf','').encode(),csrf().encode()): abort(400)
            user=find_user(db,name=request.form.get('username','').strip())
            if user and user.get('active',True):
                next_url=request.args.get('next') or session.get('post_login_next','')
                session.clear(); session.update(username=user['username'],role=user.get('role','worker'),user_id=str(user['_id']))
                db.users.update_one({'_id':user['_id']},{'$set':{'last_login':stamp()}})
                db.audit_events.insert_one({'_id':str(uuid4()),'action':'login','user_id':str(user['_id']),
                    'username':user['username'],'timestamp':stamp()})
                if next_url.startswith('/') and not next_url.startswith('//') and '\\' not in next_url:
                    return redirect(next_url)
                return redirect(url_for('admin_dashboard' if session['role']=='admin' else 'index'))
            error='Select an active account. Contact an administrator if your account is missing.'
        try:
            accounts=[public_user(u) for u in db.users.find({'active':{'$ne':False}})]
            accounts.sort(key=lambda u:u['username'].casefold())
        except PyMongoError: return render_template('platform/unavailable.html'),503
        return render_template('platform/login.html',accounts=accounts,error=error,token=csrf())

    original_login=app.view_functions['login']; original_create=app.view_functions['create_user']
    app.view_functions['login']=lambda: account_login() if app.config['INVENTORY_PLATFORM_ENABLED'] else original_login()
    app.view_functions['create_user']=lambda: redirect(url_for('platform.page',section='users')) if app.config['INVENTORY_PLATFORM_ENABLED'] else original_create()

    @bp.get('/<section>')
    @require()
    def page(section):
        if section not in ('users','history','analytics','forecasting','sync','settings'): abort(404)
        required={'users':'users.manage','sync':'sync.manage','settings':'settings.manage'}.get(section)
        if required and required not in g.platform_permissions: abort(403)
        titles={'users':'Users','history':'Stock History','analytics':'Inventory Analytics','forecasting':'Stock Forecasting','sync':'Inventory Sync','settings':'Settings'}
        return render_template('platform/page.html',section=section,title=titles[section],today=business_date())

    @bp.get('/api/users')
    @require('users.manage')
    def user_list(): return jsonify([public_user(u) for u in db.users.find({}).sort('username',1)])

    @bp.route('/api/users',methods=['POST'])
    @bp.route('/api/users/<user_id>',methods=['PUT'])
    @require('users.manage',True)
    def user_save(user_id=None): return jsonify(save_user(store,data(),actor(),user_id))

    @bp.get('/api/users/<user_id>/activity')
    @require('users.manage')
    def user_activity(user_id):
        events=[json_doc(v) for v in db.audit_events.find({'$or':[{'user_id':user_id},{'target_user_id':user_id}]}).sort('timestamp',-1).limit(100)]
        events += [{**json_doc(v),'action':'stock_'+v.get('movement_type','movement')} for v in db.stock_movements.find({'user_id':user_id}).sort('timestamp',-1).limit(100)]
        return jsonify(sorted(events,key=lambda v:v.get('timestamp',''),reverse=True)[:100])

    @bp.get('/api/stock/<path:uid>')
    @require()
    def stock_get(uid):
        p=db.products.find_one({'uid':uid})
        if not p: abort(404)
        error=None
        try: state=stock_state(p)
        except (Invalid,Conflict) as exc: state=None; error=str(exc)
        recent=list(db.stock_movements.find({'uid':uid}).sort('timestamp',-1).limit(10))
        return jsonify(uid=uid,name=p.get('name',''),mssid=p.get('readable_id',''),state=state,error=error,
            revision=p.get('stock_revision',0),last_updated=p.get('last_stock_update'),last_updated_by=p.get('last_stock_user'),
            sync_status=p.get('sync_status','uninitialised'),writable=app.config['STOCK_WRITES_ENABLED'] and 'stock.edit' in g.platform_permissions and bool(p.get('access_sync_eligible')) and bool(p.get('access_confirmed')),
            recent=[json_doc(m) for m in recent])

    @bp.post('/api/stock/<path:uid>')
    @require('stock.edit')
    def stock_update(uid):
        if not app.config['STOCK_WRITES_ENABLED']: return jsonify(error='Stock editing is disabled during staging. Live quantities have not changed.'),409
        body=data(); bounded_text(body.get('reference',''),'Reference',120)
        return jsonify(saved_to_cloud=True,sync_status='pending',movement=json_doc(store.change(uid,body,actor())))

    def history_query():
        q={}
        for param,field in (('uid','uid'),('user','user_id'),('type','movement_type'),('location','changes.location')):
            value=request.args.get(param,'').strip()
            if value: q[field]=value[:120]
        start=request.args.get('start',''); end=request.args.get('end','')
        if start or end:
            list(dates(start or '2020-01-01',end or business_date()))
            q['business_date']={'$gte':start or '2020-01-01','$lte':end or business_date()}
        ref=request.args.get('reference','').strip()
        if ref: q['reference']=re.compile(re.escape(ref[:120]),re.I)
        return q

    def movement_status(rows):
        states={d['_id']:d['status'] for d in db.sync_events.find({'_id':{'$in':[r['_id'] for r in rows]}})}
        return [{**json_doc(r),'sync_status':states.get(r['_id'],r.get('sync_status'))} for r in rows]

    @bp.get('/api/history')
    @require()
    def history():
        q=history_query(); page=integer(request.args.get('page',1),'Page',1,100000)
        rows=list(db.stock_movements.find(q).sort([('timestamp',-1),('_id',-1)]).skip((page-1)*50).limit(50))
        return jsonify(rows=movement_status(rows),total=db.stock_movements.count_documents(q),page=page)

    @bp.get('/api/history.csv')
    @require()
    def history_csv():
        def safe(value):
            value=str(value)
            return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
        def stream():
            output=StringIO(); writer=csv.writer(output)
            header=['Event ID','Product UID','Product name','Business date','UTC timestamp','User ID','Username','Source','Type','Location','Previous','Change','New','Reference','Note','Sync status']
            writer.writerow(header); yield '\ufeff'+output.getvalue(); output.seek(0);output.truncate(0)
            cursor=db.stock_movements.find(history_query()).sort('timestamp',-1)
            for m in cursor:
                state=db.sync_events.find_one({'_id':m['_id']},{'status':1}) or m
                for c in m.get('changes',[]):
                    values=[m['_id'],m['uid'],m.get('product_name',''),m['business_date'],m['timestamp'],m['user_id'],m['username'],m['source'],m['movement_type'],c['location'],c['previous'],c['change'],c['new'],m.get('reference',''),m.get('note',''),state.get('status',m.get('sync_status',''))]
                    writer.writerow([safe(v) for v in values]); yield output.getvalue(); output.seek(0);output.truncate(0)
        from flask import stream_with_context
        return Response(stream_with_context(stream()),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=stock-history.csv'})

    @bp.get('/api/analytics')
    @require()
    def analytics():
        end=request.args.get('end') or business_date(); start=request.args.get('start') or (date.fromisoformat(end)-timedelta(days=29)).isoformat()
        day_list=list(dates(start,end)); daily={d:{'date':d,'issued':0,'received':0} for d in day_list}
        stats=defaultdict(lambda:{'issued':0,'received':0,'frequency':0,'last_movement':None})
        categories=defaultdict(int); weekly=defaultdict(int); monthly=defaultdict(int)
        for m in db.stock_movements.find({'business_date':{'$gte':start,'$lte':end}}):
            s=stats[m['uid']]; s['frequency']+=1; s['last_movement']=max(s['last_movement'] or '',m['timestamp'])
            if m['movement_type']=='issued':
                amount=m.get('demand_units',0); s['issued']+=amount; daily[m['business_date']]['issued']+=amount
                categories[m.get('category') or 'Uncategorized']+=amount
                d=date.fromisoformat(m['business_date']); weekly[(d-timedelta(days=d.weekday())).isoformat()]+=amount
                monthly[m['business_date'][:7]]+=amount
            if m['movement_type']=='received':
                office=next((c for c in m['changes'] if c.get('slot')=='Office'),None)
                amount=max(0,office['change']) if office else sum(max(0,c['change']) for c in m['changes'])
                s['received']+=amount;daily[m['business_date']]['received']+=amount
        coverage={d['_id']:d.get('complete',False) for d in db.business_days.find({'_id':{'$gte':start,'$lte':end}})}
        closed=sum(coverage.get(d,False) for d in day_list)
        rows=[]
        for p in db.products.find({}):
            uid=p.get('uid',''); s=stats[uid]; settings=db.inventory_settings.find_one({'_id':'product:'+uid}) or {}
            avg=s['issued']/len(day_list) if closed==len(day_list) else None
            stock=p.get('stock',0) or 0; threshold=settings.get('reorder_threshold',0)
            rows.append({'uid':uid,'name':p.get('name',''),'stock':stock,**s,'average_daily_demand':avg,
                         'coverage_days':stock/avg if avg else None,'reorder_threshold':threshold,
                         'suggested_quantity':max(0,threshold-stock) if threshold else None})
        rows.sort(key=lambda r:r['issued'],reverse=True)
        return jsonify(cards={'Total SKUs':len(rows),'Products with movements':sum(r['frequency']>0 for r in rows),
            'Units issued':sum(r['issued'] for r in rows),'Units received':sum(r['received'] for r in rows),
            'Low stock':sum(0<r['stock']<=r['reorder_threshold'] for r in rows),'Out of stock':sum(r['stock']<=0 for r in rows),
            'Fast-moving candidates':sum(r['issued']>0 for r in rows[:10]),'No issues in selected period':sum(r['issued']==0 for r in rows),
            'Below configured reorder threshold':sum(r['suggested_quantity'] is not None and r['suggested_quantity']>0 for r in rows)},
            rows=rows,daily=list(daily.values()),weekly=dict(weekly),monthly=dict(monthly),categories=dict(categories),
            coverage={'closed_days':closed,'days':len(day_list)},note='Issue frequency and stock coverage are proxies, not financial inventory turnover. Averages require complete-day attestation.')

    @bp.get('/api/forecasting')
    @require()
    def forecasting():
        query=request.args.get('q','').strip(); match={'$or':[{'uid':re.compile(re.escape(query),re.I)},{'name':re.compile(re.escape(query),re.I)}]} if query else {}
        rows=[]
        for p in db.products.find(match).limit(100):
            uid=p.get('uid',''); saved=db.forecasts.find_one({'uid':uid},sort=[('generated_at',-1)])
            rows.append({'uid':uid,'name':p.get('name',''),'stock':p.get('stock'),
                         'analysis':saved['analysis'] if saved else forecast([]),
                         'replenishment':saved['replenishment'] if saved else {'status':'Insufficient demand history; no recommendation.'},
                         'generated_at':saved.get('generated_at') if saved else None,'data_cutoff':saved.get('data_cutoff') if saved else None,
                         'history':[{'date':v['date'],'demand':v.get('demand'),'usable':v.get('usable')} for v in db.daily_demand.find({'uid':uid}).sort('date',-1).limit(60)][::-1]})
        return jsonify(rows=rows,job=json_doc(db.inventory_sync_control.find_one({'_id':'forecast_job'})),note='Showing up to 100 products. Forecasts are advisory snapshots, not purchasing instructions.')

    @bp.get('/api/sync')
    @require('sync.manage')
    def sync_info():
        return jsonify(heartbeat=json_doc(db.inventory_sync_control.find_one({'_id':'worker'})),mongo='Connected',
            pending=db.sync_events.count_documents({'status':'pending'}),failed=db.sync_events.count_documents({'status':'failed'}),
            conflicts=db.sync_events.count_documents({'status':'conflict'}),
            events=[json_doc(e) for e in db.sync_events.find({'status':{'$ne':'synced'}}).sort('created_at',1).limit(100)])

    @bp.post('/api/sync/wake')
    @require('sync.manage',True)
    def sync_wake():
        db.inventory_sync_control.update_one({'_id':'wake'},{'$set':{'requested_at':stamp(),'by':actor()['id']}},upsert=True)
        return jsonify(message='Sync requested. The Windows worker must be online; queued changes remain durable while it is off.')

    @bp.post('/api/sync/<event_id>/resolve')
    @require('sync.manage',True)
    def sync_resolve(event_id):
        body=data(); store.resolve(event_id,body.get('decision'),body.get('expected'),actor(),bounded_text(body.get('note',''),'Reason',1000,True))
        return jsonify(message='Conflict queued for a conditional retry against the reviewed Access values.')

    @bp.get('/api/settings')
    @require('settings.manage')
    def settings_get():
        return jsonify(permissions=(db.inventory_settings.find_one({'_id':'permissions'}) or {}).get('roles',{'admin':list(PERMISSIONS),'worker':[]}),
            stock_writes=app.config['STOCK_WRITES_ENABLED'],config_writes=app.config['PLATFORM_CONFIG_WRITES'],
            policies=[json_doc(v) for v in db.inventory_settings.find({'_id':re.compile('^product:')})],
            calendar=[json_doc(v) for v in db.calendar_events.find({}).sort('start',-1).limit(100)])

    @bp.post('/api/settings/<kind>')
    @require('settings.manage',True)
    def settings_save(kind):
        body=data(); who=actor()
        if kind=='product':
            uid=bounded_text(body.get('uid',''),'Product UID',120,True)
            if not db.products.find_one({'uid':uid}): raise Invalid('Unknown product UID.')
            collection=db.inventory_settings; key='product:'+uid; values=policy(body)
        elif kind=='permissions':
            roles=body.get('roles')
            if not isinstance(roles,dict) or set(roles)!={'admin','worker'}: raise Invalid('Provide Admin and Worker permissions.')
            if any(not isinstance(v,list) or any(p not in PERMISSIONS for p in v) for v in roles.values()): raise Invalid('Invalid permissions.')
            if not {'users.manage','settings.manage'}.issubset(roles['admin']): raise Invalid('Keep Admin user/settings management permissions.')
            collection=db.inventory_settings;key='permissions';values={'roles':roles}
        elif kind=='calendar':
            start=body.get('start','');end=body.get('end','');list(dates(start,end))
            collection=db.calendar_events;key=str(uuid4());values={'start':start,'end':end,'name':bounded_text(body.get('name',''),'Event name',160,True),'note':bounded_text(body.get('note',''),'Note',1000),'supplier':bounded_text(body.get('supplier',''),'Supplier',160)}
        elif kind=='coverage':
            day=body.get('date','');list(dates(day,day))
            if day>=business_date(): raise Invalid('Only completed Malaysia business days may be closed.')
            if body.get('complete') is not True: raise Invalid('Confirm that all demand and stockouts were recorded for the day.')
            collection=db.business_days;key=day;values={'complete':True,'by':who['id'],'at':stamp(),'note':bounded_text(body.get('note',''),'Evidence/note',1000,True)}
        elif kind=='override':
            collection=db.forecast_overrides;key=str(uuid4());values={'uid':bounded_text(body.get('uid',''),'UID',120,True),'quantity':integer(body.get('quantity'),'Quantity'),'reason':bounded_text(body.get('reason',''),'Reason',1000,True),'user_id':who['id'],'at':stamp()}
        else: abort(404)
        def write(kw):
            collection.update_one({'_id':key},{'$set':values},upsert=True,**kw)
            db.audit_events.insert_one({'_id':str(uuid4()),'action':'settings_'+kind,'timestamp':stamp(),'user_id':who['id'],'username':who['username'],'values':values},**kw)
        store.transact(write)
        return jsonify(message='Saved.')

    @app.cli.command('inventory-init')
    def initialise():
        """Explicit migration; use tools/platform_admin.py for a dry-run plan first."""
        print(migrate_users(db,True));store.indexes()
        db.platform_meta.update_one({'_id':'collection_started'},{'$setOnInsert':{'date':business_date(),'at':stamp()}},upsert=True)

    app.register_blueprint(bp)
