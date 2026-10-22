"""Paper-only ledger, weighted-average accounting and durable quote capture.

No broker order submission method is imported or called from this module.
Active-position receipts are durable until valuation completes. Compact mode
keeps current marks and observed trade extrema, without retaining tick history.
"""
import copy
import hashlib
import json
import math
import time
import uuid
import logging
import threading
import weakref
from collections import OrderedDict
from datetime import datetime, timezone
from sqlalchemy import select, delete, update, func, event as sqlalchemy_event
from sqlalchemy.orm import Session
from fastapi import HTTPException
from .database import iso_now
from .models import (Setting, Trade, Event, Timeline, QuoteEvent, ActionRecord,
                     PendingQuote, LatestQuote, QuoteReceiptCounter, CompactQuoteReceipt)
from .payoff import strategy_risk

BROKERS={'dhan':'Dhan','kotak':'Kotak Neo','fivepaisa':'5paisa'}
DEFAULT_SETTINGS={'brokerage_per_order':20.0,'charges_percent':0.05,'slippage_bps':0.0,'fill_basis':'bid_ask','logging_mode':'summary','recording_policy_version':2,'preferred_feed':'broker','stale_after_seconds':10,'demo_mode':True,'paper_capital':1000000.0,'delete_after_local_save':False,'auto_delete_online':False}
catalog = None
log=logging.getLogger(__name__)
_caches=weakref.WeakKeyDictionary()
_cache_lock=threading.RLock()


def _cache(db):
    binding=db.get_bind()
    binding=getattr(binding,'engine',binding)
    with _cache_lock:
        if binding not in _caches:
            _caches[binding]={'quotes':OrderedDict(),'trades':None,'settings':None,
                              'quotes_loaded':False,'baselines':{},'portfolio':[]}
        return _caches[binding]


def clear_cache(db=None):
    """Use after out-of-process maintenance; a rollback also invalidates caches."""
    with _cache_lock:
        if db is None:_caches.clear()
        else:
            binding=db.get_bind();_caches.pop(getattr(binding,'engine',binding),None)


@sqlalchemy_event.listens_for(Session,'after_rollback')
def _rollback_cache(db):
    clear_cache(db)


@sqlalchemy_event.listens_for(Session,'before_flush')
def _orm_changes_cache(db,flush_context,instances):
    state=_cache(db)
    for row in list(db.new)+list(db.dirty):
        if isinstance(row,Trade) and state['trades'] is not None:
            state['trades'][row.id]=copy.deepcopy(row.data)
        if isinstance(row,Timeline):state['baselines'].clear()
    for row in db.deleted:
        if isinstance(row,Trade) and state['trades'] is not None:state['trades'].pop(row.id,None)


def trade_records(db):
    """The service runs one writer; reuse its ledger instead of rereading JSON."""
    state=_cache(db)
    with _cache_lock:
        if state['trades'] is None:
            state['trades']={row.id:copy.deepcopy(row.data) for row in db.scalars(select(Trade))}
        # Include direct ORM changes in the caller's transaction (including
        # maintenance/test imports), without publishing stale cached values.
        for row in list(db.new)+list(db.dirty):
            if isinstance(row,Trade):state['trades'][row.id]=copy.deepcopy(row.data)
        for row in db.deleted:
            if isinstance(row,Trade):state['trades'].pop(row.id,None)
        return copy.deepcopy(list(state['trades'].values()))

def set_catalog(value):
    global catalog
    catalog=value
    clear_cache()

def get_settings(db):
    state=_cache(db)
    if state['settings'] is None:
        row=db.get(Setting,'app');stored=row.value if row else {}
        result={**DEFAULT_SETTINGS,**stored}
        # Older installs inherited every-tick logging. Apply the user's new
        # compact policy once, while still supporting deliberate later choices.
        if stored.get('recording_policy_version',0)<2:
            result['logging_mode']='summary';result['recording_policy_version']=2
        state['settings']=result
    return copy.deepcopy(state['settings'])

def update_settings(db, changes):
    settings=get_settings(db)
    if 'delete_after_local_save' in changes: changes['auto_delete_online']=changes['delete_after_local_save']
    if 'auto_delete_online' in changes: changes['delete_after_local_save']=changes['auto_delete_online']
    settings.update(changes)
    settings['recording_policy_version']=2
    row=db.get(Setting,'app')
    if row: row.value=settings
    else:db.add(Setting(key='app',value=settings))
    _cache(db)['settings']=copy.deepcopy(settings)
    db.add(Event(trade_id='SETTINGS',kind='configuration_change',data={'changes':changes},timestamp=iso_now()))
    return settings

def ident(prefix):return f'{prefix}_{uuid.uuid4().hex[:16]}'

def event(db,trade,kind,data=None,at=None):
    db.add(Event(trade_id=trade['id'],kind=kind,timestamp=at or iso_now(),data=data or {}))

def age_seconds(timestamp):
    if not timestamp:return None
    try:
        value=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
        if not value.tzinfo:value=value.replace(tzinfo=timezone.utc)
        return max(0,(datetime.now(timezone.utc)-value).total_seconds())
    except (ValueError,TypeError):return None

def contract_identity(value):
    try:strike=float(value.get('strike'))
    except (TypeError,ValueError):strike=None
    return (str(value.get('underlying')),str(value.get('expiry')),strike,str(value.get('option_type')))

