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
from fastapi import FastAPI, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from sqlalchemy import select, delete
from sqlalchemy.exc import SQLAlchemyError
from .database import (Base, engine as db_engine, SessionLocal, get_db, iso_now,
                       is_database_unavailable, is_sqlite_contention,
                       run_transaction, sqlite_error_code)
from .models import Owner, UserSession, SocketTicket, Trade, Event, Timeline, QuoteEvent, Setting, SchemaVersion, ActionRecord
from .security import require_session, create_session, password_hash, verify_password, verify_totp, digest, rate_limit, validate_origin, session_from_cookie, COOKIE, ALLOWED_ORIGINS
from .schemas import AuthInput, PasswordInput, TradeInput, ActionInput, SettingsInput, RiskInput
from . import engine
from .payoff import strategy_risk

log=logging.getLogger('personal_options_trader')
write_lock=asyncio.Lock()
stop_event=asyncio.Event()
feed_manager=None
feed_status={}
catalog=None
_feed_contract_ids=None
database_ready=True
DATABASE_RETRY_SECONDS=60

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

def selected_feed_contracts(db):
    wanted={s.strip() for s in os.getenv('FEED_INSTRUMENT_IDS','').split(',') if s.strip()}
    for row in db.scalars(select(Trade).where(Trade.status.in_(['open','pending']))):
        if row.data.get('demo') is False:
            wanted.update(l['instrument_id'] for l in row.data.get('legs',[]) if l.get('status') in {'open','pending'})
    return [c for c in catalog.all(demo=False) if c['id'] in wanted]

async def refresh_selected_feeds(db,force=False):
    global _feed_contract_ids
    if feed_manager is None:return
    contracts=selected_feed_contracts(db)
    identities=frozenset((c['id'],c['broker']) for c in contracts)
    if force or identities!=_feed_contract_ids:
        await feed_manager.stop()
        await feed_manager.start(contracts)
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
        engine.record_quote(db,demo_quote(c,c.get('broker','dhan')))
    def contracts(underlying,broker='dhan',kind='CE'):
        rows=[c for c in all_contracts if c['underlying']==underlying and c.get('broker')==broker and c['option_type']==kind]
        expiries=sorted({c['expiry'] for c in rows})
        return sorted([c for c in rows if c['expiry']==expiries[0]],key=lambda c:c['strike']) if expiries else []
    underlyings=list(dict.fromkeys(c['underlying'] for c in all_contracts))
    nifty=next((u for u in underlyings if u.upper().replace(' ','') in ('NIFTY','NIFTY50')),underlyings[0] if underlyings else None)
    gold=next((u for u in underlyings if 'GOLD' in u.upper()),nifty)
    configurations=[('NIFTY bull call spread',nifty,'dhan','spread'),('NIFTY iron condor',nifty,'kotak','condor'),('Gold protective spread',gold,'fivepaisa','spread'),('Completed practice trade',nifty,'dhan','closed')]
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
    try:
        async with write_lock:
            await asyncio.to_thread(process_quote_receipt,quote)
    except SQLAlchemyError as error:
        log_database_error('Quote processing',error)
        if is_database_unavailable(error):database_unavailable()
        raise

async def status_received(broker,status):
    feed_status[broker]=status

def capture_once():
    def capture(db):
        engine.recover_pending_quotes(db)
        settings=engine.get_settings(db)
        if settings['demo_mode']:
            needed=set()
            for trade in engine.trade_records(db):
                if trade['status'] in ('open','pending') and trade.get('demo'):
                    source=trade['broker'] if settings['preferred_feed']=='broker' else settings['preferred_feed']
                    needed.update((source,l['instrument_id']) for l in trade['legs'] if l['status'] in ('open','pending'))
            for broker,instrument_id in needed:
                contract=engine.contract_for(instrument_id,broker,True)
                engine.record_quote(db,demo_quote(contract,broker))
        engine.take_snapshots(db)
    run_transaction(capture,SessionLocal)

