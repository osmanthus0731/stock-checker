"""Atomic cloud stock + append-only ledger + durable outbox. Replica-set transactions required."""
from copy import deepcopy
from uuid import uuid4
from pymongo import ReturnDocument
from .domain import adjustment, stock_state, cloud_fields, stamp, business_date, Conflict, Invalid, integer


class Store:
    def __init__(self, db, transact=None):
        self.db=db
        self.transact=transact or self._transaction

    def _transaction(self, fn):
        with self.db.client.start_session() as session:
            return session.with_transaction(lambda s:fn({'session':s}))

    def indexes(self):
        self.db.stock_movements.create_index([('uid',1),('timestamp',-1)])
        self.db.stock_movements.create_index([('business_date',1),('movement_type',1)])
        self.db.sync_events.create_index([('status',1),('created_at',1)])
        self.db.sync_events.create_index([('uid',1),('version',1)],unique=True)
        self.db.daily_demand.create_index([('uid',1),('date',1)],unique=True)
        self.db.forecasts.create_index([('uid',1),('generated_at',-1)])

    def change(self, uid, data, actor):
        event_id=data.get('event_id')
        if not isinstance(event_id,str) or len(event_id)!=36:
            raise Invalid('A unique request ID is required.')
        from uuid import UUID
        try: UUID(event_id)
        except ValueError: raise Invalid('Invalid request ID.') from None
        expected=integer(data.get('revision'),'Revision')
        def work(kw):
            existing=self.db.stock_movements.find_one({'_id':event_id},**kw)
            if existing:
                if existing['uid']!=uid or existing['user_id']!=actor['id'] or existing.get('request')!=data:
                    raise Conflict('This request ID was already used for a different change.')
                return existing
            product=self.db.products.find_one({'uid':uid},**kw)
            if not product: raise Invalid('Product not found.')
            if product.get('stock_revision',0)!=expected: raise Conflict('Stock changed. Reload and review the latest values.')
            if not product.get('access_sync_eligible'): raise Conflict('This product has not passed Access stock validation.')
            if not product.get('access_confirmed'): raise Conflict('Access baseline is not confirmed.')
            if product.get('sync_status') in ('conflict','failed'): raise Conflict('Resolve the sync issue before changing this product.')
            before,after,changes,reason,note=adjustment(product,data)
            ts=stamp(); version=expected+1
            movement={'_id':event_id,'event_id':event_id,'uid':uid,'product_name':product.get('name',''),
                      'category':product.get('category',''),'supplier':product.get('supplier',''),
                      'timestamp':ts,'business_date':business_date(),'user_id':actor['id'],'username':actor['username'],
                      'source':'website','movement_type':reason,'reason':reason,'changes':changes,
                      'before':before,'after':after,'unit':'units','reference':str(data.get('reference',''))[:120],
                      'note':note,'sync_status':'pending','record_version':version,
                      'demand_units':max(0,before['total']-after['total']) if reason=='issued' else 0,'request':deepcopy(data)}
            result=self.db.products.update_one({'_id':product['_id'],'stock_revision':expected},
                {'$set':{**cloud_fields(after),'stock_revision':version,'last_stock_update':ts,
                         'last_stock_user':actor['username'],'sync_status':'pending','last_stock_event':event_id}},**kw)
            if not result.matched_count: raise Conflict('Stock changed. Reload before saving.')
            self.db.stock_movements.insert_one(movement,**kw)
            self.db.sync_events.insert_one({'_id':event_id,'uid':uid,'version':version,'before':before,'after':after,
                'status':'pending','created_at':ts,'attempts':0},**kw)
            return movement
        return self.transact(work)

    def acknowledge(self, event, method='verified_write'):
        def work(kw):
            current=self.db.sync_events.find_one({'_id':event['_id']},**kw)
            if current['status']=='synced': return
            if current['status'] not in ('pending','failed'): raise Conflict('Event is no longer available for acknowledgement.')
            self.db.sync_events.update_one({'_id':event['_id']},{'$set':{'status':'synced','synced_at':stamp(),'ack_method':method}},**kw)
            product=self.db.products.find_one({'uid':event['uid']},**kw)
            values={'access_confirmed':event['after'],'last_access_sync':stamp()}
            if product.get('stock_revision')==event['version']: values['sync_status']='synced'
            self.db.products.update_one({'_id':product['_id']},{'$set':values},**kw)
        self.transact(work)

    def observe(self, mapped):
        """Access observations are corrections, never inferred sales. No automatic first baseline overwrite."""
        uid=mapped['uid']; actual=stock_state(mapped)
        def work(kw):
            product=self.db.products.find_one({'uid':uid},**kw)
            if not product:
                # New products are imported only by explicitly enabled worker, never during preflight.
                self.db.products.insert_one({**mapped,'stock_revision':0,'access_confirmed':actual,'sync_status':'synced','access_sync_eligible':True,
                                             'last_access_sync':stamp()},**kw)
                return 'baseline'
            if self.db.sync_events.find_one({'uid':uid,'status':{'$ne':'synced'}},**kw): return 'pending'
            confirmed=product.get('access_confirmed')
            if confirmed is None:
                # Existing totals must agree; incomplete legacy slot mapping can be filled from Access.
                if product.get('stock')!=actual['total']: raise Conflict('Initial cloud/Access totals differ; baseline approval required.')
                self.db.products.update_one({'_id':product['_id']},{'$set':{**mapped,'stock_revision':0,'access_confirmed':actual,'access_sync_eligible':True,
                    'sync_status':'synced','last_access_sync':stamp()}},**kw)
                return 'baseline'
            if confirmed==actual:
                if not product.get('access_sync_eligible'):
                    self.db.products.update_one({'_id':product['_id']},{'$set':{'access_sync_eligible':True}},**kw)
                return 'unchanged'
            if stock_state(product)!=confirmed: raise Conflict('Cloud stock differs from confirmed Access baseline.')
            version=product.get('stock_revision',0)+1
            # Deterministic ID for retry after unknown transaction outcome.
            event_id=f'access:{uid}:{version}'
            changes=[{'slot':s,'location':actual['location_'+s.lower()],'previous':confirmed[k],
                      'change':actual[k]-confirmed[k],'new':actual[k]} for s,k in (('A','a'),('B','b')) if actual[k]!=confirmed[k]]
            if actual['total']!=confirmed['total']:
                changes.append({'slot':'Office','location':'Office total','previous':confirmed['total'],
                                'change':actual['total']-confirmed['total'],'new':actual['total']})
            self.db.stock_movements.insert_one({'_id':event_id,'event_id':event_id,'uid':uid,'product_name':mapped['name'],
                'category':mapped['category'],'supplier':mapped['supplier'],'timestamp':stamp(),'business_date':business_date(),
                'user_id':'access-service','username':'Microsoft Access','source':'access','movement_type':'observed_correction',
                'reason':'Observed snapshot change; business reason unknown','changes':changes,'before':confirmed,'after':actual,
                'unit':'units','reference':'','note':'','sync_status':'synced','record_version':version,'demand_units':0},**kw)
            result=self.db.products.update_one({'_id':product['_id'],'stock_revision':version-1},{'$set':{**mapped,'access_sync_eligible':True,
                'stock_revision':version,'access_confirmed':actual,'sync_status':'synced','last_access_sync':stamp(),
                'last_stock_update':stamp(),'last_stock_user':'Microsoft Access'}},**kw)
            if not result.matched_count: raise Conflict('Concurrent website edit; retry Access observation.')
            return 'observed'
        return self.transact(work)

    def flag(self,event,status,detail,actual=None):
        def work(kw):
            self.db.sync_events.update_one({'_id':event['_id'],'status':{'$ne':'synced'}},{'$set':{'status':status,
                'error':detail,'observed_access':actual,'updated_at':stamp()},'$inc':{'attempts':1}},**kw)
            self.db.products.update_one({'uid':event['uid']},{'$set':{'sync_status':status}},**kw)
        self.transact(work)

    def resolve(self,event_id,decision,expected,actor,note):
        """Explicit keep-cloud approval rebases a conditional write; never an unconditional overwrite."""
        if decision!='keep_cloud': raise Invalid('Only explicit keep-cloud retry is supported; export/reconcile other cases manually.')
        if not note.strip(): raise Invalid('A resolution reason is required.')
        def work(kw):
            event=self.db.sync_events.find_one({'_id':event_id,'status':'conflict'},**kw)
            if not event or event.get('observed_access')!=expected: raise Conflict('Conflict changed. Refresh before resolving.')
            if not expected: raise Invalid('A fresh Access snapshot is required.')
            self.db.sync_events.update_one({'_id':event_id},{'$set':{'before':expected,'status':'pending','error':''},
                '$push':{'resolutions':{'by':actor['id'],'at':stamp(),'reason':note,'decision':decision,'expected':expected}}},**kw)
            self.db.products.update_one({'uid':event['uid']},{'$set':{'sync_status':'pending'}},**kw)
            self.db.audit_events.insert_one({'_id':str(uuid4()),'action':'sync_resolution','user_id':actor['id'],
                'username':actor['username'],'event_id':event_id,'timestamp':stamp(),'reason':note},**kw)
        self.transact(work)
