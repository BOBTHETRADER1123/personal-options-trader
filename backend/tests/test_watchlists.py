"""Isolated watchlist/API/ledger tests; no broker network or owner data."""
import asyncio
import copy
import io
import secrets
import time
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException,WebSocketDisconnect
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine,select,func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from app import engine,main,reporting
from app.database import Base,configure_sqlite,get_db,iso_now
from app.instruments import ContractCatalog,IST
from app.models import Setting,Trade,Event,PnlSnapshot,UserSession
from app.security import COOKIE,digest
from app.trade_categories import instrument_category,trade_category,category_summary
from app.watchlists import WatchlistService,KEY,LIMITS
from app.brokers.kotak import parse_sfeed


def contract(token,category='fno',**changes):
    now=datetime.now(timezone.utc);identifier=f'kotak:{token}'
    stock=category=='stocks';mcx=category=='commodities'
    row={'id':identifier,'instrument_id':identifier,'broker':'kotak','token':str(token),'underlying':'RELIANCE' if stock else 'GOLD' if mcx else 'NIFTY',
         'name':'Reliance Industries' if stock else 'Gold futures' if mcx else 'Nifty option','exchange':'MCX' if mcx else 'NSE',
         'segment':'mcx_fo' if mcx else 'nse_cm' if stock else 'nse_fo','symbol':f'RELIANCE-EQ' if stock else f'GOLD 2030 FUT {token}' if mcx else f'NIFTY 2030 25000 CE {token}',
         'expiry':None if stock else '2030-11-30','strike':0 if stock or mcx else 25000.,'option_type':'EQ' if stock else 'FUT' if mcx else 'CE',
         'lot_size':1 if stock else 10 if mcx else 65,'tick_size':.05,'price_multiplier':1,'demo':False,'source':'broker_master',
         'master_active':True,'master_source_date':now.astimezone(IST).date().isoformat(),'master_refresh_at':now.isoformat()}
    if stock:row.update(instrument_type='stock',isin='INE002A01018')
    if mcx:row['monetary_conversion_source']='offline verified fixture'
    return {**row,**changes}


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    sql=create_engine(f'sqlite:///{tmp_path/"watchlists.db"}',connect_args={'check_same_thread':False})
    configure_sqlite(sql,'DELETE');Base.metadata.create_all(sql);factory=sessionmaker(sql,expire_on_commit=False)
    token=secrets.token_urlsafe(48)
    with factory() as db:
        db.add(UserSession(token_hash=digest(token),csrf_token='offline-csrf',expires_at=time.time()+600))
        engine.update_settings(db,{'demo_mode':False,'logging_mode':'full_history','unlimited_paper_buying_power':True});db.commit()
    previous=engine.catalog;catalog=ContractCatalog(tmp_path/'empty.json')
    rows=[contract(101),contract(201,'stocks'),contract(301,'commodities')]
    catalog.merge_live_contracts([rows[0],rows[2]]);catalog.stock_rows=[rows[1]];catalog.revision+=1
    engine.set_catalog(catalog)
    monkeypatch.setattr(main,'catalog',catalog);monkeypatch.setattr(main,'watchlist_service',WatchlistService())
    monkeypatch.setattr(main,'database_ready',True);monkeypatch.setattr(main,'collector_lease',None)
    monkeypatch.setattr(main,'write_lock',asyncio.Lock());monkeypatch.setattr(main,'_quote_selection_requests',{})
    monkeypatch.setattr(main,'feed_manager',None);monkeypatch.setattr(main,'feed_status',{'kotak':{'status':'connected'}})
    calls=[]
    async def refresh(db,**kwargs):
        # Mirror the real feed refresh: ledger demand reads can open a transaction
        # after commit, and refresh ends it before any socket/subscription wait.
        main.option_chain.finish_read_transaction(db)
        assert not db.in_transaction();calls.append(main.requested_quote_selection_ids())
    monkeypatch.setattr(main,'refresh_selected_feeds',refresh)
    def override():
        with factory() as db:yield db
    main.app.dependency_overrides[get_db]=override
    client=TestClient(main.app)
    yield SimpleNamespace(client=client,factory=factory,catalog=catalog,rows=rows,token=token,calls=calls)
    client.close();main.app.dependency_overrides.clear();engine.set_catalog(previous);sql.dispose()


