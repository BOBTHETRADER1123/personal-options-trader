"""Real ledger and API tests use an isolated in-memory journal, never user data."""
import copy
import time
import uuid
from datetime import datetime,timezone,timedelta
import pytest
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.database import Base,get_db,iso_now,configure_sqlite
from app.models import Trade,Event,Timeline,QuoteEvent,ActionRecord,Owner,UserSession,PendingQuote
from app.reporting import ExportJob
from app.instruments import ContractCatalog
from app import engine
from app.payoff import strategy_risk,leg_payoff
from app.security import password_hash,verify_password,verify_totp

@pytest.fixture
def db(tmp_path):
    sql=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    configure_sqlite(sql)
    Base.metadata.create_all(sql)
    catalog=ContractCatalog(tmp_path/'absent.json');engine.set_catalog(catalog)
    with sessionmaker(sql,expire_on_commit=False)() as session:
        # Legacy detailed mode remains explicit so its audit/replay contract is
        # tested independently of the new default compact recording policy.
        engine.update_settings(session,{'brokerage_per_order':0,'charges_percent':0,'slippage_bps':0,'fill_basis':'ltp','demo_mode':True,'logging_mode':'every_update'})
        session.commit();yield session
    sql.dispose()

def contract(broker='kotak',kind='CE',offset=0):
    items=engine.catalog.list_contracts(underlying='NIFTY',broker=broker,demo=True)
    expiry=min(c['expiry'] for c in items)
    rows=sorted([c for c in items if c['option_type']==kind and c['expiry']==expiry],key=lambda c:c['strike'])
    return rows[len(rows)//2+offset]

def quote(db,c=None,price=100,broker='kotak',**kw):
    c=c or contract(broker)
    return engine.record_quote(db,{'broker':broker,'instrument_id':c['id'],'ltp':price,'bid':price-.1,'ask':price+.1,'received_at':iso_now(),'demo':True,**kw})

def enter(db,side='BUY',lots=2,brokers=None,order_type='MARKET',price=None,key=None):
    brokers=brokers or ['kotak'];c=contract()
    for b in brokers:quote(db,c,100,b)
    result=engine.create_trades(db,{'name':'Test strategy','underlying':'NIFTY','brokers':brokers,'legs':[{'instrument_id':c['id'],'side':side,'lots':lots,'order_type':order_type,'price':price,'enabled':True}],'idempotency_key':key or f'test-{uuid.uuid4()}','confirmed':True})
    db.commit();return result

def action(db,trade,kind,**kw):
    payload={'action':kind,'idempotency_key':f'test-action-{uuid.uuid4()}','confirmed':True,**kw}
    result=engine.apply_action(db,trade['id'],payload);db.commit();return result

def test_long_and_short_pnl(db):
    buy=enter(db,'BUY')['trades'][0];sell=enter(db,'SELL')['trades'][0]
    quote(db,price=115);db.commit()
    assert db.get(Trade,buy['id']).data['unrealized_pnl']==15*2*50
    assert db.get(Trade,sell['id']).data['unrealized_pnl']==-15*2*50

def test_add_lots_weighted_average_and_partial_exit(db):
    trade=enter(db,lots=2)['trades'][0];leg=trade['legs'][0]
    quote(db,price=120)
    result=action(db,trade,'add_lots',leg_id=leg['id'],lots=1)['trade']
    assert result['legs'][0]['entry_price']==pytest.approx(106.6666666667)
    quote(db,price=130)
    result=action(db,trade,'partial_exit',leg_id=leg['id'],lots=1)['trade']
    assert result['legs'][0]['open_lots']==2
    assert result['realized_pnl']==pytest.approx(1166.67,abs=.01)
    assert result['unrealized_pnl']==pytest.approx(2333.33,abs=.01)

def test_idempotent_kotak_trade_and_key_mismatch(db):
    key='kotak-only-001';first=enter(db,brokers=['kotak'],key=key)
    second=enter(db,brokers=['kotak'],key=key)
    assert first['action_id']==second['action_id']
    assert db.scalar(select(func.count()).select_from(Trade))==1
    with pytest.raises(HTTPException):enter(db,lots=1,key=key)

def test_limit_waits_then_fills_at_limit_or_better(db):
    trade=enter(db,order_type='LIMIT',price=90)['trades'][0]
    assert trade['status']=='pending'
    quote(db,price=95);assert db.get(Trade,trade['id']).data['status']=='pending'
    quote(db,price=89);db.commit();filled=db.get(Trade,trade['id']).data
    assert filled['status']=='open';assert filled['legs'][0]['entry_price']==89

def test_cancel_pending_does_not_create_position(db):
    trade=enter(db,order_type='LIMIT',price=90)['trades'][0]
    result=action(db,trade,'cancel_pending')['trade']
    assert result['status']=='cancelled';assert result['legs'][0]['open_lots']==0

def test_stale_quote_rejected_without_fabrication(db):
    trade=enter(db)['trades'][0]
    quote(db,price=110,received_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
    with pytest.raises(HTTPException,match='stale'):action(db,trade,'exit_strategy')
    db.rollback();assert db.get(Trade,trade['id']).status=='open'

def test_close_enqueues_workbook_in_same_transaction(db):
    trade=enter(db)['trades'][0];quote(db,price=125)
    closed=action(db,trade,'exit_strategy')['trade']
    assert closed['status']=='closed';assert closed['net_pnl']==2500
    job=db.scalar(select(ExportJob).where(ExportJob.trade_id==trade['id']))
    assert job and job.status=='pending'
    assert job.payload['format']=='summary' and job.payload['trade']['net_pnl']==2500
    assert not any(key in job.payload for key in ('events','timeline','quotes'))
    assert db.get(Trade,trade['id']).data['export_queued']

def test_excursions_keep_observed_times_and_giveback(db):
    trade=enter(db,lots=1)['trades'][0]
    at1=iso_now();quote(db,price=80,received_at=at1)
    at2=iso_now();quote(db,price=140,received_at=at2)
    quote(db,price=128);db.commit()
    result=action(db,trade,'exit_strategy')['trade']
    assert result['mfe']==2000;assert result['mae']==-1000
    assert result['mfe_at']==at2 and result['mae_at']==at1
    assert result['net_pnl']==1400;assert result['profit_giveback']==600

def test_every_received_quote_journaled_and_snapshot_carried_forward(db):
    trade=enter(db)['trades'][0]
    before=db.scalar(select(func.count()).select_from(QuoteEvent))
    for i in range(4):quote(db,price=100,sequence=str(i))
    assert db.scalar(select(func.count()).select_from(QuoteEvent))==before+4
    engine.take_snapshots(db);db.flush();engine.take_snapshots(db);db.flush()
    last=db.scalars(select(Timeline).where(Timeline.trade_id==trade['id']).order_by(Timeline.id.desc())).first()
    assert last.data['carried_forward'] is True

def test_add_leg_duplicate_and_edit_audit(db):
    trade=enter(db)['trades'][0];c=contract(offset=1);quote(db,c,70)
    result=action(db,trade,'add_leg',leg={'instrument_id':c['id'],'side':'SELL','lots':1,'order_type':'MARKET'})['trade']
    assert len(result['legs'])==2
    edited=action(db,trade,'edit',name='Changed assumptions')['trade']
    assert edited['name']=='Changed assumptions'
    assert db.scalar(select(Event).where(Event.trade_id==trade['id'],Event.kind=='paper_assumption_edited'))
    duplicate=action(db,trade,'duplicate')
    assert duplicate['trades'][0]['id']!=trade['id']

def test_confirmations_and_quantity_bounds(db):
    trade=enter(db)['trades'][0]
    with pytest.raises(HTTPException):engine.apply_action(db,trade['id'],{'action':'exit_strategy','confirmed':False,'idempotency_key':'unconfirmed'})
    with pytest.raises(HTTPException):action(db,trade,'partial_exit',leg_id=trade['legs'][0]['id'],lots=100)

def test_payoff_known_spread_and_unlimited_risk():
    base={'underlying':'NIFTY','expiry':'2027-01-01','lot_size':50,'lots':1,'option_type':'CE'}
    legs=[{**base,'strike':24000,'entry_price':150,'side':'BUY'},{**base,'strike':24200,'entry_price':60,'side':'SELL'}]
    risk=strategy_risk(legs)
    assert risk['max_profit']==5500 and risk['max_loss']==4500
    assert risk['breakevens']==[24090]
    assert risk['net_premium']==-4500
    short=strategy_risk([legs[1]])
    assert short['loss_unlimited'] and short['max_loss'] is None
    mixed=strategy_risk([legs[0],{**legs[1],'expiry':'2027-02-01'}])
    assert not mixed['supported']

def test_put_payoff_at_zero_and_iron_condor():
    base={'underlying':'NIFTY','expiry':'2027-01-01','lot_size':50,'lots':1,'entry_price':20}
    legs=[{**base,'strike':23900,'side':'BUY','option_type':'PE'},{**base,'strike':24000,'side':'SELL','option_type':'PE','entry_price':50},{**base,'strike':24200,'side':'SELL','option_type':'CE','entry_price':50},{**base,'strike':24300,'side':'BUY','option_type':'CE'}]
    risk=strategy_risk(legs)
    assert risk['max_profit']==3000 and risk['max_loss']==2000
    assert risk['breakevens']==[23940,24260]
    assert sum(leg_payoff(l,0) for l in legs)==-2000

def test_password_hash_and_totp():
    hashed=password_hash('correct horse battery')
    assert verify_password('correct horse battery',hashed)
    assert not verify_password('incorrect',hashed)
    # RFC 6238 test vector 1234567890 -> 89005924 (8 digits), 005924 for six.
    assert verify_totp('005924','GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',1234567890)
    assert not verify_totp('000000','GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',1234567890)

def test_auth_csrf_origin_and_api_flow(db,monkeypatch):
    from app import main
    monkeypatch.delenv('OWNER_TOTP_SECRET',raising=False);monkeypatch.delenv('SETUP_TOKEN',raising=False);monkeypatch.setenv('ENVIRONMENT','development')
    monkeypatch.setattr(main,'catalog',engine.catalog)
    main.app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(main.app)
        assert client.get('/api/dashboard').status_code==401
        setup=client.post('/api/auth/setup',json={'password':'test strong password'})
        assert setup.status_code==200
        csrf=setup.json()['csrf_token'];headers={'x-csrf-token':csrf,'origin':'http://localhost:5173'}
        assert client.get('/api/dashboard').status_code==200
        assert client.patch('/api/settings',json={'paper_capital':200000}).status_code==403
        assert client.patch('/api/settings',json={'paper_capital':200000},headers={**headers,'origin':'https://evil.invalid'}).status_code==403
        assert client.patch('/api/settings',json={'paper_capital':200000},headers=headers).status_code==200
        assert client.post('/api/ws-ticket').status_code==403
        ticket=client.post('/api/ws-ticket',headers=headers)
        assert ticket.status_code==200 and ticket.json()['expires_in']==30
        valid_risk={'underlying':'NIFTY','expiry':'2027-01-01','lot_size':50,'lots':1,'strike':24000,'entry_price':100,'side':'BUY','option_type':'CE','extra_contract_field':'allowed'}
        assert client.post('/api/risk',json={'legs':[valid_risk]},headers=headers).status_code==200
        assert client.post('/api/risk',json={'legs':[{**valid_risk,'side':'INVALID'}]},headers=headers).status_code==422
        assert client.post('/api/risk',json={'legs':[{**valid_risk,'lots':-1}]},headers=headers).status_code==422
        assert client.post('/api/risk',json={'legs':[{**valid_risk,'entry_price':'Infinity'}]},headers=headers).status_code==422
        c=contract();payload={'name':'Browser trade','underlying':'NIFTY','brokers':['kotak'],'legs':[{'instrument_id':c['id'],'side':'BUY','lots':1}],'idempotency_key':'browser-flow-001','confirmed':True}
        created=client.post('/api/trades',json=payload,headers=headers)
        assert created.status_code==200 and len(created.json()['trades'])==1
        trade=created.json()['trades'][0]
        assert client.get(f'/api/trades/{trade["id"]}').json()['events']
        changed=client.post('/api/auth/password',json={'current_password':'test strong password','new_password':'another strong password'},headers=headers)
        assert changed.status_code==200
        assert client.get('/api/dashboard').status_code==401
    finally:main.app.dependency_overrides.clear()

def test_outer_rollback_removes_trade_and_idempotency_record(db):
    c=contract();quote(db,c);db.commit()
    payload={'name':'Atomic','underlying':'NIFTY','brokers':['kotak'],'legs':[{'instrument_id':c['id'],'side':'BUY','lots':1,'enabled':True}],'idempotency_key':'rollback-atomic-test','confirmed':True}
    engine.create_trades(db,payload)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Trade))==0
    assert db.get(ActionRecord,payload['idempotency_key']) is None

