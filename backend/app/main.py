"""FastAPI application. Run one worker so a single process owns broker feeds."""
import asyncio
import copy
import logging
import math
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from typing import Literal
from fastapi import FastAPI, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from .database import (Base, engine as db_engine, SessionLocal, get_db, iso_now,
                       is_database_unavailable, is_sqlite_contention,
                       run_transaction, sqlite_error_code)
from .models import Owner, UserSession, SocketTicket, Trade, Event, Timeline, QuoteEvent, Setting, SchemaVersion, ActionRecord, PnlSnapshot, RecordingGap
from .security import require_session, create_session, password_hash, verify_password, verify_totp, digest, rate_limit, validate_origin, session_from_cookie, COOKIE, ALLOWED_ORIGINS
from .schemas import AuthInput, PasswordInput, TradeInput, ActionInput, SettingsInput, RiskInput
from . import engine
from .payoff import strategy_risk
from .scheduler import CollectorLease, ExchangeCalendar, guard_lease
from . import cloud_backup, resource_health, maintenance, reporting, daily_reports, sheets_sync, archival
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from . import broker_auth, broker_setup

from .log_safety import install as install_safe_logs
install_safe_logs()
log=logging.getLogger('personal_options_trader')
write_lock=asyncio.Lock()
stop_event=asyncio.Event()
feed_manager=None
feed_status={}
catalog=None
_feed_contract_ids=None
_feed_refresh_lock=asyncio.Lock()
_quote_selection_lock=Lock()
_quote_selection_requests={}
database_ready=True
DATABASE_RETRY_SECONDS=60
collector_lease=None
exchange_calendar=None
cloud_service=None
resource_monitor=None
daily_service=None
sheets_service=None
storage_archiver=None
kotak_setup=None
_active_positions=[]
_activity_until=0.0
_viewers=0
_started_at=time.monotonic()
_last_database_write=None
_recording_status={}
_runtime_settings={}

def touch_activity():
    global _activity_until
    _activity_until=time.monotonic()+30

def update_demand(db):
    global _active_positions,_runtime_settings
    _active_positions=[t for t in engine.trade_records(db) if t['status'] in ('open','pending') and t.get('broker') in engine.BROKERS]
    _runtime_settings=engine.get_settings(db)

def recording_needed():
    now=datetime.now(timezone.utc)
    for trade in _active_positions:
        if trade.get('demo'):
            if _runtime_settings.get('demo_mode',False):return True
            continue
        # Genuine paper positions keep recording through their own market
        # sessions, independently of whether the UI is showing sample trades.
        if exchange_calendar and any(exchange_calendar.state(leg,now)['open'] for leg in trade.get('legs',[]) if leg.get('status') in ('open','pending')):return True
    return False

async def ensure_writer(db):
    # SQLite remains the existing one-process laptop workspace. PostgreSQL
    # deployments fence mutations as well as capture during a rolling handover.
    if collector_lease and db.get_bind().dialect.name=='postgresql':
        if collector_lease.token is None and not await asyncio.to_thread(collector_lease.renew):
            raise HTTPException(503,'The backend is completing a collector handover. Retry this action shortly.')
        if not await asyncio.to_thread(guard_lease,db,collector_lease.token):
            raise HTTPException(503,'Collector ownership changed. Retry this action shortly.')

def database_unavailable():
    global database_ready
    database_ready=False

def log_database_error(operation,error):
    # SQLAlchemy exception strings contain SQL and bound values, including
    # account/session data. Only diagnostic types and numeric SQLite codes belong
    # in this log; the original exception is deliberately not interpolated.
    log.warning('%s failed (error_type=%s, sqlite_code=%s, database_unavailable=%s).',
                operation,type(error).__name__,sqlite_error_code(error),is_database_unavailable(error))

async def wait_for_shutdown(seconds):
    try:await asyncio.wait_for(stop_event.wait(),timeout=seconds)
    except asyncio.TimeoutError:pass

def requested_quote_selection_ids():
    now=time.monotonic()
    with _quote_selection_lock:
        for key in list(_quote_selection_requests):
            if _quote_selection_requests[key][0]<=now:_quote_selection_requests.pop(key,None)
        return set().union(*(row[1] for row in _quote_selection_requests.values())) if _quote_selection_requests else set()


def selected_feed_contracts(db):
    wanted={s.strip() for s in os.getenv('FEED_INSTRUMENT_IDS','').split(',') if s.strip()}
    wanted.update(option_chain.requested_contract_ids())
    wanted.update(requested_quote_selection_ids())
    for row in db.scalars(select(Trade).where(Trade.status.in_(['open','pending']))):
        if row.data.get('demo') is False:
            wanted.update(l['instrument_id'] for l in row.data.get('legs',[]) if l.get('status') in {'open','pending'})
    return catalog.select_live(wanted,engine.BROKERS)

async def refresh_selected_feeds(db,force=False):
    global _feed_contract_ids
    if feed_manager is None:return
    # Every caller commits its paper mutation before reconfiguring sockets.
    # Release any subsequent read snapshot before waiting for another refresh:
    # rollback-journal commits must never wait for a socket-owning reader.
    await asyncio.to_thread(option_chain.finish_read_transaction,db)
    binding=db.get_bind()
    def selected():
        with Session(binding,expire_on_commit=False) as reader:
            return selected_feed_contracts(reader)
    async with _feed_refresh_lock:
        if collector_lease and collector_lease.token is None:return
        contracts=await asyncio.to_thread(selected)
        if exchange_calendar:
            now=datetime.now(timezone.utc)
            contracts=[c for c in contracts if exchange_calendar.state(c,now)['open']]
        identities=frozenset((c['id'],c['broker']) for c in contracts)
        if force or identities!=_feed_contract_ids:
            if force:
                # A saved credential change needs a new authenticated socket.
                await feed_manager.stop()
                await feed_manager.start(contracts)
            else:
                await feed_manager.reconfigure(contracts)
            _feed_contract_ids=identities

def demo_quote(contract,broker,now=None):
    now=now or time.time();base=float(contract.get('base_price') or 100)
    # Synthetic practice prices only; never used for broker-master contracts.
    phase=sum(ord(c) for c in contract['id'])/17
    wave=math.sin(now/28+phase)*.04+math.sin(now/7+phase)*.009
    ltp=round(max(.05,base*(1+wave)),2)
    return {**{k:contract.get(k) for k in ('underlying','exchange','segment','symbol','expiry','strike','option_type')},'instrument_id':contract.get('instrument_id',contract['id']),'broker':broker,'ltp':ltp,'bid':round(max(.05,ltp-.15),2),'ask':round(ltp+.15,2),'received_at':iso_now(),'exchange_timestamp':None,'sequence':f'demo-{time.time_ns()}','demo':True,'source':'synthetic_practice_feed','volume':None,'open_interest':None}