def signed(f):
    f.client.cookies.set(COOKIE,f.token);f.client.headers['X-CSRF-Token']='offline-csrf';return f.client


def mark(f,row,price=100,**changes):
    quote={**row,'ltp':price,'bid':price-.1,'ask':price+.1,'previous_close':90.,'volume':123,'received_at':iso_now(),'source':'kotak',**changes}
    with f.factory() as db:engine.cache_quote(db,quote,persist=True);db.commit()
    return quote


def save_items(f,ids,list_id='watchlist-1',revision=None):
    return signed(f).patch('/api/watchlists/'+list_id,json={'instrument_ids':ids,**({'revision':revision} if revision is not None else {})})


def test_owner_csrf_and_three_categories_only(fixture):
    f=fixture
    assert f.client.get('/api/watchlists').status_code==401
    f.client.cookies.set(COOKIE,f.token)
    assert f.client.post('/api/watchlists',json={'name':'Private'}).status_code==403
    c=signed(f)
    assert c.get('/api/watchlists/search?category=crypto').status_code==422
    assert c.get('/api/watchlists').json()['revision']==0
    for category,row in zip(('fno','stocks','commodities'),f.rows):
        result=c.get('/api/watchlists/search',params={'category':category,'q':row['underlying']})
        assert result.status_code==200
        assert [item['instrument_id'] for item in result.json()['instruments']]==[row['id']]
    with f.factory() as db:assert db.get(Setting,KEY) is None


def test_definitions_persist_reorder_across_sessions_and_restarts(fixture):
    f=fixture;c=signed(f)
    first=save_items(f,[f.rows[0]['id'],f.rows[1]['id']]).json()
    reordered=save_items(f,[f.rows[1]['id'],f.rows[0]['id']],revision=first['revision']).json()
    assert reordered['watchlists'][0]['items'][0]['instrument_id']==f.rows[1]['id']
    assert c.patch('/api/watchlists/watchlist-1',json={'name':'Renamed','revision':reordered['revision']}).status_code==200
    assert c.post('/api/watchlists',json={'name':'Swing positions'}).status_code==201
    with f.factory() as db:
        snapshot,ids=WatchlistService().snapshot(db,f.catalog,engine.get_settings(db),engine.latest_quote,engine.age_seconds)
        assert snapshot['watchlists'][0]['name']=='Renamed'
        assert snapshot['watchlists'][-1]['name']=='Swing positions'
        assert ids=={f.rows[0]['id'],f.rows[1]['id']}
        assert db.scalar(select(func.count()).select_from(Trade))==0


def test_conflicts_limits_unknown_identifiers_and_duplicates_do_not_save(fixture):
    f=fixture;c=signed(f);row=f.rows[0]
    assert save_items(f,[row['id']]).status_code==200
    revision=c.get('/api/watchlists').json()['revision']
    for body in ({'instrument_ids':['missing']},{'instrument_ids':[row['id']]*2},{'instrument_ids':[row['id']]*51},{'name':'x','revision':revision-1}):
        response=c.patch('/api/watchlists/watchlist-1',json=body)
        assert response.status_code in (409,422)
    assert c.get('/api/watchlists').json()['revision']==revision
    for name in ('','   ','x\nprivate'):
        assert c.post('/api/watchlists',json={'name':name}).status_code==422
    assert c.get('/api/watchlists').json()['revision']==revision


