"""Transparent demand models. Missing/censored days are never silently filled with zero."""
from collections import defaultdict
from datetime import date, timedelta
from math import ceil, sqrt
from statistics import mean, pstdev, NormalDist
from uuid import uuid4
from .domain import integer, Invalid, stamp, business_date

MODEL_VERSION='mizitco-baselines-v1'


def dates(start,end):
    current=date.fromisoformat(start); stop=date.fromisoformat(end)
    if stop<current or (stop-current).days>3660: raise Invalid('Select a date range of up to ten years.')
    while current<=stop:
        yield current.isoformat(); current+=timedelta(days=1)


def aggregate(movements, coverage, start, end, uid):
    days={d:{'date':d,'uid':uid,'demand':0,'events':0,'complete':coverage.get(d,False),'censored':False,'unexplained':False} for d in dates(start,end)}
    for movement in movements:
        row=days.get(movement.get('business_date'))
        if not row or movement.get('uid')!=uid: continue
        if movement.get('movement_type')=='issued':
            row['demand']+=max(0,int(movement.get('demand_units',0))); row['events']+=1
        if movement.get('after',{}).get('total',1)<=0 or movement.get('before',{}).get('total',1)<=0: row['censored']=True
        if movement.get('movement_type')=='observed_correction': row['unexplained']=True
    for row in days.values():
        row['usable']=row['complete'] and not row['censored'] and not row['unexplained']
        if not row['complete']: row['demand']=None
    return list(days.values())


def predict(values,model):
    if not values: return 0.0
    if model=='moving_average': return mean(values[-14:])
    if model=='exponential_smoothing':
        level=values[0]
        for y in values[1:]: level=.3*y+.7*level
        return max(0,level)
    if model=='croston_sba':
        level=None; interval=1.; elapsed=1
        for value in values:
            if value>0:
                if level is None: level=float(value); interval=float(elapsed)
                else: level=.2*value+.8*level; interval=.2*elapsed+.8*interval
                elapsed=1
            else: elapsed+=1
        return .9*level/interval if level is not None else 0.0
    if model=='trend':
        ys=values[-28:]; n=len(ys); center=(n-1)/2; avg=mean(ys)
        slope=sum((i-center)*(y-avg) for i,y in enumerate(ys))/max(1,sum((i-center)**2 for i in range(n)))
        return max(0,avg+slope*(n-center))
    if model=='seasonal_naive_7': return values[-7] if len(values)>=7 else mean(values)
    if model=='holt_winters_7':
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        fit=ExponentialSmoothing(values,trend='add',seasonal='add',seasonal_periods=7,initialization_method='estimated').fit(optimized=True)
        return max(0,float(fit.forecast(1)[0]))
    raise Invalid('Unknown forecast model.')


def backtest(values,models):
    # Expanding training window; no observation on/after cutoff enters its model.
    results=[]; cutoffs=range(max(14,len(values)-14),len(values))
    for model in models:
        residuals=[]; actuals=[]
        try:
            for cutoff in cutoffs:
                forecast=predict(values[:cutoff],model)
                residuals.append(values[cutoff]-forecast); actuals.append(values[cutoff])
            if not residuals: continue
            absolute=sum(abs(v) for v in residuals)
            results.append({'model':model,'mae':absolute/len(residuals),'wape':absolute/sum(actuals) if sum(actuals)>0 else None,
                            'folds':len(residuals),'residual_std':pstdev(residuals)})
        except (ValueError,ImportError,ArithmeticError):
            continue
    return sorted(results,key=lambda r:r['mae'])


def forecast(rows, allow_advanced=False):
    usable=[r for r in rows if r['usable']]
    event_count=sum(r['events'] for r in usable)
    demand_days=sum(r['demand']>0 for r in usable)
    coverage=len(usable)/len(rows) if rows else 0
    result={'stage':0,'status':'Collecting Inventory History','days_recorded':len(usable),'calendar_days':len(rows),
            'valid_demand_events':event_count,'demand_days':demand_days,'coverage':coverage,'model_version':MODEL_VERSION,
            'next_milestone':'14 usable days and at least 5 confirmed demand events','models':[],
            'exclusions':{'missing':sum(not r['complete'] for r in rows),'censored':sum(r['censored'] for r in rows),
                          'unexplained':sum(r['unexplained'] for r in rows)}}
    if len(usable)<14 or event_count<5 or demand_days<3: return result
    result.update(stage=1,status='Descriptive estimates',average_daily_demand=mean(r['demand'] for r in usable),
                  next_milestone='30 consecutive usable days and at least 10 demand events across 8 days')
    # Use an unbroken usable suffix: compacting missing dates would distort intervals and weekly patterns.
    suffix=[]
    for row in reversed(rows):
        if not row['usable']: break
        suffix.append(row['demand'])
    values=list(reversed(suffix))
    if len(values)<30 or event_count<10 or demand_days<8: return result
    models=['moving_average','exponential_smoothing','croston_sba']
    stage=2
    if len(values)>=90: models+=['trend','seasonal_naive_7']; stage=3
    if len(values)>=180:
        stage=4
        if allow_advanced: models.append('holt_winters_7')
    scores=backtest(values,models)
    if not scores: return result
    winner=scores[0]
    # A complex seasonal candidate must materially beat the best transparent baseline.
    if winner['model']=='holt_winters_7':
        baseline=min((s for s in scores if s['model']!='holt_winters_7'),key=lambda r:r['mae'],default=None)
        if baseline and winner['mae']>baseline['mae']*.98: winner=baseline
    result.update(stage=stage,status='Backtested advisory forecast',models=scores,model=winner['model'],
                  daily_forecast=predict(values,winner['model']),uncertainty='Low confidence: limited history' if len(values)<90 else 'Empirical backtest uncertainty; not a calibrated probability',
                  error_std=winner['residual_std'],training_days=len(values),next_milestone='Continue collecting confirmed demand and complete business days')
    if stage==4: result['seasonal_note']='Weekly patterns may be evaluated; one year is limited evidence for annual seasonality. Holiday effects are not assumed.'
    return result