def refresh_demo_for_payload(db,payload,broker=None):
    settings=engine.get_settings(db)
    if not settings['demo_mode']:return
    selected=payload.get('brokers') or ([broker] if broker else list(engine.BROKERS))
    ids=[l['instrument_id'] for l in payload.get('legs',[]) if l.get('enabled',True)]
    if payload.get('leg'):ids.append(payload['leg']['instrument_id'])
    if payload.get('trade_id'):
        row=db.get(Trade,payload['trade_id'])
        if row:ids.extend(l['instrument_id'] for l in row.data['legs'])
    for b in selected:
        for instrument_id in set(ids):
            contract=engine.contract_for(instrument_id,b,True)
            engine.record_quote(db,demo_quote(contract,b))

def seed_demo(db):
    if db.scalars(select(Trade).limit(1)).first() or not engine.get_settings(db)['demo_mode']:return
    all_contracts=catalog.all(demo=True)
    # Initial marks are synthetic catalog prices, clearly attributed in every row.
    for c in all_contracts:
        engine.record_quote(db,demo_quote(c,c.get('broker','kotak')))
    def contracts(underlying,broker='kotak',kind='CE'):
        rows=[c for c in all_contracts if c['underlying']==underlying and c.get('broker')==broker and c['option_type']==kind]
        expiries=sorted({c['expiry'] for c in rows})
        return sorted([c for c in rows if c['expiry']==expiries[0]],key=lambda c:c['strike']) if expiries else []
    underlyings=list(dict.fromkeys(c['underlying'] for c in all_contracts))
    nifty=next((u for u in underlyings if u.upper().replace(' ','') in ('NIFTY','NIFTY50')),underlyings[0] if underlyings else None)
    gold=next((u for u in underlyings if 'GOLD' in u.upper()),nifty)
    configurations=[('NIFTY bull call spread',nifty,'kotak','spread'),('NIFTY iron condor',nifty,'kotak','condor'),('Gold protective spread',gold,'kotak','spread'),('Completed practice trade',nifty,'kotak','closed')]
    for index,(name,underlying,broker,kind) in enumerate(configurations):
        calls=contracts(underlying,broker,'CE');puts=contracts(underlying,broker,'PE')
        if len(calls)<4:continue
        mid=len(calls)//2
        if kind=='condor' and len(puts)>4:
            chosen=[(puts[max(0,mid-2)],'BUY'),(puts[max(0,mid-1)],'SELL'),(calls[min(len(calls)-1,mid+1)],'SELL'),(calls[min(len(calls)-1,mid+2)],'BUY')]
        else:chosen=[(calls[mid],'BUY'),(calls[mid+1],'SELL')] if kind!='closed' else [(calls[mid],'BUY')]
        payload={'name':name,'underlying':underlying,'brokers':[broker],'legs':[{'instrument_id':c['id'],'side':side,'lots':2 if kind=='spread' else 1,'order_type':'MARKET','enabled':True} for c,side in chosen],'idempotency_key':f'demo-seed-{index}','confirmed':True}
        result=engine.create_trades(db,payload)
        if not result['trades']:continue
        row=db.get(Trade,result['trades'][0]['id']);trade=copy.deepcopy(row.data)
        # Recorded sample assumptions create a useful practice portfolio, not market history.
        for leg in trade['legs']:
            leg['entry_price']=round(leg['entry_price']*(.94 if leg['side']=='BUY' else 1.03),2)
        engine.event(db,trade,'demo_sample_assumptions',{'note':'Synthetic sample portfolio; entry assumptions are educational, not historical exchange fills.'})
        engine.recalculate(trade);engine.snapshot(db,trade,'sample_seed');engine.store_trade(db,trade)
        if kind=='closed':
            started=datetime.now(timezone.utc)-timedelta(hours=4)
            trade['created_at']=started.isoformat(timespec='microseconds')
            for offset,pnl in ((15,-1200),(61,-1900),(193,2000),(219,1300)):
                timestamp=(started+timedelta(minutes=offset)).isoformat(timespec='microseconds')
                db.add(Timeline(trade_id=trade['id'],timestamp=timestamp,data={'timestamp':timestamp,'kind':'sample_history','net_pnl':pnl,'gross_pnl':pnl+trade['charges'],'realized_pnl':0,'unrealized_pnl':pnl+trade['charges'],'charges':trade['charges'],'carried_forward':False,'demo':True,'legs':[]}))
            leg=trade['legs'][0]
            desired=1400;quantity=leg['open_lots']*leg['lot_size'];settings=engine.get_settings(db)
            pct=settings['charges_percent']/100
            exit_price=(desired+leg['entry_price']*quantity+leg['charges']+settings['brokerage_per_order'])/(quantity*(1-pct))
            engine.close_leg(db,trade,leg,leg['open_lots'],settings,manual=exit_price,kind='demo_sample_close')
            engine.recalculate(trade);trade['mfe']=2000;trade['mae']=-1900;trade['mfe_at']=(started+timedelta(minutes=193)).isoformat(timespec='microseconds');trade['mae_at']=(started+timedelta(minutes=61)).isoformat(timespec='microseconds');trade['profit_giveback']=2000-trade['net_pnl'];engine.snapshot(db,trade,'closed');engine.store_trade(db,trade);engine.archive_close(db,trade);engine.store_trade(db,trade)
    db.commit()

def process_quote_receipt(quote):
    receipt_id=run_transaction(lambda db:engine.record_quote(db,quote,process=False),SessionLocal)
    # Durable receipt precedes simulation/Excel processing. A valuation retry
    # must replay this receipt, not receive the same quote a second time.
    if receipt_id is not None:
        run_transaction(lambda db:engine.revalue_quote(db,db.get(QuoteEvent,receipt_id)),SessionLocal)

async def quote_received(quote):
    # A high-frequency feed coalesces in bounded memory. Durable one-second
    # samples retain the source quote's timestamp; raw ticks are not DB writes.
    if collector_lease and collector_lease.token is None:return
    if _runtime_settings.get('logging_mode','full_history')!='every_update':
        engine.buffer_quote(quote)
        return
    try:
        async with write_lock:
            await asyncio.to_thread(process_quote_receipt,quote)
    except SQLAlchemyError as error:
        log_database_error('Quote processing',error)
        if is_database_unavailable(error):database_unavailable()
        raise