def test_stale_expired_or_recycled_saved_contracts_are_retained_but_blocked(fixture):
    f=fixture;row=f.rows[0];save_items(f,[row['id']])
    old=f.catalog._live[0]
    old['expiry']='2000-01-01';f.catalog.revision+=1
    snapshot=signed(f).get('/api/watchlists').json();item=snapshot['watchlists'][0]['items'][0]
    assert item['instrument_id']==row['id'] and not item['valid']
    assert item['contract']['expiry']=='2030-11-30'
    assert save_items(f,[row['id']]).status_code==200,'Reordering existing invalid history does not delete it'
    old['underlying']='RECYCLED';old['expiry']='2031-01-01';f.catalog.revision+=1
    mark(f,old,999)
    item=signed(f).get('/api/watchlists').json()['watchlists'][0]['items'][0]
    assert not item['valid'] and item['quote'] is None
    stale=contract(999,master_source_date='2000-01-01');f.catalog.merge_live_contracts([stale])
    assert save_items(f,[stale['id']]).status_code==422


def test_only_active_list_subscribes_and_quote_fields_are_safe(fixture):
    f=fixture;first,stock,commodity=f.rows
    quote=mark(f,first,100,open=91.,high=103.,low=89.,raw_message={'secret':'do not expose'},session_id='private-session')
    save_items(f,[first['id']]);save_items(f,[stock['id'],commodity['id']],list_id='watchlist-2')
    response=signed(f).get('/api/watchlists?active_list_id=watchlist-2')
    assert response.status_code==200 and f.calls[-1]=={stock['id'],commodity['id']}
    payload=response.json();q=payload['watchlists'][0]['items'][0]['quote']
    assert q['ltp']==100 and q['change']==10 and q['change_percent']==pytest.approx(100/9)
    assert (q['open'],q['high'],q['low'])==(91,103,89) and q['received_at']==quote['received_at']
    assert 'secret' not in response.text and 'private-session' not in response.text and 'raw_message' not in q
    assert not q['stale']
    mark(f,first,100,received_at=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat())
    assert signed(f).get('/api/watchlists').json()['watchlists'][0]['items'][0]['quote']['stale']


def test_last_watchlist_cannot_be_deleted_and_trades_remain_intact(fixture):
    f=fixture;c=signed(f)
    with f.factory() as db:
        db.add(Trade(id='history-kept',broker='kotak',status='closed',data={'retained':True}));db.commit()
    assert c.delete('/api/watchlists/favourites').status_code==200
    assert c.delete('/api/watchlists/watchlist-2').status_code==200
    assert c.delete('/api/watchlists/watchlist-1').status_code==409
    with f.factory() as db:assert db.get(Trade,'history-kept').data=={'retained':True}


def test_search_is_bounded_and_does_not_scan_unknown_queries(fixture):
    f=fixture;c=signed(f)
    response=c.get('/api/watchlists/search?q=25000').json()
    assert response['selection_required'] and response['instruments']==[]
    extras=[contract(1000+i,symbol=f'NIFTY unmatched {i}') for i in range(21)]
    f.catalog.merge_live_contracts(extras)
    old=LIMITS['search_inspections'];LIMITS['search_inspections']=5
    try:
        response=c.get('/api/watchlists/search?q=NIFTY+no-match').json()
        assert response['selection_required'] and response['truncated']
    finally:LIMITS['search_inspections']=old


def test_watchlist_order_uses_existing_ledger_records_seconds_and_exports(fixture,monkeypatch):
    f=fixture;row=f.rows[0];save_items(f,[row['id']]);mark(f,row)
    c=signed(f);payload={'name':'Watchlist paper call','underlying':row['underlying'],'brokers':['kotak'],
        'legs':[{'instrument_id':row['id'],'side':'BUY','lots':1,'order_type':'MARKET'}],
        'idempotency_key':'offline-watchlist-paper-001','confirmed':True}
    response=c.post('/api/trades',json=payload);assert response.status_code==201
    trade=response.json()['trades'][0];assert trade['category']=='fno'
    calendar=SimpleNamespace(state=lambda *_:{'known':True,'open':True,'reason':'offline verified fixture'})
    with f.factory() as db:
        assert engine.take_snapshots(db,calendar=calendar)['recorded']==1;db.commit()
        assert db.scalar(select(func.count()).select_from(PnlSnapshot).where(PnlSnapshot.trade_id==trade['id']))==1
    mark(f,row,110)
    result=c.post('/api/trades/'+trade['id']+'/actions',json={'action':'exit_strategy','confirmed':True,'idempotency_key':'offline-watchlist-exit-001'})
    assert result.status_code==200 and result.json()['trade']['category']=='fno'
    with f.factory() as db:
        stored=db.get(Trade,trade['id']).data
        assert stored['status']=='closed'
        assert db.scalar(select(func.count()).select_from(reporting.ExportJob).where(reporting.ExportJob.trade_id==trade['id']))==1
        content=reporting.build_workbook({'trade':engine.public_trade(stored)})
        workbook=load_workbook(io.BytesIO(content),read_only=True)
        try:assert 'Trade Summary' in workbook.sheetnames
        finally:workbook.close()
        summaries=engine.dashboard(db)['category_summary']
        assert summaries['fno']['trade_count']==1 and summaries['stocks']['trade_count']==0
        assert summaries['total']['net_pnl']==summaries['fno']['net_pnl']