def test_stale_crossed_limit_receipt_survives_without_fill(db):
    trade=enter(db,order_type='LIMIT',price=90)['trades'][0]
    before=db.scalar(select(func.count()).select_from(QuoteEvent))
    quote(db,price=80,received_at=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat())
    db.commit()
    assert db.scalar(select(func.count()).select_from(QuoteEvent))==before+1
    assert db.get(Trade,trade['id']).data['status']=='pending'

def test_durable_receipt_recovers_after_processing_restart(db):
    trade=enter(db)['trades'][0];c=contract()
    event_id=engine.record_quote(db,{'broker':'kotak','instrument_id':c['id'],'ltp':120,'received_at':iso_now(),'demo':True},process=False)
    db.commit();db.expire_all()
    assert db.get(PendingQuote,event_id)
    assert engine.recover_pending_quotes(db)==1
    db.commit()
    assert db.get(Trade,trade['id']).data['unrealized_pnl']==2000
    assert db.get(PendingQuote,event_id) is None

@pytest.mark.parametrize('side,limit,trigger,slippage,expected',[
    ('BUY',90,80,0,80),('SELL',110,120,0,120),
    ('BUY',90,80,5000,90),('SELL',110,120,5000,110),
])
def test_queued_limit_replay_uses_trigger_receipt_and_respects_limit(db,side,limit,trigger,slippage,expected):
    trade=enter(db,side=side,lots=1,order_type='LIMIT',price=limit)['trades'][0];c=contract()
    assert trade['status']=='pending'
    engine.update_settings(db,{'slippage_bps':slippage})
    at=iso_now()
    first=engine.record_quote(db,{'broker':'kotak','instrument_id':c['id'],'ltp':trigger,'received_at':at,'demo':True},process=False)
    second=engine.record_quote(db,{'broker':'kotak','instrument_id':c['id'],'ltp':100,'received_at':iso_now(),'demo':True},process=False)
    db.commit();db.expire_all()  # Simulate restart after durable receipts, before processing.
    assert engine.recover_pending_quotes(db)==2
    db.commit();result=db.get(Trade,trade['id']).data;leg=result['legs'][0]
    assert leg['entry_price']==expected and leg['entry_at']==at
    assert leg['current_price']==100
    assert leg['slippage_cost']==abs(expected-trigger)*leg['lot_size']
    fill=db.scalar(select(Event).where(Event.trade_id==trade['id'],Event.kind=='limit_filled'))
    assert fill.data['quote_event_id']==first and fill.timestamp==at
    assert db.get(PendingQuote,first) is None and db.get(PendingQuote,second) is None