def policy(data):
    out={k:integer(data.get(k,default),k,lo,hi) for k,default,lo,hi in (
        ('lead_time',7,0,365),('review_period',7,1,365),('minimum_order',0,0,2_000_000_000),
        ('order_multiple',1,1,2_000_000_000),('service_level',95,50,99),('reorder_threshold',0,0,2_000_000_000))}
    for k in ('maximum_stock','confirmed_incoming','committed_outgoing'):
        value=data.get(k)
        out[k]=None if value in (None,'') else integer(value,k)
    return out


def replenishment(stock, analysis, settings, today=None):
    if analysis['stage']<1: return {'status':'Insufficient demand history; no recommendation.'}
    p=policy(settings); demand=analysis.get('daily_forecast',analysis['average_daily_demand'])
    incoming=p['confirmed_incoming']; outgoing=p['committed_outgoing']
    position=stock+(incoming or 0)-(outgoing or 0)
    sigma=analysis.get('error_std',0)
    safety=ceil(NormalDist().inv_cdf(p['service_level']/100)*sigma*sqrt(p['lead_time']))
    lead=demand*p['lead_time']; reorder=ceil(lead+safety)
    target=ceil(demand*(p['lead_time']+p['review_period'])+safety)
    if p['maximum_stock'] is not None: target=min(target,p['maximum_stock'])
    qty=max(0,target-position)
    if qty: qty=ceil(max(qty,p['minimum_order'])/p['order_multiple'])*p['order_multiple']
    constraint=None
    if p['maximum_stock'] is not None and position+qty>p['maximum_stock'] and qty:
        constraint='MOQ/order multiple conflicts with maximum stock; review manually.'; qty=None
    days=max(0,stock-(outgoing or 0))/demand if demand>0 else None
    return {'status':'Advisory only','current_stock':stock,'daily_demand':demand,'inventory_position':position,
            'expected_lead_time_demand':lead,'safety_stock':safety,'reorder_point':reorder,'target_stock':target,
            'suggested_quantity':qty,'coverage_days':days,
            'estimated_stockout_date':((today or date.today())+timedelta(days=min(36500,ceil(days)))).isoformat() if days is not None else None,
            'incomplete_position':incoming is None or outgoing is None,'constraint':constraint,
            'explanation':f'Confirmed demand averages {analysis["average_daily_demand"]:.2f} units/day over usable recorded days. On-hand stock is {stock}.',
            'safety_stock_note':'Normal approximation using one-day backtest errors; independence assumed. Zero when no forecast error estimate is available.'}


def refresh(db, allow_advanced=False):
    end=(date.fromisoformat(business_date())-timedelta(days=1)).isoformat()
    deployment=db.platform_meta.find_one({'_id':'collection_started'})
    if not deployment: return 0
    start=max(deployment['date'],(date.fromisoformat(end)-timedelta(days=729)).isoformat())
    if start>end: return 0
    coverage={d['_id']:d.get('complete',False) for d in db.business_days.find({'_id':{'$gte':start,'$lte':end}})}
    movements=list(db.stock_movements.find({'business_date':{'$gte':start,'$lte':end}}))
    grouped=defaultdict(list)
    for m in movements: grouped[m['uid']].append(m)
    # Products without any movements stay at Stage 0 and need no dense daily documents.
    product_ids=set(grouped)
    count=0
    for product in db.products.find({'uid':{'$in':list(product_ids)}}):
        uid=product.get('uid')
        if not uid: continue
        rows=aggregate(grouped[uid],coverage,start,end,uid)
        result=forecast(rows,allow_advanced)
        for row in rows: db.daily_demand.replace_one({'uid':uid,'date':row['date']},row,upsert=True)
        settings=db.inventory_settings.find_one({'_id':'product:'+uid}) or {}
        plan=replenishment(product.get('stock',0),result,settings,date.fromisoformat(business_date()))
        db.forecasts.insert_one({'_id':str(uuid4()),'uid':uid,'generated_at':stamp(),'data_cutoff':end,
                                 'analysis':result,'replenishment':plan,'policy_snapshot':policy(settings)})
        count+=1
    db.inventory_sync_control.update_one({'_id':'forecast_job'},{'$set':{'last_date':business_date(),'completed_at':stamp(),'products':count}},upsert=True)
    return count