@pytest.mark.parametrize('action',['add_leg','replace_leg'])
def test_category_cannot_change_through_leg_actions(fixture,action):
    f=fixture;row=f.rows[0];mark(f,row)
    with f.factory() as db:
        opened=engine.create_trades(db,{'name':'Original F&O','underlying':'NIFTY','brokers':['kotak'],
            'legs':[{'instrument_id':row['id'],'side':'BUY','lots':1}], 'idempotency_key':'original-fno-category','confirmed':True})['trades'][0]
        db.commit();before=copy.deepcopy(db.get(Trade,opened['id']).data)
        replacement={**f.rows[2],'underlying':'NIFTY','symbol':'SAME UNDERLYING COMMODITY'}
        f.catalog._live.append(replacement);f.catalog.revision+=1
        with pytest.raises(HTTPException,match='original Stocks'):
            engine.apply_action(db,opened['id'],{'action':action,'leg_id':opened['legs'][0]['id'],
                'leg':{'instrument_id':replacement['id'],'side':'BUY','lots':1},'idempotency_key':'no-cross-category-'+action,'confirmed':True})
        assert db.get(Trade,opened['id']).data==before


def test_categories_are_exclusive_and_total_every_trade_once():
    trades=[{'legs':[contract(1,'stocks')],'status':'open','net_pnl':10,'gross_pnl':12,'charges':2},
            {'legs':[contract(2)],'status':'closed','net_pnl':-4,'gross_pnl':-3,'charges':1},
            {'legs':[contract(3,'commodities')],'status':'open','net_pnl':20,'gross_pnl':23,'charges':3}]
    assert [trade_category(t) for t in trades]==['stocks','fno','commodities']
    summary=category_summary(trades)
    assert set(summary)=={'stocks','fno','commodities','total'}
    assert summary['total']['net_pnl']==26 and summary['total']['trade_count']==3
    assert trade_category({'legs':[contract(1),contract(2,'commodities')]})=='commodities'


def sfeed_row(**changes):
    return {'type':'scrip','exchange_segment':'nse_fo','instrument_token':'101','last_traded_price':100.,
            'buy':[{'price':99.9}],'sell':[{'price':100.1}],**changes}


def test_official_optional_ohlc_fields_are_normalized():
    result=parse_sfeed(sfeed_row(open_price=91.5,high_price=105.,low_price=90.25,close_price=92.),{('nse_fo','101'):contract(101)})
    assert (result['open'],result['high'],result['low'],result['previous_close'])==(91.5,105.,90.25,92.)


def test_missing_optional_ohlc_does_not_invent_prices():
    result=parse_sfeed(sfeed_row(),{('nse_fo','101'):contract(101)})
    assert result['ltp']==100 and all(result[key] is None for key in ('open','high','low'))


def test_invalid_optional_ohlc_does_not_disconnect_valid_quote():
    result=parse_sfeed(sfeed_row(open_price=True,high_price='bad',low_price=-1),{('nse_fo','101'):contract(101)})
    assert result['ltp']==100 and all(result[key] is None for key in ('open','high','low'))