@pytest.mark.parametrize('side,kind,threshold,trigger,expected',[
    ('BUY','stop_loss',90,80,79.5*.99),('BUY','target',130,140,139.5*.99),
    ('SELL','stop_loss',110,120,120.5*1.01),('SELL','target',70,60,60.5*1.01),
])
def test_queued_stop_and_target_replay_fill_from_trigger_bid_ask(db,side,kind,threshold,trigger,expected):
    trade=enter(db,side=side,lots=1)['trades'][0];c=contract();leg_id=trade['legs'][0]['id']
    action(db,trade,'edit',leg_id=leg_id,**{kind:threshold})
    engine.update_settings(db,{'fill_basis':'bid_ask','slippage_bps':100})
    at=iso_now()
    first=engine.record_quote(db,{'broker':'kotak','instrument_id':c['id'],'ltp':trigger,'bid':trigger-.5,'ask':trigger+.5,'received_at':at,'demo':True},process=False)
    second=engine.record_quote(db,{'broker':'kotak','instrument_id':c['id'],'ltp':100,'bid':99.5,'ask':100.5,'received_at':iso_now(),'demo':True},process=False)
    db.commit();db.expire_all()
    assert engine.recover_pending_quotes(db)==2
    db.commit();result=db.get(Trade,trade['id']).data;leg=result['legs'][0]
    assert result['status']=='closed'
    assert leg['exit_price']==pytest.approx(expected) and leg['exit_at']==at
    exit_event=db.scalar(select(Event).where(Event.trade_id==trade['id'],Event.kind==kind))
    assert exit_event.data['quote_event_id']==first and exit_event.timestamp==at
    assert db.get(PendingQuote,first) is None and db.get(PendingQuote,second) is None