async def status_received(broker,status):
    feed_status[broker]=status

def capture_once(lease=None):
    global _last_database_write,_recording_status
    def capture(db):
        if lease is not None and not guard_lease(db,lease):return {'lease_lost':True}
        engine.recover_pending_quotes(db)
        engine.flush_buffered_quotes(db)
        settings=engine.get_settings(db)
        if settings['demo_mode']:
            needed=set()
            for trade in engine.trade_records(db):
                if trade['status'] in ('open','pending') and trade.get('demo'):
                    source=trade['broker']
                    needed.update((source,l['instrument_id']) for l in trade['legs'] if l['status'] in ('open','pending'))
            for broker,instrument_id in needed:
                contract=engine.contract_for(instrument_id,broker,True)
                engine.record_quote(db,demo_quote(contract,broker))
        result=engine.take_snapshots(db,calendar=exchange_calendar,lease=lease)
        if result.get('recorded'):
            day=datetime.fromtimestamp(result.get('second',int(time.time())),timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Kolkata')).date().isoformat()
            daily_reports.ensure_day(db,day)
        update_demand(db)
        return result
    result=run_transaction(capture,SessionLocal)
    _last_database_write=iso_now();_recording_status=result or {}
    if result and result.get('recorded') and sheets_service:sheets_service.notify_work()
    return result

async def background_loop():
    global _feed_contract_ids
    next_tick=time.monotonic();renewed=0.0;refreshed=0.0
    while not stop_event.is_set() and database_ready:
        try:
            async with write_lock:
                active=recording_needed()
                demand=active or _viewers>0 or time.monotonic()<_activity_until
                if collector_lease and not demand:
                    if collector_lease.token:
                        await feed_manager.stop();_feed_contract_ids=None
                        await asyncio.to_thread(collector_lease.release)
                elif collector_lease:
                    now=time.monotonic()
                    if now-renewed>=5 or collector_lease.token is None:
                        previous=collector_lease.token
                        owned=await asyncio.to_thread(collector_lease.renew);renewed=now
                        if not owned:
                            await feed_manager.stop();_feed_contract_ids=None
                        elif previous!=collector_lease.token:
                            await asyncio.to_thread(restore_active_state)
                            refreshed=0
                    if collector_lease.token:
                        if now-refreshed>=5:
                            with SessionLocal() as db:await refresh_selected_feeds(db)
                            refreshed=now
                        if active or _viewers or engine.buffered_quote_status().get('pending',0):
                            result=await asyncio.to_thread(capture_once,collector_lease.token)
                            if result.get('lease_lost'):
                                await feed_manager.stop();collector_lease.token=None
                else:
                    await asyncio.to_thread(capture_once)
        except SQLAlchemyError as error:
            log_database_error('Database capture',error)
            if is_database_unavailable(error):
                database_unavailable()
                return
            # A short local write conflict or one invalid statement must not
            # turn a healthy workspace into a minute-long connection outage.
        except Exception as error:
            log.warning('Background capture interrupted (error_type=%s); records retained.',type(error).__name__)
        next_tick+=1
        if next_tick<time.monotonic():next_tick=math.ceil(time.monotonic())
        await wait_for_shutdown(max(.01,next_tick-time.monotonic()))

def restore_active_state():
    with SessionLocal() as db:
        active=[row.data for row in db.scalars(select(Trade).where(Trade.status.in_(['open','pending'])))]
        catalog.restore_demo_contracts(active);catalog.restore_live_contracts(active)
        update_demand(db)

def initialize_database():
    """Run blocking database setup outside the HTTP event loop."""
    maintenance.migrate(db_engine)
    # Restore encrypted broker settings before collectors start. No network calls.
    kotak_setup.restore()
    with SessionLocal() as db:
        if not db.get(SchemaVersion,1):db.add(SchemaVersion(version=1));db.commit()
        persisted_trades=[row.data for row in db.scalars(select(Trade))]
        catalog.restore_demo_contracts(persisted_trades)
        catalog.restore_live_contracts(persisted_trades)
        while engine.recover_pending_quotes(db):db.commit()
        if os.getenv('ENVIRONMENT')=='production' and not db.get(Setting,'app'):
            engine.update_settings(db,{'demo_mode':False});db.commit()
        seed_demo(db)
        update_demand(db)

async def try_initialize_database():
    global database_ready
    try:
        await asyncio.to_thread(initialize_database)
    except SQLAlchemyError as exc:
        if is_database_unavailable(exc):database_unavailable()
        log_database_error('Database initialization',exc)
        return False
    database_ready=True
    return True

async def database_services(reporting_loop):
    """Pause all SQL workers during an outage instead of continuously retrying."""
    workers=[]
    try:
        while not stop_event.is_set():
            if not database_ready:
                for task in workers:task.cancel()
                if workers:await asyncio.gather(*workers,return_exceptions=True)
                workers=[]
                await feed_manager.stop()
                if collector_lease:collector_lease.token=None
                await wait_for_shutdown(DATABASE_RETRY_SECONDS)
                if stop_event.is_set():break
                if not await try_initialize_database():continue
            if not workers:
                workers=[asyncio.create_task(background_loop()),asyncio.create_task(reporting_loop(stop_event))]
            await wait_for_shutdown(5)
    finally:
        for task in workers:task.cancel()
        if workers:await asyncio.gather(*workers,return_exceptions=True)

@asynccontextmanager
async def lifespan(app):
    global catalog,feed_manager,stop_event,database_ready,collector_lease,exchange_calendar,cloud_service,resource_monitor,daily_service,sheets_service,storage_archiver,_started_at,_viewers,_activity_until,kotak_setup,_feed_refresh_lock
    from .instruments import ContractCatalog
    from .reporting import reporting_loop
    from . import reporting
    from .brokers import FeedManager
    stop_event=asyncio.Event();_feed_refresh_lock=asyncio.Lock();database_ready=False;_viewers=0;_activity_until=0;_started_at=time.monotonic()
    with _quote_selection_lock:_quote_selection_requests.clear()
    kotak_setup=broker_setup.KotakSetupService(factory=SessionLocal)
    catalog=ContractCatalog();engine.set_catalog(catalog)
    exchange_calendar=ExchangeCalendar();collector_lease=CollectorLease(SessionLocal)
    cloud_service=cloud_backup.CloudBackupService(session_factory=SessionLocal)
    resource_monitor=resource_health.ResourceMonitor()
    sheets_service=sheets_sync.SheetsSyncService(SessionLocal,cloud_service)
    daily_service=daily_reports.DailyReportService(SessionLocal,calendar=exchange_calendar,on_finalized=sheets_service.notify_work,can_work=lambda:not recording_needed())
    storage_archiver=archival.AutomaticArchiver(cloud_service,sheets_service,daily_service,lambda:resource_monitor.latest,write_lock,ready_provider=lambda:database_ready,can_prune=lambda:not recording_needed())
    # A laptop acknowledgement alone cannot remove the only cloud history.
    reporting.configure_archival(lambda db:False,None)
    reporting.configure_report_ready(cloud_service.notify_work)
    feed_manager=FeedManager(on_quote=quote_received,on_status=status_received)
    await try_initialize_database()
    services=asyncio.create_task(database_services(reporting_loop))
    cloud_task=asyncio.create_task(cloud_service.run(stop_event))
    resource_task=asyncio.create_task(resource_monitor.run(stop_event))
    daily_task=asyncio.create_task(daily_service.run(stop_event))
    sheets_task=asyncio.create_task(sheets_service.run(stop_event))
    archive_task=asyncio.create_task(storage_archiver.run(stop_event))
    try:yield
    finally:
        stop_event.set()
        await feed_manager.stop()
        if collector_lease and collector_lease.token:
            with suppress(SQLAlchemyError):await asyncio.to_thread(collector_lease.release)
        try:await asyncio.wait_for(services,timeout=20)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            services.cancel()
            await asyncio.gather(services,return_exceptions=True)
        for task in (cloud_task,resource_task,daily_task,sheets_task,archive_task):task.cancel()
        await asyncio.gather(cloud_task,resource_task,daily_task,sheets_task,archive_task,return_exceptions=True)
        collector_lease=None;feed_manager=None

app=FastAPI(title='Personal Options Trader',version='1.0.0',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=ALLOWED_ORIGINS,allow_credentials=True,allow_methods=['GET','POST','PATCH','DELETE','OPTIONS'],allow_headers=['Content-Type','X-CSRF-Token','Authorization'])

@app.exception_handler(RequestValidationError)
async def private_broker_validation(request, error):
    # Validation errors ordinarily echo submitted input, including private keys.
    if request.url.path.startswith('/api/brokers/'):
        return JSONResponse({'detail':'Check the connection fields and try again.'},status_code=422)
    return await request_validation_exception_handler(request,error)


@app.middleware('http')
async def security_headers(request,call_next):
    if request.url.path.startswith('/api/') and request.url.path!='/api/health' and not database_ready:
        response=JSONResponse({'detail':'The database is temporarily unavailable. Background activity is paused; saved records are retained. Try again after database access is restored.','database_ready':False},status_code=503,headers={'Retry-After':str(DATABASE_RETRY_SECONDS)})
    else:
        try:response=await call_next(request)
        except SQLAlchemyError as error:
            log_database_error('API database operation',error)
            if is_database_unavailable(error):
                database_unavailable()
                response=JSONResponse({'detail':'The database connection is temporarily unavailable. Background activity is paused; saved records are retained.','database_ready':False},status_code=503,headers={'Retry-After':str(DATABASE_RETRY_SECONDS)})
            elif is_sqlite_contention(error):
                response=JSONResponse({'detail':'Another local save is completing. Please retry this action.','database_ready':True},status_code=503,headers={'Retry-After':'1'})
            else:
                response=JSONResponse({'detail':'This database operation could not be completed. Saved records are retained; please review the action or contact support.'},status_code=500)
    if request.url.path.startswith('/api/') and request.cookies.get(COOKIE) and response.status_code<400:touch_activity()
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Cache-Control']='no-store' if request.url.path.startswith('/api') else 'no-cache'
    if os.getenv('COOKIE_SECURE','false').lower()=='true':response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    return response

@app.get('/api/health')
def health():return {'ok':True,'mode':'PAPER','live_orders_supported':False,'database_ready':database_ready,'time':iso_now()}

@app.get('/health/live')
def process_health():
    return {'ok':True,'mode':'PAPER','uptime_seconds':round(time.monotonic()-_started_at,1)}

@app.get('/health/ready')
def readiness():
    return JSONResponse({'ready':database_ready,'mode':'PAPER','database_ready':database_ready},status_code=200 if database_ready else 503)

@app.get('/api/system/resources',dependencies=[Depends(require_session)])
def resources():
    result=resource_monitor.status() if resource_monitor else {'sample':None,'active_alerts':[]}
    return {**result,'archival':storage_archiver.status() if storage_archiver else None,'uptime_seconds':round(time.monotonic()-_started_at,1),'database_ready':database_ready,
            'last_database_write':_last_database_write,'collector_owned':bool(collector_lease and collector_lease.token),
            'recording':_recording_status,'quote_buffer':engine.buffered_quote_status(),
            'database':{'engine':db_engine.dialect.name,'type':db_engine.dialect.name,'size_bytes':(resource_monitor.latest or {}).get('storage',{}).get('sqlite_bytes') if resource_monitor else None,'journal_mode':os.getenv('SQLITE_JOURNAL_MODE','WAL'),'external_subscription_required':False,'verified':os.getenv('SQLITE_STORAGE_VERIFIED')=='true','filesystem':os.getenv('SQLITE_FILESYSTEM'),'path':'/app/data/trading.db' if db_engine.dialect.name=='sqlite' else None}}

@app.get('/api/system/health',dependencies=[Depends(require_session)])
def detailed_health():
    now=datetime.now(timezone.utc)
    sessions=[{'trade_id':trade['id'],'broker':trade['broker'],
               'legs':[{'instrument_id':leg['instrument_id'],**(exchange_calendar.state(leg,now) if exchange_calendar else {'known':False,'open':False,'reason':'Calendar unavailable.'})}
                       for leg in trade.get('legs',[]) if leg.get('status') in ('open','pending')]}
              for trade in _active_positions if not trade.get('demo')]
    return {'mode':'PAPER','live_orders_supported':False,'resources':resources(),
            'brokers':feed_manager.statuses() if feed_manager else feed_status,'exchange_sessions':sessions,
            'calendar_error':exchange_calendar.error if exchange_calendar else 'Calendar unavailable.'}

def drive_service():
    if cloud_service is None:raise HTTPException(503,'Cloud backup service has not started.')
    return cloud_service

@app.get('/api/cloud/drive/status',dependencies=[Depends(require_session)])
def drive_status(db=Depends(get_db)):
    return drive_service().status(db)

@app.post('/api/cloud/drive/connect')
async def drive_connect(current=Depends(require_session),db=Depends(get_db)):
    async with write_lock:
        try:return {'authorization_url':await asyncio.to_thread(cloud_backup.begin_authorization,db,current.token_hash,drive_service().config)}
        except cloud_backup.BackupError as error:raise HTTPException(409,str(error)) from None

@app.get('/api/cloud/drive/callback')
async def drive_callback(state:str=Query(min_length=16,max_length=250),code:str|None=Query(default=None,max_length=4096),current=Depends(require_session),db=Depends(get_db)):
    if not code:raise HTTPException(400,'Google Drive permission was not granted. Reconnect from Data & settings.')
    service=drive_service()
    def authorize():
        # This lock coordinates only Google workers. Broker capture keeps running
        # while the OAuth token exchange waits on Google's network response.
        with service.operation_lock:
            service.complete_authorization(current.token_hash,state,code)
            if sheets_service:
                sheets_service._quota_at=0
                with service.factory() as retry_db:sheets_service.retry(retry_db)
            if storage_archiver:
                storage_archiver.state={**storage_archiver.state,'next_attempt':0,'attempts':0}
                storage_archiver.notify_work()
            service.notify_work()
    try:await asyncio.to_thread(authorize)
    except cloud_backup.BackupError as error:raise HTTPException(409,str(error)) from None
    return RedirectResponse('/?drive=connected',status_code=303)

@app.post('/api/cloud/drive/queue',dependencies=[Depends(require_session)])
async def queue_cloud_reports():
    queued=await asyncio.to_thread(drive_service().enqueue_ready)
    drive_service().notify_work();reporting.notify_work()
    return {'queued':queued}

@app.post('/api/cloud/drive/retry/{job_id}',dependencies=[Depends(require_session)])
async def retry_cloud_report(job_id:str,db=Depends(get_db)):
    async with write_lock:
        retried=await asyncio.to_thread(drive_service().retry,db,job_id);drive_service().notify_work()
        return {'retried':retried}

@app.get('/api/session')
def session(request:Request,db=Depends(get_db)):
    current=session_from_cookie(request,db)
    return {'authenticated':bool(current),'setup_required':not bool(db.get(Owner,1)),'csrf_token':current.csrf_token if current else None,'totp_required':bool(os.getenv('OWNER_TOTP_SECRET'))}

@app.post('/api/auth/setup')
async def setup(payload:AuthInput,request:Request,response:Response,db=Depends(get_db)):
    validate_origin(request);rate_limit(request)
    async with write_lock:
        if db.get(Owner,1):raise HTTPException(409,'The owner account already exists. Sign in.')
        required=os.getenv('SETUP_TOKEN')
        if required:
            import hmac
            if not hmac.compare_digest(payload.setup_token or '',required):raise HTTPException(403,'Enter the setup token from your server environment.')
        elif os.getenv('ENVIRONMENT','development')=='production':raise HTTPException(503,'Set SETUP_TOKEN before creating the production owner account.')
        if len(payload.password)<12:raise HTTPException(422,'Use a password with at least 12 characters.')
        if not verify_totp(payload.totp_code):raise HTTPException(401,'A valid authenticator code is required.')
        db.add(Owner(id=1,password_hash=password_hash(payload.password)));db.commit()
        return create_session(db,response)

@app.post('/api/auth/login')
def login(payload:AuthInput,request:Request,response:Response,db=Depends(get_db)):
    validate_origin(request);rate_limit(request)
    owner=db.get(Owner,1)
    if not owner or not verify_password(payload.password,owner.password_hash) or not verify_totp(payload.totp_code):raise HTTPException(401,'The password or authenticator code is incorrect.')
    return create_session(db,response)

@app.post('/api/auth/password')
async def change_password(payload:PasswordInput,request:Request,response:Response,current=Depends(require_session),db=Depends(get_db)):
    rate_limit(request,'password',5)
    async with write_lock:
        owner=db.get(Owner,1)
        if not verify_password(payload.current_password,owner.password_hash) or not verify_totp(payload.totp_code):raise HTTPException(401,'The current password or authenticator code is incorrect.')
        owner.password_hash=password_hash(payload.new_password)
        db.execute(delete(UserSession));db.commit();response.delete_cookie(COOKIE)
        return {'ok':True,'authenticated':False,'message':'Password changed. Sign in again on each device.'}

@app.post('/api/auth/logout')
def logout(response:Response,current=Depends(require_session),db=Depends(get_db)):
    db.execute(delete(UserSession).where(UserSession.token_hash==current.token_hash));db.commit();response.delete_cookie(COOKIE);return {'ok':True}

@app.get('/api/dashboard',dependencies=[Depends(require_session)])
def get_dashboard(db=Depends(get_db)):return engine.dashboard(db,feed_manager.statuses() if feed_manager else feed_status)

@app.get('/api/trades',dependencies=[Depends(require_session)])
def trades(demo:bool|None=None,status:str|None=None,hidden:bool=False,offset:int=Query(default=0,ge=0),limit:int=Query(default=100,ge=1,le=200),db=Depends(get_db)):
    mode=engine.get_settings(db)['demo_mode'] if demo is None else demo
    query=select(Trade)
    if status=='closed':query=query.where(Trade.status.in_(['closed','cancelled']))
    elif status=='active':query=query.where(Trade.status.in_(['open','pending']))
    # Portable keyset-sized page; the date in each trade is retained rather
    # than reinterpreting this as a daily-only trade count.
    rows=sorted([r.data for r in db.scalars(query) if bool(r.data.get('demo'))==bool(mode) and bool(r.data.get('history_hidden'))==hidden],key=lambda t:t.get('created_at',''),reverse=True)
    return [engine.public_trade(t,engine.get_settings(db)) for t in rows[offset:offset+limit]]

@app.post('/api/trades',dependencies=[Depends(require_session)])
async def create_trade(payload:TradeInput,db=Depends(get_db)):
    async with write_lock:
        await ensure_writer(db)
        def persist():
            existing=db.get(ActionRecord,payload.idempotency_key)
            if not existing:refresh_demo_for_payload(db,payload.model_dump())
            result=engine.create_trades(db,payload);db.commit();update_demand(db)
            return result
        result=await asyncio.to_thread(persist);touch_activity()
        await refresh_selected_feeds(db)
        return result

@app.get('/api/trades/{trade_id}',dependencies=[Depends(require_session)])
def get_trade(trade_id:str,include_history:bool=True,history_limit:int=Query(default=200,ge=1,le=500),before_second:int|None=None,db=Depends(get_db)):
    row=db.get(Trade,trade_id)
    if not row:raise HTTPException(404,'Trade not found.')
    snapshots=[];timeline=[];events=[]
    if include_history:
        query=select(PnlSnapshot).where(PnlSnapshot.trade_id==trade_id)
        if before_second is not None:query=query.where(PnlSnapshot.snapshot_second<before_second)
        snapshots=list(db.scalars(query.order_by(PnlSnapshot.snapshot_second.desc()).limit(history_limit)))
        timeline=[s.data for s in reversed(snapshots)] if snapshots else [s.data for s in reversed(list(db.scalars(select(Timeline).where(Timeline.trade_id==trade_id).order_by(Timeline.id.desc()).limit(history_limit))))]
        events=[{'id':e.id,'kind':e.kind,'timestamp':e.timestamp,**e.data} for e in reversed(list(db.scalars(select(Event).where(Event.trade_id==trade_id).order_by(Event.id.desc()).limit(history_limit))))]
    return {'trade':engine.public_trade(row.data,engine.get_settings(db)),'events':events,'timeline':timeline,
            'history_limit':history_limit,'next_before_second':snapshots[-1].snapshot_second if len(snapshots)==history_limit else None,
            'recording_gaps':[{'start_second':g.start_second,'end_second':g.end_second,'reason':g.reason} for g in db.scalars(select(RecordingGap).where(RecordingGap.trade_id==trade_id).limit(100))] if include_history else []}

class HistoryVisibilityInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    hidden:bool=Field(strict=True)
    confirmed:Literal[True]

@app.post('/api/trades/{trade_id}/history-visibility',dependencies=[Depends(require_session)])
async def trade_history_visibility(trade_id:str,payload:HistoryVisibilityInput,db=Depends(get_db)):
    async with write_lock:
        await ensure_writer(db)
        def persist():
            result=engine.set_trade_history_hidden(db,trade_id,payload.hidden)
            db.commit()
            return result
        return await asyncio.to_thread(persist)

@app.post('/api/trades/{trade_id}/actions',dependencies=[Depends(require_session)])
async def trade_action(trade_id:str,payload:ActionInput,db=Depends(get_db)):
    async with write_lock:
        await ensure_writer(db)
        def persist():
            row=db.get(Trade,trade_id)
            if row and not db.get(ActionRecord,payload.idempotency_key):refresh_demo_for_payload(db,{**payload.model_dump(),'trade_id':trade_id},row.broker)
            result=engine.apply_action(db,trade_id,payload);db.commit();update_demand(db)
            return result
        result=await asyncio.to_thread(persist);touch_activity();reporting.notify_work()
        if cloud_service:cloud_service.notify_work()
        await refresh_selected_feeds(db)
        return result

@app.get('/api/events',dependencies=[Depends(require_session)])
def recent_events(limit:int=20,db=Depends(get_db)):
    limit=max(1,min(200,limit))
    return [{'id':e.id,'trade_id':e.trade_id,'kind':e.kind,'timestamp':e.timestamp,**e.data} for e in db.scalars(select(Event).order_by(Event.id.desc()).limit(limit))]

# Reuse immutable catalog rows by reference. Copying the entire 100k+ contract
# universe for each phone refresh previously consumed substantial CPU and RAM.
# Only selected pages are serialized; metadata contains no complete contracts.
from threading import Lock as _CatalogLock
_catalog_view_lock=_CatalogLock()
_catalog_view_cache={}

def _catalog_day():
    return datetime.now(timezone(timedelta(hours=5,minutes=30))).date().isoformat()

def _catalog_view(demo):
    global _catalog_view_cache
    rows=catalog._demo if demo else catalog._live
    day=_catalog_day()
    key=(id(catalog),catalog.revision,id(rows),len(rows),bool(demo),day)
    with _catalog_view_lock:
        if _catalog_view_cache.get('key')==key:return _catalog_view_cache
        grouped={};selected=[]
        for row in rows:
            if row['expiry']<day or row.get('broker') not in engine.BROKERS:continue
            selected.append(row)
            group_key=(row['underlying'],row['exchange'])
            group=grouped.setdefault(group_key,{'underlying':row['underlying'],'exchange':row['exchange'],'expiries':set(),'brokers':set(),'contracts':0})
            group['expiries'].add(row['expiry']);group['brokers'].add(row['broker']);group['contracts']+=1
        groups=[{**g,'expiries':sorted(g['expiries']),'brokers':sorted(g['brokers'])} for _,g in sorted(grouped.items())]
        metadata={'revision':catalog.revision,'trading_date':day,'total':len(selected),'groups':groups,
                  'underlyings':sorted({g['underlying'] for g in groups}),'demo':bool(demo),
                  'source':'Synthetic practice catalog — not exchange-valid contracts' if demo else 'Imported broker instrument master'}
        # One cache only: switching mode/reloading never retains a second master.
        _catalog_view_cache={'key':key,'rows':selected,'metadata':metadata}
        return _catalog_view_cache

@app.get('/api/instruments/catalog',dependencies=[Depends(require_session)])
def instrument_catalog(db=Depends(get_db)):
    return _catalog_view(engine.get_settings(db)['demo_mode'])['metadata']

@app.get('/api/instruments',dependencies=[Depends(require_session)])
def instruments(underlying:str|None=None,broker:str|None=None,exchange:str|None=None,expiry:str|None=None,
                limit:int=Query(2000,ge=1,le=2000),offset:int=Query(0,ge=0),db=Depends(get_db)):
    demo=engine.get_settings(db)['demo_mode'];view=_catalog_view(demo)
    items=[row for row in view['rows'] if (not underlying or row['underlying']==underlying)
           and (not broker or row['broker']==broker) and (not exchange or row['exchange']==exchange)
           and (not expiry or row['expiry']==expiry)]
    total=len(items);end=min(total,offset+limit)
    return {'instruments':items[offset:end],'underlyings':view['metadata']['underlyings'],'demo':demo,
            'source':view['metadata']['source'],'revision':view['metadata']['revision'],
            'total':total,'offset':offset,'limit':limit,'truncated':offset>0 or end<total,
            'next_offset':end if end<total else None,
            'selection_required':not underlying and total>limit,
            'catalog_url':'/api/instruments/catalog'}

@app.post('/api/instruments/reload',dependencies=[Depends(require_session)])
async def reload_instruments():
    catalog.reload()
    with SessionLocal() as db:
        active=[row.data for row in db.scalars(select(Trade).where(Trade.status.in_(['open','pending'])))]
        catalog.restore_live_contracts(active)
        await refresh_selected_feeds(db,force=True)
    return {'ok':True,'live_contracts':len(catalog.all(demo=False))}

class QuoteSelectionItem(BaseModel):
    model_config=ConfigDict(extra='forbid')
    broker:Literal['kotak']
    instrument_id:str=Field(min_length=1,max_length=180)


class QuoteSelectionInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    instruments:list[QuoteSelectionItem]=Field(default_factory=list,max_length=40)


def resolve_quote_selection(db,items):
    settings=engine.get_settings(db)
    option_chain.finish_read_transaction(db)
    if settings['demo_mode']:
        raise HTTPException(409,'Choose market-data mode before requesting broker quotes.')
    today=datetime.now(timezone(timedelta(hours=5,minutes=30))).date().isoformat()
    selected={}
    for item in items:
        row=catalog.find(item.instrument_id,item.broker) if catalog else None
        if not row or row.get('demo') is not False or row['broker']!=item.broker or row['expiry']<today:
            raise HTTPException(422,'Select current contracts from each selected broker instrument master.')
        selected[(item.broker,item.instrument_id)]=row
    return list(selected.values()),settings['stale_after_seconds']


def read_selected_quotes(db,contracts,stale_after):
    result=[]
    public_fields=('broker','instrument_id','underlying','exchange','segment','symbol','expiry','strike','option_type',
                   'ltp','bid','ask','received_at','exchange_timestamp','open_interest','volume','previous_close','source','demo')
    for contract in contracts:
        identifier=contract.get('instrument_id',contract['id']);broker=contract['broker']
        quote=engine.latest_quote(db,identifier,broker)
        if not quote or quote.get('demo') is not False or quote.get('broker')!=broker or quote.get('instrument_id')!=identifier:continue
        if any(quote.get(key) is not None and quote[key]!=contract.get(key) for key in ('exchange','segment')):continue
        public={key:quote.get(key) for key in public_fields}
        age=engine.age_seconds(quote.get('received_at'))
        public.update(quote_age_seconds=age,stale=age is None or age>stale_after)
        result.append(public)
    option_chain.finish_read_transaction(db)
    return result


@app.post('/api/quotes/selection')
async def select_quotes(payload:QuoteSelectionInput,request:Request,current=Depends(require_session),db=Depends(get_db)):
    rate_limit(request,'quote-selection',limit=120,seconds=60)
    contracts,stale_after=await asyncio.to_thread(resolve_quote_selection,db,payload.instruments)
    owner=current.token_hash
    with _quote_selection_lock:
        if contracts:
            # Short-lived views are bounded separately from durable positions.
            # An idle tab cannot retain subscriptions after its TTL expires.
            if owner not in _quote_selection_requests and len(_quote_selection_requests)>=8:
                oldest=min(_quote_selection_requests,key=lambda key:_quote_selection_requests[key][0])
                _quote_selection_requests.pop(oldest,None)
            _quote_selection_requests[owner]=(time.monotonic()+30,{row['id'] for row in contracts})
        else:_quote_selection_requests.pop(owner,None)
    touch_activity()
    await refresh_selected_feeds(db)
    quotes=await asyncio.to_thread(read_selected_quotes,db,contracts,stale_after)
    return {'quotes':quotes,'requested':len(contracts),'subscription_ttl_seconds':30,'mode':'PAPER'}


@app.get('/api/brokers',dependencies=[Depends(require_session)])
def brokers(db=Depends(get_db)):return engine.dashboard(db,feed_manager.statuses() if feed_manager else {})['brokers']

class KotakCredentialInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    consumer_key:SecretStr
    mobile_number:SecretStr
    ucc:SecretStr
    mpin:SecretStr
    totp_secret:SecretStr

@app.get('/api/brokers/kotak/setup',dependencies=[Depends(require_session)])
def kotak_setup_status(db=Depends(get_db)):
    return kotak_setup.status(db)

@app.post('/api/brokers/kotak/setup',dependencies=[Depends(require_session)])
async def save_kotak_setup(payload:KotakCredentialInput,request:Request):
    rate_limit(request,'broker-setup',limit=6)
    values={name:getattr(payload,name).get_secret_value() for name in type(payload).model_fields}
    try:
        # Credential writes use short transactions; no broker HTTP call runs here.
        async with write_lock:
            result=await asyncio.to_thread(kotak_setup.save,values)
    except broker_setup.BrokerSetupError as error:
        raise HTTPException(409,str(error)) from None
    # Reconnect through the normal collector demand/lease path, outside the save.
    global _feed_contract_ids
    _feed_contract_ids=None
    touch_activity()
    return result

@app.get('/api/settings',dependencies=[Depends(require_session)])
def settings(db=Depends(get_db)):return engine.get_settings(db)

@app.patch('/api/settings',dependencies=[Depends(require_session)])
async def patch_settings(payload:SettingsInput,db=Depends(get_db)):
    async with write_lock:
        await ensure_writer(db)
        def persist():
            result=engine.update_settings(db,payload.model_dump(exclude_none=True));db.commit();update_demand(db);return result
        return await asyncio.to_thread(persist)

@app.post('/api/risk',dependencies=[Depends(require_session)])
def risk(payload:RiskInput):
    legs=[leg.model_dump(exclude_none=True) for leg in payload.legs]
    if any(leg.get('exchange')=='MCX' and not leg.get('demo') and not leg.get('price_multiplier') for leg in legs):return {'supported':False,'reason':'MCX risk requires the verified monetary multiplier from the broker master.','points':[]}
    try:return strategy_risk(legs,payload.spot,payload.charges)
    except (KeyError,TypeError,ValueError):raise HTTPException(422,'Supply option type, side, strike, lot size, lots and entry price for every leg.')

@app.post('/api/ws-ticket')
async def issue_ws_ticket(current=Depends(require_session),db=Depends(get_db)):
    async with write_lock:
        def save_ticket():
            raw=secrets.token_urlsafe(32)
            db.execute(delete(SocketTicket).where(SocketTicket.expires_at<time.time()))
            db.add(SocketTicket(token_hash=digest(raw),session_hash=current.token_hash,expires_at=time.time()+30))
            db.commit()
            return {'ticket':raw,'expires_in':30}
        return await asyncio.to_thread(save_ticket)

def socket_identity(ticket,request):
    with SessionLocal() as db:
        if ticket:
            row=db.get(SocketTicket,digest(ticket))
            session=db.get(UserSession,row.session_hash) if row and row.expires_at>time.time() else None
            if row:db.delete(row);db.commit()
        else:session=session_from_cookie(request,db)
        if not session or session.expires_at<=time.time():return None
        return session.expires_at,session.token_hash

def socket_dashboard(session_hash,statuses):
    with SessionLocal() as db:
        current=db.get(UserSession,session_hash)
        if not current or current.expires_at<=time.time():return None
        return engine.dashboard(db,statuses)

@app.websocket('/ws')
@app.websocket('/api/ws')
async def websocket(ws:WebSocket):
    global _viewers
    origin=ws.headers.get('origin')
    if origin and origin.rstrip('/') not in ALLOWED_ORIGINS:await ws.close(code=4403);return
    async with write_lock:
        identity=await asyncio.to_thread(socket_identity,ws.query_params.get('ticket'),ws)
        if not identity:await ws.close(code=4401);return
        expiry,session_hash=identity
    await ws.accept()
    _viewers+=1;touch_activity()
    try:
        while time.time()<expiry:
            data=await asyncio.to_thread(socket_dashboard,session_hash,feed_manager.statuses() if feed_manager else {})
            if data is None:break
            await ws.send_json({'type':'dashboard','data':data})
            await asyncio.sleep(1)
        await ws.close(code=4401)
    except (WebSocketDisconnect,RuntimeError):pass
    except SQLAlchemyError as error:
        log_database_error('Workspace stream',error)
        if is_database_unavailable(error):database_unavailable()
        with suppress(RuntimeError):await ws.close(code=1013)
    finally:_viewers=max(0,_viewers-1)

class DailyPrepareInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')

class LocalBackupInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    records:int=Field(ge=0)
    local_backup_confirmed:bool=False
    confirmation:str=''

@app.get('/api/daily-reports',dependencies=[Depends(require_session)])
def daily_list(db=Depends(get_db)):
    return {'reports':daily_reports.list_reports(db)}

@app.post('/api/daily-reports/prepare',dependencies=[Depends(require_session)])
async def daily_prepare(payload:DailyPrepareInput,db=Depends(get_db)):
    async with write_lock:
        try:result=await asyncio.to_thread(daily_reports.queue_day,db,payload.date)
        except (ValueError,daily_reports.DailyReportError) as error:raise HTTPException(409,str(error)) from None
        if daily_service:daily_service.notify_work()
        return {**result,'message':'Daily export queued. It finalizes after its verified trading sessions end.'}

@app.get('/api/daily-reports/{day}',dependencies=[Depends(require_session)])
def daily_details(day:str,db=Depends(get_db)):
    try:return daily_reports.report_manifest(db,day)
    except (ValueError,daily_reports.DailyReportError) as error:raise HTTPException(404,str(error)) from None

@app.get('/api/daily-reports/{day}/download',dependencies=[Depends(require_session)])
def daily_download(day:str,kind:str='xlsx',part:int=Query(default=1,ge=0,le=10000),db=Depends(get_db)):
    if kind not in {'xlsx','csv_gz','index_xlsx'}:raise HTTPException(422,'Unknown export format.')
    try:item=daily_reports.artifact(db,day,kind,part)
    except (ValueError,daily_reports.DailyReportError) as error:raise HTTPException(409,str(error)) from None
    return Response(content=item['file_bytes'],media_type=item['mime_type'],headers={'Content-Disposition':f'attachment; filename="{item["filename"]}"','X-Content-SHA256':item['sha256'],'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})

@app.post('/api/daily-reports/{day}/verify-local',dependencies=[Depends(require_session)])
async def daily_verify_local(day:str,payload:LocalBackupInput,db=Depends(get_db)):
    async with write_lock:
        try:return await asyncio.to_thread(daily_reports.verify_local_ack,db,day,payload.sha256,payload.local_backup_confirmed,payload.records)
        except daily_reports.DailyReportError as error:raise HTTPException(409,str(error)) from None

@app.get('/api/sheets/status',dependencies=[Depends(require_session)])
def sheet_status(db=Depends(get_db)):
    if not sheets_service:raise HTTPException(503,'Google Sheets worker has not started.')
    return sheets_service.status(db)

@app.post('/api/sheets/retry',dependencies=[Depends(require_session)])
async def sheets_retry(db=Depends(get_db)):
    if not sheets_service:raise HTTPException(503,'Google Sheets worker has not started.')
    async with write_lock:await asyncio.to_thread(sheets_service.retry,db);sheets_service.notify_work()
    return {'retry_requested':True}

@app.get('/api/daily-reports/{day}/cloud-cleanup',dependencies=[Depends(require_session)])
def daily_cleanup_preview(day:str,db=Depends(get_db)):
    if not sheets_service:raise HTTPException(503,'Google Sheets worker has not started.')
    try:return sheets_service.cleanup_preview(db,day)
    except sheets_sync.SheetsError as error:raise HTTPException(409,str(error)) from None

@app.post('/api/daily-reports/{day}/cloud-cleanup',dependencies=[Depends(require_session)])
async def daily_cleanup(day:str,payload:LocalBackupInput):
    if not sheets_service:raise HTTPException(503,'Google Sheets worker has not started.')
    # Google cleanup coordinates with account switches, uploads and archival
    # through the service's operation_lock. Do not hold the recorder's lock
    # while waiting for quota, Trash or verification HTTP requests.
    try:return await asyncio.to_thread(sheets_service.cleanup_confirm,day,payload.sha256,payload.records,payload.local_backup_confirmed,payload.confirmation)
    except sheets_sync.SheetsError as error:raise HTTPException(409,str(error)) from None

# Reporting tables are imported before lifespan's create_all and share the same journal.
from .companion_api import router_browser,router_companion
app.include_router(router_browser,dependencies=[Depends(require_session)])
app.include_router(router_companion)

# Register before the SPA catch-all, with the same owner/session/CSRF rules.
from . import option_chain
option_chain.configure(write_lock,refresh_selected_feeds)
app.include_router(option_chain.router)

# The built PWA can be opened with one Windows backend process. API routes above
# always take precedence and are never caught by the SPA fallback.
FRONTEND_DIST=Path(__file__).resolve().parents[2]/'frontend'/'dist'
if FRONTEND_DIST.exists():
    from fastapi.staticfiles import StaticFiles
    if (FRONTEND_DIST/'assets').exists():app.mount('/assets',StaticFiles(directory=FRONTEND_DIST/'assets'),name='assets')
    @app.get('/{path:path}',include_in_schema=False)
    def frontend(path:str):
        if path.startswith(('api/','ws')):raise HTTPException(404,'Endpoint not found.')
        requested=(FRONTEND_DIST/path).resolve()
        if requested.is_relative_to(FRONTEND_DIST.resolve()) and requested.is_file():return FileResponse(requested)
        return FileResponse(FRONTEND_DIST/'index.html')