async def background_loop():
    while not stop_event.is_set() and database_ready:
        try:
            async with write_lock:
                # Database latency must not prevent HTTP health checks or UI input.
                await asyncio.to_thread(capture_once)
        except SQLAlchemyError as error:
            log_database_error('Database capture',error)
            if is_database_unavailable(error):
                database_unavailable()
                return
            # A short local write conflict or one invalid statement must not
            # turn a healthy workspace into a minute-long connection outage.
        except Exception:
            log.exception('Background capture failed; journal data retained. Feed/UI should be checked.')
        try:await asyncio.wait_for(stop_event.wait(),timeout=1)
        except asyncio.TimeoutError:pass

def initialize_database():
    """Run blocking database setup outside the HTTP event loop."""
    Base.metadata.create_all(db_engine)
    with SessionLocal() as db:
        if not db.get(SchemaVersion,1):db.add(SchemaVersion(version=1));db.commit()
        persisted_trades=[row.data for row in db.scalars(select(Trade))]
        catalog.restore_demo_contracts(persisted_trades)
        catalog.restore_live_contracts(persisted_trades)
        while engine.recover_pending_quotes(db):db.commit()
        seed_demo(db)

async def try_initialize_database():
    global database_ready
    try:
        await asyncio.to_thread(initialize_database)
        with SessionLocal() as db:
            await refresh_selected_feeds(db,force=True)
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
    global catalog,feed_manager,stop_event,database_ready
    from .instruments import ContractCatalog
    from .reporting import reporting_loop
    from . import reporting
    from .brokers import FeedManager
    stop_event=asyncio.Event();database_ready=False
    catalog=ContractCatalog();engine.set_catalog(catalog)
    def delete_archived_rows(db,trade_id):
        row=db.get(Trade,trade_id)
        if not row or row.status!='closed':
            raise ValueError('Only the acknowledged closed trade may be archived.')
        events=db.execute(delete(Event).where(Event.trade_id==trade_id)).rowcount
        timeline=db.execute(delete(Timeline).where(Timeline.trade_id==trade_id)).rowcount
        row.data={**row.data,'archived':True}
        return {'trade_events':events,'timeline':timeline}
    reporting.configure_archival(lambda db:engine.get_settings(db).get('delete_after_local_save',False),delete_archived_rows)
    feed_manager=FeedManager(on_quote=quote_received,on_status=status_received)
    await try_initialize_database()
    services=asyncio.create_task(database_services(reporting_loop))
    try:yield
    finally:
        stop_event.set()
        await feed_manager.stop()
        try:await asyncio.wait_for(services,timeout=20)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            services.cancel()
            await asyncio.gather(services,return_exceptions=True)

app=FastAPI(title='Personal Options Trader',version='1.0.0',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=ALLOWED_ORIGINS,allow_credentials=True,allow_methods=['GET','POST','PATCH','DELETE','OPTIONS'],allow_headers=['Content-Type','X-CSRF-Token','Authorization'])

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
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Cache-Control']='no-store' if request.url.path.startswith('/api') else 'no-cache'
    if os.getenv('COOKIE_SECURE','false').lower()=='true':response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    return response

@app.get('/api/health')
def health():return {'ok':True,'mode':'PAPER','live_orders_supported':False,'database_ready':database_ready,'time':iso_now()}

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
def trades(demo:bool|None=None,db=Depends(get_db)):return engine.list_trades(db,demo)

@app.post('/api/trades',dependencies=[Depends(require_session)])
async def create_trade(payload:TradeInput,db=Depends(get_db)):
    async with write_lock:
        existing=db.get(ActionRecord,payload.idempotency_key)
        if not existing:refresh_demo_for_payload(db,payload.model_dump())
        result=engine.create_trades(db,payload);db.commit()
        await refresh_selected_feeds(db)
        return result

@app.get('/api/trades/{trade_id}',dependencies=[Depends(require_session)])
def get_trade(trade_id:str,include_history:bool=True,db=Depends(get_db)):
    row=db.get(Trade,trade_id)
    if not row:raise HTTPException(404,'Trade not found.')
    return {'trade':engine.public_trade(row.data,engine.get_settings(db)),'events':[{'id':e.id,'kind':e.kind,'timestamp':e.timestamp,**e.data} for e in db.scalars(select(Event).where(Event.trade_id==trade_id).order_by(Event.id))] if include_history else [],'timeline':[s.data for s in db.scalars(select(Timeline).where(Timeline.trade_id==trade_id).order_by(Timeline.id))] if include_history else []}