def test_mcq_money_multiplier_is_used_in_payoff_and_ledger(db):
    leg={'underlying':'GOLD','exchange':'MCX','expiry':'2027-01-01','lot_size':1,'price_multiplier':100,'lots':2,'strike':75000,'entry_price':100,'side':'BUY','option_type':'CE'}
    assert leg_payoff(leg,75200)==20000
    trade=enter(db,lots=2)['trades'][0]
    row=db.get(Trade,trade['id']);data=copy.deepcopy(row.data);data['legs'][0]['price_multiplier']=100;row.data=data;db.commit()
    quote(db,price=101);db.commit()
    assert db.get(Trade,trade['id']).data['unrealized_pnl']==10000

def test_payoff_rejects_nonfinite_or_invalid_contracts():
    leg={'underlying':'NIFTY','expiry':'2027-01-01','lot_size':50,'lots':1,'strike':24000,'entry_price':100,'side':'BUY','option_type':'CE'}
    with pytest.raises(ValueError):strategy_risk([{**leg,'entry_price':float('inf')}])
    with pytest.raises(ValueError):strategy_risk([{**leg,'side':'INVALID'}])
    with pytest.raises(ValueError):strategy_risk([{**leg,'lots':1.5}])

def test_calendar_payoff_unsupported_but_standalone_margin_not_zero(db):
    trade=enter(db,lots=1)['trades'][0]
    current=copy.deepcopy(db.get(Trade,trade['id']).data)
    short=copy.deepcopy(current['legs'][0])
    short.update({'id':str(uuid.uuid4()),'side':'SELL','expiry':'2099-12-31','entry_price':80})
    current['legs'].append(short)
    engine.recalculate(current)
    units=current['legs'][0]['lot_size']
    expected=(100+current['spot']*.2)*units
    assert current['risk']['supported'] is False
    assert current['risk']['points']==[]
    assert current['margin_estimate']==pytest.approx(expected)
    assert current['risk']['estimated_initial_margin']==pytest.approx(expected)
    assert current['risk']['hedge_benefit']==0
    assert current['risk']['net_premium']==-20*units
    assert 'No cross-expiry' in current['risk']['assumptions']

