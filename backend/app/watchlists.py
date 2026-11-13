"""Private durable watchlist definitions and bounded read-only quote snapshots.

The trading journal is authoritative. This module never creates a fill, deletes
trade data, downloads a master, opens a socket or authenticates with a broker.
"""
from __future__ import annotations
import copy
import math
import threading
import uuid
from datetime import datetime,timezone
from itertools import chain
from fastapi import HTTPException
from .database import iso_now
from .models import Setting
from .instruments import contract_master_status,IST
from .trade_categories import CATEGORIES,instrument_category

LIMITS={'watchlists':10,'items_per_watchlist':50,'unique_instruments':100,'search_results':50,'search_inspections':10000}
KEY='watchlists_v1'
CONTRACT_FIELDS=('id','instrument_id','broker','token','underlying','name','symbol','exchange','segment','expiry','strike',
                 'option_type','instrument_type','lot_size','tick_size','demo','source','isin','price_multiplier',
                 'monetary_conversion_source','master_active','master_source_date','master_refresh_at')
IDENTITY_FIELDS=('broker','exchange','segment','underlying','expiry','strike','option_type','lot_size','isin','price_multiplier')
QUOTE_FIELDS=('broker','instrument_id','symbol','underlying','exchange','segment','ltp','bid','ask','previous_close','volume',
              'open_interest','open','high','low','received_at','exchange_timestamp','exchange_timestamp_basis','source','demo')

def number(value):
    return float(value) if isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value) else None

def contract_view(contract):
    result={key:copy.deepcopy(contract[key]) for key in CONTRACT_FIELDS if key in contract}
    result['instrument_id']=str(contract.get('instrument_id') or contract.get('id') or '')
    result['id']=result['instrument_id']
    return result

def defaults():
    return {'revision':0,'watchlists':[{'id':'watchlist-1','name':'Watchlist 1','items':[]},
                                     {'id':'watchlist-2','name':'Watchlist 2','items':[]},
                                     {'id':'favourites','name':'Favourites','items':[]}]}

def read_state(db):
    row=db.get(Setting,KEY)
    value=copy.deepcopy(row.value) if row else defaults()
    if not isinstance(value,dict) or not isinstance(value.get('watchlists'),list) or len(value['watchlists'])>LIMITS['watchlists']:
        raise HTTPException(503,'Saved watchlists need recovery. No watchlist or trading records were changed.')
    return value

def clean_name(value):
    if not isinstance(value,str) or not value.strip() or len(value.strip())>60 or any(ord(char)<32 for char in value):
        raise HTTPException(422,'Use a watchlist name of 1–60 characters without control characters.')
    return value.strip()

def verify_contract(contract,now=None,previous=None):
    if not contract or contract.get('broker')!='kotak' or contract.get('demo') is not False:
        return False,'Instrument is not in the current Kotak Neo master. Saved history is retained.'
    if previous and any(previous.get(field)!=contract.get(field) for field in IDENTITY_FIELDS):
        return False,'The broker identifier now refers to a different contract. Select the current instrument explicitly.'
    if str(contract.get('segment','')).lower() not in {'nse_cm','bse_cm','nse_fo','bse_fo','mcx_fo'}:
        return False,'This exchange segment is not enabled for the watchlist.'
    moment=now or datetime.now(timezone.utc)
    expiry=contract.get('expiry')
    if expiry and str(expiry)[:10]<moment.astimezone(IST).date().isoformat():
        return False,'Contract expired. Existing paper trades and history are retained.'
    status=contract_master_status(contract,moment)
    if not status['ready']:return False,status['reason']
    return True,None