@app.post('/api/trades/{trade_id}/actions',dependencies=[Depends(require_session)])
async def trade_action(trade_id:str,payload:ActionInput,db=Depends(get_db)):
    async with write_lock:
        row=db.get(Trade,trade_id)
        if row and not db.get(ActionRecord,payload.idempotency_key):refresh_demo_for_payload(db,{**payload.model_dump(),'trade_id':trade_id},row.broker)
        result=engine.apply_action(db,trade_id,payload);db.commit()
        await refresh_selected_feeds(db)
        return result

@app.get('/api/events',dependencies=[Depends(require_session)])
def recent_events(limit:int=20,db=Depends(get_db)):
    limit=max(1,min(200,limit))
    return [{'id':e.id,'trade_id':e.trade_id,'kind':e.kind,'timestamp':e.timestamp,**e.data} for e in db.scalars(select(Event).order_by(Event.id.desc()).limit(limit))]

@app.get('/api/instruments',dependencies=[Depends(require_session)])
def instruments(underlying:str|None=None,broker:str|None=None,db=Depends(get_db)):
    demo=engine.get_settings(db)['demo_mode'];items=catalog.list_contracts(underlying=underlying,broker=broker,demo=demo)
    return {'instruments':items,'underlyings':sorted({i['underlying'] for i in catalog.all(demo=demo)}),'demo':demo,'source':'Synthetic practice catalog — not exchange-valid contracts' if demo else 'Imported broker instrument master'}

@app.post('/api/instruments/reload',dependencies=[Depends(require_session)])
async def reload_instruments():
    catalog.reload()
    with SessionLocal() as db:
        active=[row.data for row in db.scalars(select(Trade).where(Trade.status.in_(['open','pending'])))]
        catalog.restore_live_contracts(active)
        await refresh_selected_feeds(db,force=True)
    return {'ok':True,'live_contracts':len(catalog.all(demo=False))}

@app.get('/api/brokers',dependencies=[Depends(require_session)])
def brokers(db=Depends(get_db)):return engine.dashboard(db,feed_manager.statuses() if feed_manager else {})['brokers']

@app.get('/api/settings',dependencies=[Depends(require_session)])
def settings(db=Depends(get_db)):return engine.get_settings(db)

@app.patch('/api/settings',dependencies=[Depends(require_session)])
async def patch_settings(payload:SettingsInput,db=Depends(get_db)):
    async with write_lock:
        result=engine.update_settings(db,payload.model_dump(exclude_none=True));db.commit();return result

@app.post('/api/risk',dependencies=[Depends(require_session)])
def risk(payload:RiskInput):
    legs=[leg.model_dump(exclude_none=True) for leg in payload.legs]
    if any(leg.get('exchange')=='MCX' and not leg.get('demo') and not leg.get('price_multiplier') for leg in legs):return {'supported':False,'reason':'MCX risk requires the verified monetary multiplier from the broker master.','points':[]}
    try:return strategy_risk(legs,payload.spot,payload.charges)
    except (KeyError,TypeError,ValueError):raise HTTPException(422,'Supply option type, side, strike, lot size, lots and entry price for every leg.')

@app.post('/api/ws-ticket')
async def issue_ws_ticket(current=Depends(require_session),db=Depends(get_db)):
    async with write_lock:
        raw=secrets.token_urlsafe(32)
        db.execute(delete(SocketTicket).where(SocketTicket.expires_at<time.time()))
        db.add(SocketTicket(token_hash=digest(raw),session_hash=current.token_hash,expires_at=time.time()+30))
        db.commit()
        return {'ticket':raw,'expires_in':30}

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
    origin=ws.headers.get('origin')
    if origin and origin.rstrip('/') not in ALLOWED_ORIGINS:await ws.close(code=4403);return
    async with write_lock:
        identity=await asyncio.to_thread(socket_identity,ws.query_params.get('ticket'),ws)
        if not identity:await ws.close(code=4401);return
        expiry,session_hash=identity
    await ws.accept()
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

# Reporting tables are imported before lifespan's create_all and share the same journal.
from .companion_api import router_browser,router_companion
app.include_router(router_browser,dependencies=[Depends(require_session)])
app.include_router(router_companion)

# Register before the SPA catch-all, with the same owner/session/CSRF rules.
from . import option_chain
option_chain.configure(write_lock)
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