def test_mixed_underlying_margin_uses_each_leg_reference_and_multiplier():
    legs=[{'underlying':'NIFTY','expiry':'2027-01-01','lot_size':50,'lots':1,'strike':24000,'entry_price':100,'side':'SELL','option_type':'CE'},
          {'underlying':'GOLD','expiry':'2027-02-01','lot_size':1,'lots':2,'strike':75000,'spot':76000,'entry_price':100,'side':'SELL','option_type':'CE','price_multiplier':100}]
    risk=strategy_risk(legs,spot=25000)
    assert risk['supported'] is False
    assert risk['estimated_margin']==24000*.2*50+76000*.2*200
    assert risk['hedge_benefit']==0
    assert risk['net_premium']==100*50+100*200

def test_today_pnl_uses_ist_midnight_baseline(db):
    from zoneinfo import ZoneInfo
    trade=enter(db,lots=1)['trades'][0]
    midnight=datetime.now(ZoneInfo('Asia/Kolkata')).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    row=db.get(Trade,trade['id']);data=copy.deepcopy(row.data);data['created_at']=(midnight-timedelta(days=1)).isoformat(timespec='microseconds');row.data=data
    before=(midnight-timedelta(seconds=1)).isoformat(timespec='microseconds')
    db.add(Timeline(trade_id=trade['id'],timestamp=before,data={'net_pnl':500}));db.commit()
    quote(db,price=120);db.commit()
    assert engine.dashboard(db)['summary']['today_pnl']==500

def test_demo_metrics_never_contaminate_market_data_mode(db):
    enter(db)
    assert engine.dashboard(db)['summary']['active_trades']==1
    engine.update_settings(db,{'demo_mode':False});db.commit()
    data=engine.dashboard(db)
    assert data['trades']==[] and data['summary']['net_pnl']==0
    assert data['summary']['active_trades']==0 and data['quotes']==[]

def test_portfolio_chart_includes_closed_trade_baseline(db):
    closed=enter(db,lots=1)['trades'][0];quote(db,price=120);action(db,closed,'exit_strategy')
    active=enter(db,lots=1)['trades'][0]
    # Force older closed history out of the bounded 250-row window.
    for i in range(260):
        engine.take_snapshots(db)
    db.commit()
    data=engine.dashboard(db)
    assert data['portfolio_timeline'][-1]['net_pnl']==data['summary']['net_pnl']