@pytest.mark.parametrize('error',[ValueError('invalid frame'),OperationalError('SQL private=?',{'private':'must-stay-private'},Exception('private database'))])
@pytest.mark.parametrize('close_error',[WebSocketDisconnect(1006),OSError('connection gone')])
def test_disconnected_socket_cleanup_never_leaks_sql(error,close_error,monkeypatch,caplog):
    class Publisher:
        def register(self,*_):return 'registration'
        def unregister(self,key):assert key=='registration'
        async def snapshot(self,*_):raise error
    class Socket:
        headers={};query_params={}
        async def accept(self):pass
        async def close(self,**_):raise close_error
    monkeypatch.setattr(main,'socket_identity',lambda *_:(time.time()+60,'offline-owner'))
    monkeypatch.setattr(main,'_dashboard_publisher',Publisher());monkeypatch.setattr(main,'_viewers',0)
    asyncio.run(main.websocket(Socket()))
    assert main._viewers==0 and 'must-stay-private' not in caplog.text and 'private database' not in caplog.text


def test_search_accepts_exact_compact_trading_symbols_and_cash_suffix(fixture):
    f=fixture;c=signed(f)
    f.catalog._live[0]['symbol']='NIFTY30NOV3025000CE';f.catalog.revision+=1
    for symbol,category,identifier in [('NIFTY30NOV3025000CE','fno',f.rows[0]['id']),('RELIANCE-EQ','stocks',f.rows[1]['id'])]:
        response=c.get('/api/watchlists/search',params={'category':category,'q':symbol})
        assert response.status_code==200
        assert [item['instrument_id'] for item in response.json()['instruments']]==[identifier]


def test_empty_stock_master_explains_explicit_refresh_without_network(fixture):
    f=fixture;f.catalog.stock_rows=[];f.catalog.revision+=1
    result=signed(f).get('/api/watchlists/search?category=stocks&q=RELIANCE').json()
    assert result['instruments']==[] and 'Refresh stock listings' in result['message']


def test_cash_equity_survives_five_years_and_database_cache_restart(fixture,monkeypatch):
    monkeypatch.setattr(engine,'ExchangeCalendar',lambda:SimpleNamespace(state=lambda *_:{'open':True,'known':True}))
    f=fixture;row=f.rows[1];mark(f,row);c=signed(f)
    result=c.post('/api/trades',json={'name':'Long term equity','underlying':row['underlying'],'brokers':['kotak'],
        'legs':[{'instrument_id':row['id'],'side':'BUY','lots':12}],
        'idempotency_key':'five-year-equity','confirmed':True})
    assert result.status_code==201
    assert result.json().get('trades'),result.json()
    original=result.json()['trades'][0];future=datetime.now(timezone.utc)+timedelta(days=5*366)
    class FutureClock(datetime):
        @classmethod
        def now(cls,tz=None):return future.astimezone(tz) if tz else future.replace(tzinfo=None)
    monkeypatch.setattr(engine,'datetime',FutureClock);engine.clear_cache()
    with f.factory() as db:
        restored=engine.list_trades(db,demo=False)[0]
        assert restored['id']==original['id'] and restored['status']=='open' and restored['category']=='stocks'
        leg=restored['legs'][0]
        assert leg['expiry'] is None and leg['open_lots']==12 and leg['lot_size']==1
        assert not leg['expired'] and not leg['settlement_required']
        assert db.get(Trade,original['id']).status=='open'


def test_official_cash_refresh_endpoint_uses_existing_stock_service(fixture,monkeypatch):
    calls=[]
    class Service:
        async def refresh(self,db):calls.append(True);return {'ok':True,'master':{'status':'ready'}}
    monkeypatch.setattr(main,'stock_master',Service())
    result=signed(fixture).post('/api/stocks/master/refresh')
    assert result.status_code==200 and result.json()['ok'] and calls==[True]


def test_deleted_watchlist_on_another_device_falls_back_without_writing(fixture):
    f=fixture;c=signed(f);c.delete('/api/watchlists/favourites')
    revision=c.get('/api/watchlists').json()['revision']
    result=c.get('/api/watchlists?active_list_id=favourites')
    assert result.status_code==200 and result.json()['active_list_id']=='watchlist-1'
    assert result.json()['revision']==revision