def contract_for(instrument_id,broker,demo):
    if catalog is None:raise HTTPException(503,'The instrument catalog has not loaded.')
    requested=catalog.find(instrument_id)
    if not requested:raise HTTPException(422,'Select a valid contract from the instrument master.')
    if bool(requested.get('demo')) != bool(demo):
        raise HTTPException(422,'Demo contracts and broker contracts cannot be mixed.')
    if requested.get('broker')==broker:return requested
    for item in catalog.list_contracts(broker=broker,demo=demo):
        if contract_identity(item)==contract_identity(requested):return item
    raise HTTPException(422,f'No matching contract in {BROKERS[broker]} instrument master.')

def latest_quote(db,instrument_id,broker):
    key=(broker,instrument_id);state=_cache(db)
    if key in state['quotes']:return copy.deepcopy(state['quotes'][key])
    latest=db.get(LatestQuote,{'broker':broker,'instrument_id':instrument_id})
    if latest:
        cache_quote(db,latest.data,persist=False)
        return copy.deepcopy(latest.data)
    row=db.scalars(select(QuoteEvent).where(QuoteEvent.instrument_id==instrument_id,QuoteEvent.broker==broker).order_by(QuoteEvent.id.desc()).limit(1)).first()
    if row:
        value={**row.data,'event_id':row.id};cache_quote(db,value,persist=False)
        return value
    return None


def cache_quote(db,quote,persist=False):
    """Bounded live lookup. Inactive option-chain rows need no database writes."""
    value=copy.deepcopy(quote);key=(value['broker'],value['instrument_id'])
    state=_cache(db)
    with _cache_lock:
        state['quotes'][key]=value;state['quotes'].move_to_end(key)
        while len(state['quotes'])>5000:state['quotes'].popitem(last=False)
    if persist:
        values={'broker':value['broker'],'instrument_id':value['instrument_id'],
                'received_at':value['received_at'],'data':value}
        dialect=db.get_bind().dialect.name
        if dialect=='postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        statement=insert(LatestQuote).values(**values)
        db.execute(statement.on_conflict_do_update(index_elements=['broker','instrument_id'],
                    set_={'received_at':value['received_at'],'data':value}))
    return value

def resolve_quote(db,contract,broker,settings):
    source=settings.get('preferred_feed','broker')
    source=broker if source=='broker' else source
    try:instrument=contract_for(contract.get('instrument_id',contract.get('id')),source,settings['demo_mode'])
    except HTTPException:return None
    return latest_quote(db,instrument.get('instrument_id',instrument['id']),source)

def fill_reference(quote,side,settings):
    price=quote['ltp']
    if settings['fill_basis']=='bid_ask':price=(quote.get('ask') if side=='BUY' else quote.get('bid')) or price
    return float(price)

def execution_price(db,contract,side,broker,settings,manual=None,quote_override=None,limit_price=None):
    if manual is not None:
        if not math.isfinite(float(manual)) or manual<=0:raise HTTPException(422,'Paper fill price must be positive and finite.')
        return float(manual),{'source':'manual_paper_assumption','ltp':float(manual),'received_at':iso_now()},0.0
    # Automated/replayed fills must use their triggering receipt, even when
    # later quotes already exist in the journal. Interactive actions use latest.
    quote=quote_override if quote_override is not None else resolve_quote(db,contract,broker,settings)
    if not quote:raise HTTPException(409,'No quote is available. Connect the market-data feed first.')
    age=age_seconds(quote.get('received_at'))
    if age is None or age>settings['stale_after_seconds']:
        raise HTTPException(409,'The selected market-data quote is stale. Reconnect before recording a paper fill.')
    price=fill_reference(quote,side,settings)
    slip=price*settings['slippage_bps']/10000
    effective=max(.01,price+(slip if side=='BUY' else -slip))
    if limit_price is not None:
        effective=min(effective,limit_price) if side=='BUY' else max(effective,limit_price)
    return effective,quote,abs(effective-price)

def charges(price,quantity,settings):
    return settings['brokerage_per_order']+price*quantity*settings['charges_percent']/100

def monetary_units(leg):
    return leg['lot_size']*float(leg.get('price_multiplier') or 1)