def test_real_lifespan_seeds_and_websocket_ticket_is_single_use(tmp_path,monkeypatch):
    from app import main,reporting,brokers,instruments
    from starlette.websockets import WebSocketDisconnect
    sql=create_engine(f'sqlite:///{(tmp_path/"lifespan.db").as_posix()}',connect_args={'check_same_thread':False})
    configure_sqlite(sql);factory=sessionmaker(sql,expire_on_commit=False)
    sample_catalog=ContractCatalog(tmp_path/'none.json')
    class OfflineFeeds:
        def __init__(self,*args,**kwargs):pass
        async def start(self,*args):pass
        async def stop(self):pass
        def statuses(self):return {}
    async def no_report_worker(stop):await stop.wait()
    monkeypatch.setattr(main,'db_engine',sql);monkeypatch.setattr(main,'SessionLocal',factory)
    monkeypatch.setattr(brokers,'FeedManager',OfflineFeeds)
    monkeypatch.setattr(instruments,'ContractCatalog',lambda:sample_catalog)
    monkeypatch.setattr(reporting,'reporting_loop',no_report_worker)
    monkeypatch.setenv('ENVIRONMENT','development');monkeypatch.delenv('SETUP_TOKEN',raising=False);monkeypatch.delenv('OWNER_TOTP_SECRET',raising=False)
    def isolated_db():
        with factory() as session:yield session
    main.app.dependency_overrides[get_db]=isolated_db
    try:
        with TestClient(main.app) as client:
            setup=client.post('/api/auth/setup',json={'password':'isolated seed password'})
            assert setup.status_code==200
            csrf=setup.json()['csrf_token'];headers={'x-csrf-token':csrf}
            data=client.get('/api/dashboard').json()
            assert len(data['trades'])==4 and data['summary']['active_trades']==3
            closed=next(t for t in data['trades'] if t['status']=='closed')
            assert closed['mfe']==2000 and closed['mae']==-1900 and closed['net_pnl']==1400
            assert all(q['demo'] for q in data['quotes'])
            ticket=client.post('/api/ws-ticket',headers=headers).json()['ticket']
            with client.websocket_connect(f'/ws?ticket={ticket}') as ws:
                assert ws.receive_json()['type']=='dashboard'
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f'/ws?ticket={ticket}'):pass
            assert client.patch('/api/settings',json={'demo_mode':False},headers=headers).status_code==200
            assert client.get('/api/dashboard').json()['trades']==[]
        # A later startup generates a different synthetic expiry catalog. The
        # persisted, still-unexpired sample contracts must be recovered before
        # pending quote processing or any background practice revaluation.
        from app.instruments import demo_contracts,IST
        first_leg=closed['legs'][0]
        next_catalog=ContractCatalog(tmp_path/'next-startup.json')
        next_catalog._demo=demo_contracts(datetime.now(IST).date()+timedelta(days=1))
        assert next_catalog.find(first_leg['instrument_id'],'kotak') is None
        monkeypatch.setattr(instruments,'ContractCatalog',lambda:next_catalog)
        with TestClient(main.app) as client:
            assert client.get('/api/health').status_code==200
            recovered=next_catalog.find(first_leg['instrument_id'],'kotak')
            assert recovered and recovered['demo'] is True and recovered['restored_from_trade'] is True
            assert recovered['lot_size']==first_leg['lot_size']
            assert next_catalog.all(demo=False)==[]
    finally:
        main.app.dependency_overrides.clear();sql.dispose()

def test_verified_database_backup_restore(db,tmp_path):
    from app.maintenance import backup,restore,read_backup
    trade=enter(db)['trades'][0]
    backup_file=tmp_path/'journal.json.gz'
    result=backup(backup_file,db.bind)
    assert result['size']>0
    assert len(read_backup(backup_file)['tables']['trades'])==1
    with pytest.raises(ValueError):restore(backup_file,db.bind)
    from sqlalchemy import delete
    db.execute(delete(Trade));db.commit()
    assert db.scalar(select(func.count()).select_from(Trade))==0;db.commit()
    restored=restore(backup_file,db.bind,server_stopped=True)
    assert restored['pre_restore_backup']
    db.expire_all();assert db.get(Trade,trade['id']) is not None
    with pytest.raises(FileExistsError):backup(backup_file,db.bind)


def test_compact_policy_updates_legacy_default_without_deleting_records(db):
    from app.models import Setting
    enter(db)
    historical=db.scalar(select(func.count()).select_from(QuoteEvent))
    row=db.get(Setting,'app');row.value={'logging_mode':'every_update','demo_mode':True}
    db.commit();engine.clear_cache(db)
    assert engine.get_settings(db)['logging_mode']=='full_history'
    assert db.scalar(select(func.count()).select_from(QuoteEvent))==historical