def public_quote(quote,contract,stale_after,age_seconds):
    if not quote or quote.get('demo') is not False or quote.get('broker')!='kotak' or quote.get('instrument_id')!=contract['instrument_id']:
        return None
    if any(quote.get(key) is not None and quote.get(key)!=contract.get(key) for key in ('exchange','segment','underlying','expiry','strike','option_type','lot_size')):return None
    result={key:copy.deepcopy(quote.get(key)) for key in QUOTE_FIELDS}
    age=age_seconds(quote.get('received_at'))
    exchange_age=age_seconds(quote.get('exchange_timestamp'))
    stale=age is None or age>stale_after or (quote.get('exchange_timestamp_basis')=='last_update_time' and (exchange_age is None or exchange_age>stale_after))
    result.update(quote_age_seconds=age,exchange_quote_age_seconds=exchange_age,stale=stale)
    last,previous=number(quote.get('ltp')),number(quote.get('previous_close'))
    result['change']=round(last-previous,4) if last is not None and previous is not None and previous>0 else None
    result['change_percent']=round((last-previous)/previous*100,4) if last is not None and previous is not None and previous>0 else None
    return result

class WatchlistService:
    def __init__(self):
        self._search_lock=threading.Lock();self._search_identity=None;self._search_groups=[]

    def snapshot(self,db,catalog,settings,quote_getter,age_seconds,*,feed=None,now=None,active_list_id=None):
        state=read_state(db);valid_ids=set()
        active_id=active_list_id or state['watchlists'][0]['id']
        if not any(saved['id']==active_id for saved in state['watchlists']):raise HTTPException(404,'Watchlist not found.')
        quotes={}
        for saved in state['watchlists']:
            items=[]
            for item in saved.get('items',[]):
                identifier=item['instrument_id'];old=item.get('contract') or {}
                current=catalog.find(identifier,'kotak') if catalog else None
                valid,problem=verify_contract(current,now,old)
                contract=contract_view(current if valid else old or current or {'id':identifier,'broker':'kotak'})
                if identifier not in quotes:quotes[identifier]=quote_getter(db,identifier,'kotak')
                quote=public_quote(quotes[identifier],contract,settings['stale_after_seconds'],age_seconds) if contract.get('instrument_id') else None
                items.append({'instrument_id':identifier,'broker':'kotak','category':instrument_category(contract),
                              'contract':contract,'valid':valid,'validation_error':problem,'quote':quote})
                if valid and saved['id']==active_id:valid_ids.add(identifier)
            saved['items']=items
        return {**state,'active_list_id':active_id,'limits':dict(LIMITS),'mode':'PAPER','workspace_demo':bool(settings.get('demo_mode')),
                'feed':copy.deepcopy(feed or {'status':'disconnected'}),'server_time':iso_now()},valid_ids

    def mutate(self,db,catalog,action,*,list_id=None,name=None,instrument_ids=None,revision=None,now=None):
        state=read_state(db)
        if revision is not None and revision!=state.get('revision',0):
            raise HTTPException(409,'This watchlist changed on another device. Reload it before saving again.')
        lists=state['watchlists'];target=next((row for row in lists if row['id']==list_id),None)
        if action=='create':
            if len(lists)>=LIMITS['watchlists']:raise HTTPException(422,'The watchlist limit has been reached.')
            lists.append({'id':'watchlist-'+uuid.uuid4().hex[:16],'name':clean_name(name),'items':[]})
        else:
            if target is None:raise HTTPException(404,'Watchlist not found.')
            if action=='delete':
                if len(lists)<=1:raise HTTPException(409,'Keep at least one watchlist.')
                lists.remove(target)
            elif action=='update':
                if name is not None:target['name']=clean_name(name)
                if instrument_ids is not None:
                    if len(instrument_ids)>LIMITS['items_per_watchlist']:raise HTTPException(422,'This watchlist contains too many instruments.')
                    if len(set(instrument_ids))!=len(instrument_ids):raise HTTPException(422,'Add each instrument only once to a watchlist.')
                    previous={item['instrument_id']:item for item in target['items']};items=[]
                    for identifier in instrument_ids:
                        if identifier in previous:
                            items.append(previous[identifier]);continue
                        current=catalog.find(identifier,'kotak') if catalog else None
                        valid,problem=verify_contract(current,now)
                        if not valid:raise HTTPException(422,problem)
                        items.append({'instrument_id':identifier,'broker':'kotak','category':instrument_category(current),'contract':contract_view(current)})
                    target['items']=items
            else:raise ValueError('Unknown watchlist action')
        identifiers={item['instrument_id'] for row in lists for item in row['items']}
        if len(identifiers)>LIMITS['unique_instruments']:raise HTTPException(422,'The total unique watchlist-instrument limit has been reached.')
        state['revision']=int(state.get('revision',0))+1;state['updated_at']=iso_now()
        row=db.get(Setting,KEY)
        if row:row.value=copy.deepcopy(state)
        else:db.add(Setting(key=KEY,value=copy.deepcopy(state)))
        return state

    def _groups(self,catalog):
        identity=(id(catalog),getattr(catalog,'revision',None),id(getattr(catalog,'stock_rows',None)),len(getattr(catalog,'_live',[])))
        with self._search_lock:
            if self._search_identity==identity:return self._search_groups
            groups={}
            for row in chain(getattr(catalog,'stock_rows',()),getattr(catalog,'_live',())):
                if row.get('broker')!='kotak' or row.get('demo') is not False:continue
                category=instrument_category(row);symbol=str(row.get('underlying','')).upper()
                key=(category,symbol,str(row.get('exchange','')))
                group=groups.setdefault(key,{'category':category,'symbol':symbol,'text':symbol+' '+str(row.get('name') or '').upper(),'rows':[]})
                group['rows'].append(row)
            self._search_groups=[groups[key] for key in sorted(groups,key=lambda key:(CATEGORIES.index(key[0]),key[1],key[2]))]
            self._search_identity=identity
            return self._search_groups

    def search(self,catalog,query='',category=None,limit=30,*,underlying=None,expiry=None,now=None):
        if category is not None and category not in CATEGORIES:raise HTTPException(422,'Choose Stocks, F&O or Commodities.')
        tokens=query.strip().upper().split();groups=self._groups(catalog) if catalog else []
        groups=[group for group in groups if not category or group['category']==category]
        if underlying:groups=[group for group in groups if group['rows'][0].get('underlying')==underlying]
        if category=='stocks' and not groups:
            return {'instruments':[],'total':0,'truncated':False,'selection_required':True,'message':'Official stock listings have not loaded. Use Refresh stock listings to request the current Kotak cash master.','limit':limit,'mode':'PAPER'}
        # Narrow symbol/company queries before inspecting exact derivative contracts.
        if tokens:
            named=[group for group in groups if tokens[0] in group['text'] or (len(group['symbol'])>=2 and tokens[0].startswith(group['symbol']))]
            if named:groups=named
            else:return {'instruments':[],'total':0,'truncated':False,'selection_required':True,'message':'Search by company or underlying symbol first; then add expiry, strike or instrument type.','limit':limit,'mode':'PAPER'}
        inspected=0;selection_required=False
        result=[];seen=set();limit=min(LIMITS['search_results'],max(1,limit));truncated=False
        for group in groups:
            for row in group['rows']:
                inspected+=1
                if inspected>LIMITS['search_inspections']:
                    truncated=True;selection_required=True;break
                identifier=row.get('instrument_id') or row.get('id')
                if identifier in seen or (expiry and row.get('expiry')!=expiry):continue
                text=' '.join(str(row.get(key) or '') for key in ('symbol','name','underlying','expiry','strike','option_type')).upper()
                if tokens and not all(token in text for token in tokens):continue
                valid,problem=verify_contract(row,now)
                if row.get('expiry') and str(row['expiry'])[:10]<(now or datetime.now(timezone.utc)).astimezone(IST).date().isoformat():continue
                if len(result)==limit:truncated=True;break
                seen.add(identifier)
                result.append({'instrument_id':identifier,'broker':'kotak','category':group['category'],'contract':contract_view(row),'valid':valid,'validation_error':problem})
            if truncated:break
        return {'instruments':result,'total':len(result),'truncated':truncated,'selection_required':selection_required,'message':'Narrow the company/underlying, expiry or strike to search more contracts.' if selection_required else None,'limit':limit,'source':'Kotak Neo current instrument masters','mode':'PAPER'}