def recalculate(trade,at=None):
    at=at or iso_now()
    from zoneinfo import ZoneInfo
    moment=datetime.fromisoformat(at.replace('Z','+00:00'))
    if not moment.tzinfo:moment=moment.replace(tzinfo=timezone.utc)
    day=moment.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
    mark=trade.get('_daily_pnl')
    if not mark or mark['day']!=day:
        previous=trade.get('net_pnl',0.0)
        created=datetime.fromisoformat(trade['created_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
        updated=trade.get('updated_at',trade['created_at'])
        updated_day=datetime.fromisoformat(updated.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
        trade['_daily_pnl']={'day':day,'baseline':0.0 if created==day else previous,
                             'complete':created==day or updated_day<day}
    for leg in trade['legs']:
        direction=1 if leg['side']=='BUY' else -1
        leg['quantity']=leg['lots']*leg['lot_size']
        leg['open_quantity']=leg['open_lots']*leg['lot_size']
        leg['valuation_quantity']=leg['open_lots']*monetary_units(leg)
        leg['unrealized_pnl']=round(direction*(leg['current_price']-leg['entry_price'])*leg['valuation_quantity'],8)
        leg['gross_pnl']=round(leg['realized_pnl']+leg['unrealized_pnl'],8)
        leg['net_pnl']=round(leg['gross_pnl']-leg['charges'],8)
    trade['realized_pnl']=sum(l['realized_pnl'] for l in trade['legs'])
    trade['unrealized_pnl']=sum(l['unrealized_pnl'] for l in trade['legs'])
    trade['gross_pnl']=trade['realized_pnl']+trade['unrealized_pnl']
    trade['charges']=sum(l['charges'] for l in trade['legs'])
    trade['net_pnl']=trade['gross_pnl']-trade['charges']
    if trade.get('mfe') is None or trade['net_pnl']>trade['mfe']:
        trade['mfe']=trade['net_pnl'];trade['mfe_at']=at
    if trade.get('mae') is None or trade['net_pnl']<trade['mae']:
        trade['mae']=trade['net_pnl'];trade['mae_at']=at
    trade['profit_giveback']=max(0,trade['mfe']-trade['net_pnl'])
    active=[l for l in trade['legs'] if l['open_lots']>0]
    pending=any(l['status']=='pending' for l in trade['legs'])
    if active:trade['status']='open'
    elif pending:trade['status']='pending'
    else:trade['status']='closed' if any(l['filled_lots']>0 for l in trade['legs']) else 'cancelled'
    if trade['status'] in ('closed','cancelled') and not trade.get('closed_at'):trade['closed_at']=at
    risk=strategy_risk(active,trade.get('spot'),trade['charges'])
    if active:trade['risk']=risk
    trade['margin_estimate']=risk.get('estimated_margin',0)
    trade['hedge_benefit']=risk.get('hedge_benefit',0)
    trade['updated_at']=at
    return trade

def store_trade(db,trade):
    values={'id':trade['id'],'broker':trade['broker'],'status':trade['status'],'data':copy.deepcopy(trade)}
    dialect=db.get_bind().dialect.name
    if dialect=='postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement=insert(Trade).values(**values)
    db.execute(statement.on_conflict_do_update(index_elements=['id'],set_={k:v for k,v in values.items() if k!='id'}))
    # Existing ORM identities must agree with the Core upsert in this session.
    row=db.identity_map.get((Trade,(trade['id'],),None))
    if row:
        from sqlalchemy.orm.attributes import set_committed_value
        set_committed_value(row,'data',copy.deepcopy(trade));set_committed_value(row,'status',trade['status'])
    state=_cache(db)
    with _cache_lock:
        if state['trades'] is not None:state['trades'][trade['id']]=copy.deepcopy(trade)

def public_trade(trade,settings=None):
    result=copy.deepcopy(trade)
    for leg in result['legs']:
        age=age_seconds(leg.get('quote_at'))
        leg['feed_age']=None if age is None else round(age,3)
        leg['stale']=age is None or age>(settings or DEFAULT_SETTINGS)['stale_after_seconds']
    result['valuation_stale']=bool(result.get('valuation_error')) or any(l['stale'] for l in result['legs'] if l['open_lots']>0)
    for key in ('realized_pnl','unrealized_pnl','gross_pnl','net_pnl','charges','mfe','mae','net_premium','profit_giveback','margin_estimate','hedge_benefit'):
        if result.get(key) is not None:result[key]=round(result[key],2)
    return result

def snapshot(db,trade,kind='snapshot',force=False):
    if get_settings(db)['logging_mode']=='summary':return None
    now=iso_now()
    quote_ids=[l.get('quote_event_id') for l in trade['legs']]
    carry=quote_ids==trade.get('_snapshot_quote_ids')
    trade['_snapshot_quote_ids']=quote_ids
    data={'timestamp':now,'kind':kind,'net_pnl':trade['net_pnl'],'gross_pnl':trade['gross_pnl'],'realized_pnl':trade['realized_pnl'],'unrealized_pnl':trade['unrealized_pnl'],'charges':trade['charges'],'carried_forward':carry,'broker':trade['broker'],'legs':[{'instrument_id':l['instrument_id'],'symbol':l['symbol'],'ltp':l['current_price'],'bid':l.get('bid'),'ask':l.get('ask'),'open_lots':l['open_lots'],'average_entry_price':l['entry_price'],'realized_pnl':l['realized_pnl'],'unrealized_pnl':l['unrealized_pnl'],'charges':l['charges'],'net_pnl':l.get('net_pnl',0),'quote_at':l.get('quote_at'),'feed_age':age_seconds(l.get('quote_at')),'event_id':l.get('quote_event_id')} for l in trade['legs']]}
    db.add(Timeline(trade_id=trade['id'],timestamp=now,data=data))
    return data

def archive_close(db,trade):
    if trade.get('export_queued') or trade['status']!='closed':return
    db.flush()
    if get_settings(db)['logging_mode']=='summary':
        from .reporting import enqueue_export
        enqueue_export(db,{**public_trade(trade),'report_detail':'summary'},[],[],[])
        trade['export_queued']=True
        return
    events=[{'id':e.id,'kind':e.kind,'timestamp':e.timestamp,**e.data} for e in db.scalars(select(Event).where(Event.trade_id==trade['id']).order_by(Event.id))]
    timeline=[{'id':s.id,**s.data} for s in db.scalars(select(Timeline).where(Timeline.trade_id==trade['id']).order_by(Timeline.id))]
    instrument_ids={l['instrument_id'] for l in trade['legs']}
    # A preferred source may use a different broker-specific token for the same
    # contract. Export all matching normalized identities, including entry marks.
    identities={contract_identity(l) for l in trade['legs']}
    period_start=min([trade['created_at']]+[l['entry_at'] for l in trade['legs'] if l.get('entry_at')])
    period_quotes=db.scalars(select(QuoteEvent).where(QuoteEvent.received_at>=period_start,QuoteEvent.received_at<=trade['closed_at']).order_by(QuoteEvent.id))
    kept={q.id:{'id':q.id,**q.data} for q in period_quotes if q.instrument_id in instrument_ids or contract_identity(q.data) in identities}
    referenced={l.get('quote_event_id') for l in trade['legs']}
    for e in events:
        for leg in e.get('legs',[]):referenced.add(leg.get('quote_event_id'))
        if e.get('quote_event_id'):referenced.add(e['quote_event_id'])
    for point in timeline:
        for leg in point.get('legs',[]):referenced.add(leg.get('event_id'))
    for q in db.scalars(select(QuoteEvent).where(QuoteEvent.id.in_([i for i in referenced if i is not None]))):
        kept[q.id]={'id':q.id,**q.data}
    quotes=[kept[k] for k in sorted(kept)]
    from .reporting import enqueue_export
    enqueue_export(db,public_trade(trade),events,timeline,quotes)
    trade['export_queued']=True

def new_leg(db,request,broker,settings,at):
    item=request if isinstance(request,dict) else request.model_dump()
    contract=contract_for(item['instrument_id'],broker,settings['demo_mode'])
    if contract.get('exchange')=='MCX' and not settings['demo_mode'] and not contract.get('price_multiplier'):
        raise HTTPException(422,'This MCX master is missing its verified price multiplier. Import the broker monetary multiplier before paper execution.')
    contract_id=contract.get('instrument_id',contract['id'])
    quote=resolve_quote(db,contract,broker,settings)
    if not quote:raise HTTPException(409,f'No quote for {contract["symbol"]}; wait for the feed.')
    pending=False
    if item.get('order_type','MARKET')=='LIMIT':
        if not item.get('price'):raise HTTPException(422,'Set a limit price.')
        compare=fill_reference(quote,item['side'],settings)
        pending=compare>item['price'] if item['side']=='BUY' else compare<item['price']
    if pending:price=float(item['price']); slip=0.0
    else:
        price,quote,slip=execution_price(db,contract,item['side'],broker,settings,
            limit_price=item['price'] if item.get('order_type')=='LIMIT' else None)
    lots=int(item['lots']);quantity=lots*int(contract['lot_size'])*float(contract.get('price_multiplier') or 1)
    leg={k:contract.get(k) for k in ('symbol','underlying','exchange','segment','expiry','strike','option_type','lot_size','price_multiplier','multiplier_verified','token','source')}
    leg.update({'id':ident('leg'),'instrument_id':contract_id,'side':item['side'],'lots':lots,'filled_lots':0 if pending else lots,'open_lots':0 if pending else lots,'order_type':item.get('order_type','MARKET'),'limit_price':item.get('price'),'entry_price':price,'current_price':float(quote['ltp']),'exit_price':None,'entry_at':None if pending else at,'exit_at':None,'realized_pnl':0.0,'unrealized_pnl':0.0,'charges':0.0 if pending else charges(price,quantity,settings),'slippage_cost':0 if pending else slip*quantity,'status':'pending' if pending else 'open','stop_loss':item.get('stop_loss'),'target':item.get('target'),'quote_at':quote.get('received_at'),'quote_event_id':quote.get('event_id'),'quote_source':quote.get('broker',broker),'bid':quote.get('bid'),'ask':quote.get('ask'),'demo':settings['demo_mode']})
    return leg

def idempotent(db,key,payload):
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
    existing=db.get(ActionRecord,key)
    if existing:
        if existing.request_hash!=fingerprint:raise HTTPException(409,'This action key was already used for a different request.')
        return fingerprint,existing.result
    return fingerprint,None

def create_trades(db,payload):
    payload=payload if isinstance(payload,dict) else payload.model_dump()
    if not payload.get('confirmed',False):raise HTTPException(422,'Confirm the paper-order preview first.')
    fingerprint,existing=idempotent(db,payload['idempotency_key'],payload)
    if existing:return existing
    settings=get_settings(db);action_id=ident('action');results=[]
    legs=[l for l in payload['legs'] if l.get('enabled',True)]
    if not legs:raise HTTPException(422,'Enable at least one option leg.')
    for broker in dict.fromkeys(payload['brokers']):
        started=time.perf_counter_ns();at=iso_now()
        try:
            with db.begin_nested():
                newlegs=[new_leg(db,l,broker,settings,at) for l in legs]
                if any(l['underlying']!=payload['underlying'] for l in newlegs):raise HTTPException(422,'The selected contract belongs to a different underlying.')
                trade={'id':ident('trade'),'strategy_id':action_id,'action_id':action_id,'name':payload['name'],'broker':broker,'underlying':payload['underlying'],'status':'open','mode':'PAPER','demo':settings['demo_mode'],'created_at':at,'closed_at':None,'legs':newlegs,'net_premium':sum(-(1 if l['side']=='BUY' else -1)*l['entry_price']*l['filled_lots']*monetary_units(l) for l in newlegs),'spot':catalog.find(legs[0]['instrument_id']).get('spot'),'mfe':None,'mae':None,'mfe_at':None,'mae_at':None,'dispatch_at':at,'processing_ms':(time.perf_counter_ns()-started)/1e6,'accounting_method':'weighted_average','export_queued':False}
                recalculate(trade,at);store_trade(db,trade);event(db,trade,'entry',{'legs':copy.deepcopy(newlegs),'action_id':action_id,'dispatch_at':at,'processing_ms':trade['processing_ms']});snapshot(db,trade,'entry');store_trade(db,trade)
                results.append({'broker':broker,'success':True,'trade_id':trade['id'],'dispatch_at':at,'processing_ms':round(trade['processing_ms'],3),'trade':public_trade(trade,settings)})
        except HTTPException as error:
            results.append({'broker':broker,'success':False,'error':str(error.detail),'dispatch_at':at,'processing_ms':round((time.perf_counter_ns()-started)/1e6,3)})
    result={'action_id':action_id,'mode':'PAPER','results':results,'trades':[r['trade'] for r in results if r['success']]}
    db.add(ActionRecord(key=payload['idempotency_key'],request_hash=fingerprint,result=result))
    db.flush()
    return result

def close_leg(db,trade,leg,lots,settings,manual=None,kind='exit',quote_override=None):
    if lots<1 or lots>leg['open_lots']:raise HTTPException(422,'Exit lots must not exceed open lots.')
    opposite='SELL' if leg['side']=='BUY' else 'BUY'
    price,quote,slip=execution_price(db,leg,opposite,trade['broker'],settings,manual,quote_override=quote_override)
    at=quote['received_at'] if quote_override is not None else iso_now()
    quantity=lots*monetary_units(leg);direction=1 if leg['side']=='BUY' else -1
    leg['realized_pnl']+=direction*(price-leg['entry_price'])*quantity
    previous_closed=leg['filled_lots']-leg['open_lots']
    leg['exit_price']=((leg['exit_price'] or 0)*previous_closed+price*lots)/(previous_closed+lots)
    leg['open_lots']-=lots;leg['current_price']=price;leg['charges']+=charges(price,quantity,settings);leg['slippage_cost']+=slip*quantity
    leg['quote_at']=quote.get('received_at');leg['quote_source']=quote.get('broker',quote.get('source',trade['broker']))
    if not leg['open_lots']:leg['status']='closed';leg['exit_at']=at
    event(db,trade,kind,{'leg_id':leg['id'],'lots':lots,'price':price,'fill_source':leg['quote_source'],'quote_event_id':quote.get('event_id'),'trigger_quote':copy.deepcopy(quote),'processed_at':iso_now(),'accounting_method':'weighted_average'},at)

def apply_action(db,trade_id,payload):
    payload=payload if isinstance(payload,dict) else payload.model_dump()
    if not payload.get('confirmed'):raise HTTPException(422,'Confirm this paper action first.')
    fingerprint,existing=idempotent(db,payload['idempotency_key'],{'trade_id':trade_id,**payload})
    if existing:return existing
    row=db.get(Trade,trade_id)
    if not row:raise HTTPException(404,'Trade not found.')
    trade=copy.deepcopy(row.data);settings=get_settings(db);action=payload['action'];at=iso_now()
    if action=='duplicate':
        new_payload={'name':f'{trade["name"]} copy'[:100],'underlying':trade['underlying'],'brokers':[trade['broker']],'legs':[{'instrument_id':l['instrument_id'],'side':l['side'],'lots':l['open_lots'] or l['lots'],'order_type':'MARKET','enabled':True} for l in trade['legs']],'idempotency_key':'duplicate_'+hashlib.sha256(payload['idempotency_key'].encode()).hexdigest(),'confirmed':True}
        result=create_trades(db,new_payload)
    else:
        if trade['status'] not in ('open','pending'):raise HTTPException(409,'This trade has finished. Duplicate it to open a new paper trade.')
        if bool(trade['demo'])!=bool(settings['demo_mode']):raise HTTPException(409,'Switch to this trade’s demo/market-data mode before modifying it.')
        leg=next((l for l in trade['legs'] if l['id']==payload.get('leg_id')),None)
        if action in ('add_lots','partial_exit','exit_leg','edit') and payload.get('leg_id') and not leg:raise HTTPException(404,'Leg not found.')
        if action=='add_leg':
            if not payload.get('leg'):raise HTTPException(422,'Select a leg to add.')
            new=new_leg(db,payload['leg'],trade['broker'],settings,at)
            if new['underlying']!=trade['underlying']:raise HTTPException(422,'A leg must use the strategy underlying.')
            trade['legs'].append(new);trade['net_premium']+=-(1 if new['side']=='BUY' else -1)*new['entry_price']*new['filled_lots']*monetary_units(new);event(db,trade,'added_leg',{'leg':new})
        elif action=='add_lots':
            if not leg or leg['status']!='open':raise HTTPException(422,'Select an open leg.')
            lots=payload.get('lots')
            if not lots:raise HTTPException(422,'Choose lots to add.')
            price,quote,slip=execution_price(db,leg,leg['side'],trade['broker'],settings,payload.get('price'))
            leg['entry_price']=(leg['entry_price']*leg['open_lots']+price*lots)/(leg['open_lots']+lots)
            leg['lots']+=lots;leg['filled_lots']+=lots;leg['open_lots']+=lots;leg['charges']+=charges(price,lots*monetary_units(leg),settings);leg['slippage_cost']+=slip*lots*monetary_units(leg);leg['current_price']=quote['ltp'];leg['quote_at']=quote.get('received_at');trade['net_premium']+=-(1 if leg['side']=='BUY' else -1)*price*lots*monetary_units(leg);event(db,trade,'added_lots',{'leg_id':leg['id'],'lots':lots,'price':price,'average_entry_price':leg['entry_price'],'quote_event_id':quote.get('event_id')})
        elif action in ('partial_exit','exit_leg'):
            if not leg:raise HTTPException(422,'Select a leg.')
            close_leg(db,trade,leg,payload.get('lots') or 0 if action=='partial_exit' else leg['open_lots'],settings,payload.get('price'),action)
        elif action=='exit_strategy':
            # Check all quotes before any simulated fill; this action is atomic.
            for l in trade['legs']:
                if l['open_lots']:execution_price(db,l,'SELL' if l['side']=='BUY' else 'BUY',trade['broker'],settings,payload.get('price'))
            for l in trade['legs']:
                if l['open_lots']:close_leg(db,trade,l,l['open_lots'],settings,payload.get('price'),'full_exit')
                if l['status']=='pending':l['status']='cancelled';event(db,trade,'cancel_pending',{'leg_id':l['id']})
        elif action=='cancel_pending':
            changed=False
            for l in trade['legs']:
                if l['status']=='pending' and (not payload.get('leg_id') or l['id']==payload['leg_id']):l['status']='cancelled';changed=True;event(db,trade,'cancel_pending',{'leg_id':l['id']})
            if not changed:raise HTTPException(409,'No pending simulated order to cancel.')
        elif action=='edit':
            before=copy.deepcopy(leg if leg else {'name':trade['name']})
            if payload.get('name'):trade['name']=payload['name']
            if leg:
                for key in ('stop_loss','target'):
                    if payload.get(key) is not None:leg[key]=payload[key]
                if payload.get('price') is not None:
                    if leg['status']!='open':raise HTTPException(422,'Only the open quantity can have its average entry assumption edited.')
                    trade['net_premium']+=-(1 if leg['side']=='BUY' else -1)*(payload['price']-leg['entry_price'])*leg['open_lots']*monetary_units(leg)
                    leg['entry_price']=payload['price']
            event(db,trade,'paper_assumption_edited',{'before':before,'changes':{k:v for k,v in payload.items() if v is not None and k not in ('idempotency_key','confirmed')}})
        recalculate(trade,at);snapshot(db,trade,'action');store_trade(db,trade);archive_close(db,trade);store_trade(db,trade)
        result={'trade':public_trade(trade,settings),'action':action,'mode':'PAPER'}
    db.add(ActionRecord(key=payload['idempotency_key'],request_hash=fingerprint,result=result))
    db.flush();return result

def record_quote(db,quote,process=True):
    quote=copy.deepcopy(quote)
    if quote.get('broker') not in BROKERS or not quote.get('instrument_id'):raise ValueError('Quote requires known broker and instrument_id.')
    if not math.isfinite(float(quote.get('ltp',-1))) or float(quote.get('ltp',-1))<0:raise ValueError('Invalid quote price.')
    quote.setdefault('received_at',iso_now());quote.setdefault('receive_ns',time.time_ns());quote.setdefault('demo',False)
    contract=catalog.find(quote['instrument_id']) if catalog else None
    if contract:
        for key in ('underlying','exchange','segment','symbol','expiry','strike','option_type'):quote.setdefault(key,contract.get(key))
    settings=get_settings(db);compact=settings['logging_mode']=='summary'
    interested=not compact or any(_trade_uses_quote(trade,quote,settings) for trade in trade_records(db))
    if not interested:
        cache_quote(db,quote,persist=False)
        return None
    # Compact receipts are removed after success; a counter prevents SQLite
    # from reusing deleted row IDs and accidentally skipping a later mark.
    counter=db.get(QuoteReceiptCounter,1)
    if counter is None:
        db.add(QuoteReceiptCounter(id=1,value=db.scalar(select(func.max(QuoteEvent.id))) or 0));db.flush()
    next_id=db.execute(update(QuoteReceiptCounter).where(QuoteReceiptCounter.id==1)
                       .values(value=QuoteReceiptCounter.value+1).returning(QuoteReceiptCounter.value)).scalar_one()
    entry=QuoteEvent(id=next_id,broker=quote['broker'],instrument_id=quote['instrument_id'],received_at=quote['received_at'],data=quote)
    db.add(entry);db.flush()
    db.add(PendingQuote(quote_id=entry.id));db.flush()
    if compact:db.add(CompactQuoteReceipt(quote_id=entry.id))
    cache_quote(db,{**quote,'event_id':entry.id},persist=True)
    if process:revalue_quote(db,entry)
    return entry.id

def _trade_uses_quote(trade,quote,settings):
    if trade['status'] not in ('open','pending') or bool(trade.get('demo'))!=bool(quote['demo']):return False
    source=settings['preferred_feed'];source=trade['broker'] if source=='broker' else source
    return source==quote['broker'] and any(leg['status'] in ('open','pending') and
        (leg['instrument_id']==quote['instrument_id'] or contract_identity(leg)==contract_identity(quote))
        for leg in trade['legs'])


def revalue_quote(db,entry):
    if entry is None:return None
    quote={**entry.data,'event_id':entry.id}
    settings=get_settings(db)
    failed=False
    for trade in trade_records(db):
        if not _trade_uses_quote(trade,quote,settings):continue
        if trade.get('_last_quote_event_id',0)>=entry.id:continue
        changed=False
        try:
            # A malformed valuation or export cannot roll back the receipt.
            with db.begin_nested():
                fresh=(age_seconds(quote['received_at']) or 0)<=settings['stale_after_seconds']
                for leg in trade['legs']:
                    if leg['status'] not in ('open','pending'):continue
                    matches=leg['instrument_id']==quote['instrument_id'] or contract_identity(leg)==contract_identity(quote)
                    if not matches:continue
                    changed=True;leg['current_price']=float(quote['ltp']);leg['quote_at']=quote['received_at'];leg['quote_event_id']=entry.id;leg['quote_source']=quote['broker'];leg['bid']=quote.get('bid');leg['ask']=quote.get('ask')
                    if leg['status']=='pending' and fresh:
                        price=fill_reference(quote,leg['side'],settings)
                        crossed=price<=leg['limit_price'] if leg['side']=='BUY' else price>=leg['limit_price']
                        if crossed:
                            fill,_,slip=execution_price(db,leg,leg['side'],trade['broker'],settings,quote_override=quote,limit_price=leg['limit_price'])
                            leg['entry_price']=fill;leg['entry_at']=quote['received_at'];leg['open_lots']=leg['lots'];leg['filled_lots']=leg['lots'];leg['status']='open';leg['charges']+=charges(fill,leg['lots']*monetary_units(leg),settings);leg['slippage_cost']+=slip*leg['lots']*monetary_units(leg);trade['net_premium']+=-(1 if leg['side']=='BUY' else -1)*fill*leg['filled_lots']*monetary_units(leg);event(db,trade,'limit_filled',{'leg_id':leg['id'],'price':fill,'quote_event_id':entry.id,'processed_at':iso_now()},quote['received_at'])
                    if leg['status']=='open' and fresh:
                        stop=leg.get('stop_loss');target=leg.get('target');p=quote['ltp'];buy=leg['side']=='BUY'
                        stop_hit=stop is not None and (p<=stop if buy else p>=stop)
                        target_hit=target is not None and (p>=target if buy else p<=target)
                        if stop_hit or target_hit:
                            # Observe the incoming mark before the simulated
                            # close applies bid/ask, slippage and exit charges.
                            recalculate(trade,quote['received_at'])
                            close_leg(db,trade,leg,leg['open_lots'],settings,kind='stop_loss' if stop_hit else 'target',quote_override=quote)
                if changed:
                    recalculate(trade,quote['received_at']);trade['_last_quote_event_id']=entry.id;trade.pop('valuation_error',None)
                    if settings['logging_mode']=='every_update':snapshot(db,trade,'market_update')
                    if trade['status']=='closed':snapshot(db,trade,'closed');store_trade(db,trade);archive_close(db,trade)
                    store_trade(db,trade)
        except Exception as error:
            failed=True
            log.exception('Valuation failed for trade %s at received event %s; raw receipt retained',trade['id'],entry.id)
            retained=copy.deepcopy(db.get(Trade,trade['id']).data);retained['valuation_error']=f'{type(error).__name__}: price event {entry.id} retained; review trade journal'
            store_trade(db,retained)
            event(db,retained,'valuation_error',{'quote_event_id':entry.id,'error_type':type(error).__name__})
    db.execute(delete(PendingQuote).where(PendingQuote.quote_id==entry.id))
    compact_receipt=db.get(CompactQuoteReceipt,entry.id)
    if compact_receipt and not failed:
        db.delete(compact_receipt);db.delete(entry)
    return entry.id

def recover_pending_quotes(db,limit=100):
    receipts=db.scalars(select(QuoteEvent).join(PendingQuote,PendingQuote.quote_id==QuoteEvent.id).order_by(QuoteEvent.id).limit(limit)).all()
    for receipt in receipts:revalue_quote(db,receipt)
    return len(receipts)

def take_snapshots(db):
    if get_settings(db)['logging_mode']=='summary':return
    for row in db.scalars(select(Trade).where(Trade.status.in_(['open','pending']))):
        trade=copy.deepcopy(row.data);snapshot(db,trade,'one_second');store_trade(db,trade)

def list_trades(db,demo=None):
    settings=get_settings(db)
    demo=settings['demo_mode'] if demo is None else demo
    return [public_trade(t,settings) for t in trade_records(db) if bool(t.get('demo'))==bool(demo)][::-1]

def _dashboard_quotes(db,settings):
    state=_cache(db)
    if not state['quotes_loaded']:
        # SQL selects latest IDs on the server; historical quote JSON never
        # crosses the network merely to find the current market watch.
        for row in db.scalars(select(LatestQuote).order_by(LatestQuote.received_at.desc()).limit(30)):
            cache_quote(db,row.data,persist=False)
        if not state['quotes']:
            newest=select(func.max(QuoteEvent.id).label('id')).group_by(QuoteEvent.broker,QuoteEvent.instrument_id).order_by(func.max(QuoteEvent.id).desc()).limit(30).subquery()
            for row in db.scalars(select(QuoteEvent).join(newest,QuoteEvent.id==newest.c.id)):
                cache_quote(db,{**row.data,'event_id':row.id},persist=False)
        state['quotes_loaded']=True
    with _cache_lock:values=list(state['quotes'].values())
    values=sorted((q for q in values if bool(q.get('demo'))==bool(settings['demo_mode'])),key=lambda q:q.get('received_at',''),reverse=True)[:30]
    quotes=[]
    for value in values:
        age=age_seconds(value.get('received_at'))
        quotes.append({**copy.deepcopy(value),'feed_age':round(age,3) if age is not None else None,
                       'stale':age is None or age>settings['stale_after_seconds']})
    return quotes


def dashboard(db,statuses=None):
    settings=get_settings(db);trades=list_trades(db);open_trades=[t for t in trades if t['status'] in ('open','pending')]
    local_now=datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Kolkata'))
    midnight=local_now.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat(timespec='microseconds')
    today_pnl=0.0;daily_complete=True;state=_cache(db)
    for trade in trades:
        if trade.get('closed_at') and trade['closed_at']<midnight:continue
        if trade['created_at']>=midnight:baseline=0.0
        else:
            mark=trade.get('_daily_pnl',{})
            if settings['logging_mode']=='summary' and mark.get('day')==local_now.date().isoformat() and mark.get('complete'):
                baseline=mark['baseline']
            else:
                cache_key=(trade['id'],midnight)
                if cache_key not in state['baselines']:
                    prior=db.scalars(select(Timeline).where(Timeline.trade_id==trade['id'],Timeline.timestamp<midnight).order_by(Timeline.timestamp.desc()).limit(1)).first()
                    state['baselines'][cache_key]=prior.data['net_pnl'] if prior else None
                prior_value=state['baselines'][cache_key]
                baseline=prior_value if prior_value is not None else 0.0
                if prior_value is None:daily_complete=False
        today_pnl+=trade['net_pnl']-baseline
    summary={'net_pnl':round(sum(t['net_pnl'] for t in trades),2),'total_pnl':round(sum(t['net_pnl'] for t in trades),2),'realized_pnl':round(sum(t['realized_pnl'] for t in trades),2),'unrealized_pnl':round(sum(t['unrealized_pnl'] for t in trades),2),'today_pnl':round(today_pnl,2),'today_pnl_complete':daily_complete,'charges':round(sum(t['charges'] for t in trades),2),'active_trades':len(open_trades),'active_positions':sum(sum(l['open_lots']>0 for l in t['legs']) for t in trades),'margin_estimate':round(sum(t.get('margin_estimate',0) for t in open_trades),2),'paper_capital':settings['paper_capital'],'available_paper_capital':settings['paper_capital']-sum(t.get('margin_estimate',0) for t in open_trades)}
    brokers=[]
    for broker,name in BROKERS.items():
        items=[t for t in trades if t['broker']==broker];status=(statuses or {}).get(broker,{})
        brokers.append({'id':broker,'name':name,'net_pnl':round(sum(t['net_pnl'] for t in items),2),'realized_pnl':round(sum(t['realized_pnl'] for t in items),2),'unrealized_pnl':round(sum(t['unrealized_pnl'] for t in items),2),'active_trades':sum(t['status']=='open' for t in items),'margin_estimate':round(sum(t.get('margin_estimate',0) for t in items if t['status']=='open'),2),'available_broker_margin':None,'status':'demo' if settings['demo_mode'] else status.get('status','disconnected'),'feed':status,'demo':settings['demo_mode']})
    quotes=_dashboard_quotes(db,settings)
    if settings['logging_mode']=='summary':
        # A small live chart lives in memory only. Recorded trade extrema and
        # final results survive restarts; each second of chart data does not.
        portfolio=state['portfolio']
        if not portfolio or portfolio[-1].get('demo')!=settings['demo_mode']:
            portfolio.clear()
        if not portfolio or portfolio[-1]['net_pnl']!=summary['net_pnl']:
            portfolio.append({'timestamp':iso_now(),'net_pnl':summary['net_pnl'],'demo':settings['demo_mode']})
            del portfolio[:-250]
        return {'summary':summary,'brokers':brokers,'trades':trades,'quotes':quotes,'timeline':[],
                'portfolio_timeline':copy.deepcopy(portfolio),'portfolio_timeline_complete':False,
                'chart_notice':'Live session chart; Excel retains trade summaries and observed extrema.',
                'feed_status':statuses or {},'mode':'PAPER','demo':settings['demo_mode'],
                'recording_mode':'summary','server_time':iso_now()}
    trade_ids=[t['id'] for t in trades]
    history=db.scalars(select(Timeline).where(Timeline.trade_id.in_(trade_ids)).order_by(Timeline.timestamp.desc(),Timeline.id.desc()).limit(250)).all() if trade_ids else []
    points=[{'timestamp':p.timestamp,'trade_id':p.trade_id,**p.data} for p in reversed(history)]
    portfolio=[];portfolio_complete=True
    if points:
        start=points[0]['timestamp'];values={}
        for t in trades:
            prior=db.scalars(select(Timeline).where(Timeline.trade_id==t['id'],Timeline.timestamp<start).order_by(Timeline.timestamp.desc(),Timeline.id.desc()).limit(1)).first()
            if prior:values[t['id']]=prior.data['net_pnl']
            elif t.get('closed_at') and t['closed_at']<start:values[t['id']]=t['net_pnl']
            else:
                values[t['id']]=0.0
                if t['created_at']<start:portfolio_complete=False
        for point in points:
            values[point['trade_id']]=point['net_pnl']
            aggregate={'timestamp':point['timestamp'],'net_pnl':round(sum(values.values()),2)}
            if portfolio and portfolio[-1]['timestamp'][:19]==point['timestamp'][:19]:portfolio[-1]=aggregate
            else:portfolio.append(aggregate)
    return {'summary':summary,'brokers':brokers,'trades':trades,'quotes':quotes,'timeline':points,'portfolio_timeline':portfolio,'portfolio_timeline_complete':portfolio_complete,'feed_status':statuses or {},'mode':'PAPER','demo':settings['demo_mode'],'server_time':iso_now()}