def test_compact_marks_keep_extrema_times_without_tick_history(db):
    from app.models import LatestQuote,CompactQuoteReceipt
    engine.update_settings(db,{'logging_mode':'summary'})
    trade=enter(db,lots=1)['trades'][0]
    low=iso_now();quote(db,price=80,received_at=low);db.commit()
    high=iso_now();quote(db,price=140,received_at=high);db.commit()
    quote(db,price=128);db.commit()
    for _ in range(3):engine.take_snapshots(db)
    result=action(db,trade,'exit_strategy')['trade']
    assert result['mfe']==2000 and result['mfe_at']==high
    assert result['mae']==-1000 and result['mae_at']==low
    assert result['net_pnl']==1400
    assert db.scalar(select(func.count()).select_from(Timeline))==0
    assert db.scalar(select(func.count()).select_from(QuoteEvent))==0
    assert db.scalar(select(func.count()).select_from(PendingQuote))==0
    assert db.scalar(select(func.count()).select_from(CompactQuoteReceipt))==0
    assert db.scalar(select(func.count()).select_from(LatestQuote))==1
    job=db.scalar(select(ExportJob).where(ExportJob.trade_id==trade['id']))
    assert job.payload['trade']['mfe']==2000 and set(job.payload)=={'format','trade'}


def test_compact_receipt_replay_and_current_mark_survive_restart(db):
    from app.models import LatestQuote
    engine.update_settings(db,{'logging_mode':'summary'})
    trade=enter(db,lots=1,order_type='LIMIT',price=90)['trades'][0];c=contract()
    at=iso_now()
    first=engine.record_quote(db,{'broker':'kotak','instrument_id':c['id'],'ltp':80,'received_at':at,'demo':True},process=False)
    second=engine.record_quote(db,{'broker':'kotak','instrument_id':c['id'],'ltp':100,'received_at':iso_now(),'demo':True},process=False)
    db.commit();db.expire_all();engine.clear_cache(db)
    assert engine.recover_pending_quotes(db)==2
    db.commit();engine.clear_cache(db)
    result=db.get(Trade,trade['id']).data
    assert result['legs'][0]['entry_price']==80 and result['legs'][0]['entry_at']==at
    assert result['legs'][0]['current_price']==100
    assert db.get(QuoteEvent,first) is None and db.get(QuoteEvent,second) is None
    latest=engine.latest_quote(db,c['id'],'kotak')
    assert latest['ltp']==100 and latest['event_id']==second
    third=quote(db,c,110);db.commit()
    assert third>second>first
    assert db.scalar(select(func.count()).select_from(LatestQuote))==1
    assert db.get(Trade,trade['id']).data['legs'][0]['current_price']==110


def test_compact_target_observes_mark_before_exit_costs(db):
    engine.update_settings(db,{'logging_mode':'summary'})
    trade=enter(db,lots=1)['trades'][0]
    action(db,trade,'edit',leg_id=trade['legs'][0]['id'],target=130)
    engine.update_settings(db,{'fill_basis':'bid_ask','brokerage_per_order':20})
    at=iso_now();quote(db,price=140,bid=139,ask=141,received_at=at);db.commit()
    result=db.get(Trade,trade['id']).data
    assert result['status']=='closed' and result['net_pnl']==1930
    assert result['mfe']==2000 and result['mfe_at']==at
    recorded=db.scalar(select(Event).where(Event.trade_id==trade['id'],Event.kind=='target'))
    assert recorded.data['trigger_quote']['ltp']==140
    assert recorded.data['trigger_quote']['bid']==139


def test_compact_repeated_dashboard_does_not_reread_history_or_ledger(db):
    from sqlalchemy import event as sql_event
    engine.update_settings(db,{'logging_mode':'summary'})
    enter(db,lots=1);quote(db,price=120);db.commit()
    engine.dashboard(db)
    statements=[]
    def seen(connection,cursor,statement,parameters,context,executemany):statements.append(statement)
    sql_event.listen(db.bind,'before_cursor_execute',seen)
    try:
        for _ in range(5):
            data=engine.dashboard(db)
            assert data['summary']['net_pnl']==1000
        assert statements==[]
    finally:sql_event.remove(db.bind,'before_cursor_execute',seen)


def test_compact_failed_valuation_retains_raw_receipt(db,monkeypatch):
    engine.update_settings(db,{'logging_mode':'summary'})
    trade=enter(db,lots=1)['trades'][0]
    def unavailable(*args,**kwargs):raise RuntimeError('test valuation failure')
    monkeypatch.setattr(engine,'recalculate',unavailable)
    receipt=quote(db,price=120);db.commit()
    assert db.get(QuoteEvent,receipt).data['ltp']==120
    assert db.get(Trade,trade['id']).data['valuation_error']


