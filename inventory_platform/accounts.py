"""Password-free selection with stable identities, explicit roles and safe migrations."""
from datetime import datetime, timezone
from uuid import uuid4
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from .domain import username, bounded_text, stamp, Conflict, Invalid, PERMISSIONS

INITIAL_USERS=('sg','szee foon','tommy','HK','MsT','Admin')


def find_user(db, user_id=None, name=None):
    if user_id:
        ids=[user_id]
        if ObjectId.is_valid(user_id): ids.append(ObjectId(user_id))
        return db.users.find_one({'_id':{'$in':ids}})
    if not name: return None
    key=name.casefold()
    matches=[u for u in db.users.find({}) if str(u.get('username','')).casefold()==key]
    return matches[0] if len(matches)==1 else None


def public_user(u):
    return {'id':str(u['_id']),'username':u.get('username',''),'display_name':u.get('display_name') or u.get('full_name') or u.get('username',''),
            'role':u.get('role','worker'),'active':u.get('active',True),'created_at':u.get('created_at'),
            'last_login':u.get('last_login'),'revision':u.get('account_revision',0)}


def migrate_users(db, apply=False):
    existing=list(db.users.find({})); keys={}
    for u in existing:
        display,key=username(u.get('username',''))
        if key in keys: raise Conflict('Case-insensitive username collisions exist. Resolve them explicitly before migration.')
        keys[key]=u
    missing=[name for name in INITIAL_USERS if name.casefold() not in keys]
    promote=[u['username'] for u in existing if u.get('role')=='worker']
    plan={'existing':len(existing),'create':missing,'promote_to_admin':promote,'apply':apply}
    if not apply: return plan
    # Intended for a maintenance window; web writes stay gated until this finishes.
    db.users.create_index('username_key',unique=True,partialFilterExpression={'username_key':{'$type':'string'}})
    for u in existing:
        name,key=username(u['username'])
        updates={'username_key':key,'active':u.get('active',True),'account_revision':u.get('account_revision',0)}
        if u.get('role')=='worker': updates['role']='admin'
        if 'created_at' not in u: updates['created_at']=None  # unknown historical creation date is not invented
        db.users.update_one({'_id':u['_id']},{'$set':updates})
    for name in missing:
        db.users.insert_one({'_id':str(uuid4()),'username':name,'username_key':name.casefold(),'display_name':name,
                             'role':'admin','active':True,'created_at':stamp(),'account_revision':0})
    db.platform_meta.update_one({'_id':'users_ready'},{'$set':{'ready':True,'at':stamp()}},upsert=True)
    return plan


def permissions(db,user):
    config=db.inventory_settings.find_one({'_id':'permissions'}) or {}
    roles=config.get('roles',{'admin':list(PERMISSIONS),'worker':[]})
    return set(roles.get(user.get('role','worker'),[])) & set(PERMISSIONS)


def save_user(store, data, actor, user_id=None):
    db=store.db
    if not db.platform_meta.find_one({'_id':'users_ready','ready':True}):
        raise Conflict('Run the reviewed user migration before managing accounts.')
    name,key=username(data.get('username',''))
    role=data.get('role','admin')
    if role != 'admin': raise Invalid('New and edited accounts must be Admin.')
    active=data.get('active',True)
    if type(active) is not bool: raise Invalid('Active must be true or false.')
    display=bounded_text(data.get('display_name',name),'Display name',100,True)
    new_id=str(uuid4())
    def work(kw):
        # Serialize last-admin changes within the transaction to avoid write skew.
        db.inventory_settings.update_one({'_id':'user_admin_lock'},{'$inc':{'revision':1}},upsert=True,**kw)
        old=None
        if user_id:
            ids=[user_id]+([ObjectId(user_id)] if ObjectId.is_valid(user_id) else [])
            old=db.users.find_one({'_id':{'$in':ids}},**kw)
            if not old: raise Invalid('Account not found.')
            if data.get('revision')!=old.get('account_revision',0): raise Conflict('Account changed. Refresh before saving.')
            if old.get('role')=='admin' and old.get('active',True) and (role!='admin' or not active):
                if db.users.count_documents({'role':'admin','active':{'$ne':False}},**kw)<=1:
                    raise Invalid('Keep at least one active administrator.')
        values={'username':name,'username_key':key,'display_name':display,'role':role,'active':active,
                'updated_at':stamp(),'account_revision':(old or {}).get('account_revision',-1)+1}
        if old:
            aliases=list(dict.fromkeys(old.get('username_aliases',[])+[old['username']]))
            values['username_aliases']=aliases
            db.users.update_one({'_id':old['_id']},{'$set':values},**kw)
            result={**old,**values}
        else:
            result={'_id':new_id,'created_at':stamp(),**values}
            db.users.insert_one(result,**kw)
        db.audit_events.insert_one({'_id':str(uuid4()),'action':'user_updated' if old else 'user_created',
            'timestamp':stamp(),'user_id':actor['id'],'username':actor['username'],'target_user_id':str(result['_id']),
            'before':public_user(old) if old else None,'after':public_user(result)},**kw)
        return public_user(result)
    try: return store.transact(work)
    except DuplicateKeyError: raise Conflict('That username already exists (case-insensitive).') from None