def test_compact_quote_rollback_restores_cache_to_committed_values(db):
    engine.update_settings(db,{'logging_mode':'summary'})
    trade=enter(db,lots=1)['trades'][0]
    quote(db,price=110);db.commit()
    quote(db,price=80);db.rollback()
    assert engine.dashboard(db)['summary']['net_pnl']==500
    assert engine.latest_quote(db,contract()['id'],'kotak')['ltp']==110
    result=db.get(Trade,trade['id']).data
    assert result['mfe']==500 and result['mae']==0


def test_compact_day_rollover_uses_last_observed_mark_without_timelines(db):
    from zoneinfo import ZoneInfo
    engine.update_settings(db,{'logging_mode':'summary'})
    trade=enter(db,lots=1)['trades'][0]
    midnight=datetime.now(ZoneInfo('Asia/Kolkata')).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    previous=(midnight-timedelta(seconds=1)).isoformat(timespec='microseconds')
    yesterday=(midnight-timedelta(days=1)).astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
    row=db.get(Trade,trade['id']);value=copy.deepcopy(row.data)
    value.update({'created_at':previous,'updated_at':previous,'net_pnl':500,
                  '_daily_pnl':{'day':yesterday,'baseline':0,'complete':True}})
    value['legs'][0]['current_price']=110;row.data=value;db.commit()
    quote(db,price=120);db.commit()
    data=engine.dashboard(db)
    assert data['summary']['today_pnl']==500 and data['summary']['today_pnl_complete']
    assert db.scalar(select(func.count()).select_from(Timeline))==0


def test_removed_broker_history_remains_readable_but_new_trade_schema_rejects_it(db):
    from app.schemas import TradeInput
    from pydantic import ValidationError
    result=enter(db)['trades'][0]
    closed=action(db,result,'exit_strategy')['trade']
    row=db.get(Trade,closed['id'])
    row.broker='dhan';row.data={**row.data,'broker':'dhan'};db.commit()
    history=engine.list_trades(db)
    assert any(t['id']==closed['id'] and t['broker']=='dhan' and t['status']=='closed' for t in history)
    c=contract()
    with pytest.raises(ValidationError):
        TradeInput(name='Disabled broker',underlying='NIFTY',brokers=['dhan'],
                   legs=[{'instrument_id':c['id'],'side':'BUY','lots':1}],idempotency_key='legacy-broker-guard',confirmed=True)


def test_kotak_quote_executes_while_retired_broker_cannot_create_a_copy(db):
    c=contract('kotak')
    quote(db,c,100,'kotak')
    with pytest.raises(ValueError):quote(db,c,123,'fivepaisa')
    result=engine.create_trades(db,{'name':'Active broker boundary','underlying':'NIFTY','brokers':['kotak','fivepaisa'],
        'legs':[{'instrument_id':c['id'],'side':'BUY','lots':1,'order_type':'MARKET','enabled':True}],
        'idempotency_key':'kotak-retired-boundary','confirmed':True})
    db.commit()
    assert {t['broker']:t['legs'][0]['entry_price'] for t in result['trades']}=={'kotak':100}
    retired=next(item for item in result['results'] if item['broker']=='fivepaisa')
    assert retired['success'] is False and 'Kotak Neo only' in retired['error']
    assert db.scalar(select(func.count()).select_from(Trade))==1


def test_expired_option_owner_settlement_can_close_at_zero_intrinsic(db):
    original=enter(db,lots=1)['trades'][0]
    stored=copy.deepcopy(db.get(Trade,original['id']).data)
    stored['legs'][0]['expiry']='2026-01-01'
    engine.store_trade(db,stored);db.commit()
    closed=action(db,stored,'settle_expiry',leg_id=stored['legs'][0]['id'],price=stored['legs'][0]['strike']-1)['trade']
    assert closed['status']=='closed' and closed['legs'][0]['exit_price']==0
    assert closed['realized_pnl']==-100*closed['legs'][0]['lot_size']
    assert closed['legs'][0]['settlement_verified_by_broker'] is False


def test_expiry_action_rejects_a_contract_that_has_not_expired(db):
    original=enter(db)['trades'][0]
    stored=copy.deepcopy(db.get(Trade,original['id']).data)
    stored['legs'][0]['expiry']='2099-12-31'
    engine.store_trade(db,stored);db.commit()
    with pytest.raises(HTTPException):action(db,stored,'settle_expiry',leg_id=stored['legs'][0]['id'],price=25000)
