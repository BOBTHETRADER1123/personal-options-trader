"""Cash-equity paper limits using isolated SQLite and fake market observations.

No owner data, broker connection, real-order API or exchange calendar is used.
"""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import sessionmaker
from app import engine
from app.database import Base,configure_sqlite,iso_now
from app.instruments import ContractCatalog,IST
from app.models import Trade,Event


@pytest.fixture
def cash(tmp_path,monkeypatch):
    sql=create_engine(f'sqlite:///{tmp_path/"cash-limit.db"}',connect_args={'check_same_thread':False})
    configure_sqlite(sql,'DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False)
    stamp=datetime.now(timezone.utc)
    identifier='kotak:nse_cm:12345:INE002A01018'
    row={'id':identifier,'instrument_id':identifier,'broker':'kotak','token':'12345',
         'underlying':'TESTCASH','name':'Isolated Cash Equity','symbol':'TESTCASH-EQ',
         'exchange':'NSE','segment':'nse_cm','expiry':None,'strike':0.,'option_type':'EQ',
         'instrument_type':'stock','isin':'INE002A01018','lot_size':1,'price_multiplier':1.,
         'tick_size':.05,'multiplier_verified':True,'demo':False,'source':'broker_master',
         'master_active':True,'master_source_date':stamp.astimezone(IST).date().isoformat(),
         'master_refresh_at':stamp.isoformat()}
    catalog=ContractCatalog(tmp_path/'absent-master.json');catalog.stock_rows=(row,);catalog.revision+=1
    previous=engine.catalog;engine.clear_cache();engine.set_catalog(catalog)
    session={'known':True,'open':True,'reason':'isolated verified-open fixture'}
    monkeypatch.setattr(engine,'ExchangeCalendar',lambda:SimpleNamespace(state=lambda *_:dict(session)))
    with factory() as db:
        engine.update_settings(db,{'demo_mode':False,'logging_mode':'full_history','fill_basis':'bid_ask',
            'slippage_bps':0.,'stale_after_seconds':10,'unlimited_paper_buying_power':True,
            'brokerage_per_order':20.,'charges_percent':.05})
        db.commit()
    f=SimpleNamespace(factory=factory,row=row,catalog=catalog,session=session)
    mark(f)
    yield f
    engine.clear_cache();engine.set_catalog(previous);sql.dispose()


def quote(f,*,ltp=100.,bid=99.9,ask=100.1,**changes):
    return {**f.row,'ltp':ltp,'bid':bid,'ask':ask,'received_at':iso_now(),
            'exchange_timestamp':iso_now(),'exchange_timestamp_basis':'last_update_time',
            'source':'kotak',**changes}


def mark(f,**changes):
    value=quote(f,**changes)
    with f.factory() as db:
        engine.cache_quote(db,value,persist=True);db.commit()
    return value


def submit(f,*,limit=95.,side='BUY',order_type='LIMIT',lots=10,key='cash-limit-initial'):
    leg={'instrument_id':f.row['id'],'side':side,'lots':lots,'order_type':order_type}
    if order_type=='LIMIT':leg['price']=limit
    payload={'name':'Cash equity paper order','underlying':'TESTCASH','brokers':['kotak'],
             'legs':[leg],'idempotency_key':key,'confirmed':True}
    with f.factory() as db:
        result=engine.create_trades(db,payload);db.commit()
    return result


def opened(f,**kwargs):
    result=submit(f,**kwargs)
    assert len(result['trades'])==1,result
    return result['trades'][0]


def incoming(f,**changes):
    value=quote(f,**changes)
    with f.factory() as db:
        engine.record_quote(db,value);db.commit()
    return value


def stored(f,identifier):
    with f.factory() as db:return db.get(Trade,identifier).data


def fill_events(f,identifier):
    with f.factory() as db:
        return db.scalar(select(func.count()).select_from(Event).where(Event.trade_id==identifier,Event.kind=='limit_filled'))


def test_cash_market_buy_still_uses_share_quantity(cash):
    t=opened(cash,order_type='MARKET',lots=7)
    leg=t['legs'][0]
    assert t['status']=='open' and t['category']=='stocks'
    assert leg['expiry'] is None and leg['lot_size']==1 and leg['open_lots']==7
    assert leg['entry_price']==pytest.approx(100.1)


def test_crossed_cash_buy_limit_fills_at_available_ask(cash):
    t=opened(cash,limit=101.)
    leg=t['legs'][0]
    assert t['status']=='open' and leg['filled_lots']==10
    assert leg['entry_price']==pytest.approx(100.1) and leg['entry_price']<=101.
    assert leg['charges']>0 and leg['expiry'] is None


def test_crossed_cash_limit_caps_adverse_slippage(cash):
    with cash.factory() as db:
        engine.update_settings(db,{'slippage_bps':1000.});db.commit()
    t=opened(cash,limit=100.2)
    assert t['status']=='open'
    assert t['legs'][0]['entry_price']==pytest.approx(100.2)
    assert t['legs'][0]['entry_price']<=t['legs'][0]['limit_price']


def test_noncrossed_cash_limit_has_no_fill_entry_charge_or_position(cash):
    t=opened(cash,limit=95.)
    leg=t['legs'][0]
    assert t['status']=='pending' and leg['status']=='pending'
    assert leg['filled_lots']==leg['open_lots']==0
    assert leg['entry_at'] is None and leg['charges']==0
    assert t['charges']==0 and t['net_premium']==0


def test_pending_cash_limit_restores_then_fills_once_from_fresh_quote(cash):
    t=opened(cash,limit=95.);engine.clear_cache()
    crossed=incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    filled=stored(cash,t['id']);leg=filled['legs'][0]
    assert filled['status']=='open' and leg['filled_lots']==leg['open_lots']==10
    assert leg['entry_price']==pytest.approx(94.9)
    assert leg['entry_at']==crossed['received_at'] and fill_events(cash,t['id'])==1
    charges=leg['charges']
    incoming(cash,ltp=94.7,bid=94.6,ask=94.8)
    same=stored(cash,t['id'])
    assert fill_events(cash,t['id'])==1 and same['legs'][0]['charges']==charges
    assert same['legs'][0]['filled_lots']==10


@pytest.mark.parametrize('known',[True,False],ids=['closed-session','unknown-session'])
def test_pending_cash_limit_waits_without_valuation_failure_outside_known_session(cash,known):
    t=opened(cash,limit=95.)
    cash.session.update(known=known,open=False,reason='isolated unavailable session')
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    waiting=stored(cash,t['id'])
    assert waiting['status']=='pending' and waiting['legs'][0]['open_lots']==0
    assert waiting['legs'][0]['charges']==0 and not waiting.get('valuation_error')
    assert fill_events(cash,t['id'])==0
    cash.session.update(known=True,open=True)
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    assert stored(cash,t['id'])['status']=='open'


@pytest.mark.parametrize('stale_field',['received_at','exchange_timestamp'])
def test_pending_cash_limit_waits_for_current_receipt_and_exchange_timestamp(cash,stale_field):
    t=opened(cash,limit=95.)
    stale=(datetime.now(timezone.utc)-timedelta(seconds=61)).isoformat()
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9,**{stale_field:stale})
    waiting=stored(cash,t['id'])
    assert waiting['status']=='pending' and waiting['legs'][0]['filled_lots']==0
    assert waiting['legs'][0]['charges']==0 and not waiting.get('valuation_error')
    assert fill_events(cash,t['id'])==0
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    assert stored(cash,t['id'])['status']=='open'


def test_pending_cash_limit_can_be_cancelled_and_never_fills_afterwards(cash):
    t=opened(cash,limit=95.)
    with cash.factory() as db:
        result=engine.apply_action(db,t['id'],{'action':'cancel_pending','confirmed':True,
            'idempotency_key':'cancel-cash-limit','leg_id':t['legs'][0]['id']})
        db.commit()
    assert result['trade']['status']=='cancelled'
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    cancelled=stored(cash,t['id'])
    assert cancelled['status']=='cancelled' and cancelled['legs'][0]['filled_lots']==0
    assert cancelled['charges']==0 and fill_events(cash,t['id'])==0


def test_pending_cash_limit_rechecks_simulated_cash_before_fill(cash):
    t=opened(cash,limit=95.)
    with cash.factory() as db:
        engine.update_settings(db,{'unlimited_paper_buying_power':False,'paper_capital':100.});db.commit()
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    waiting=stored(cash,t['id'])
    assert waiting['status']=='pending' and waiting['legs'][0]['buying_power_blocked']
    assert waiting['charges']==0 and fill_events(cash,t['id'])==0
    with cash.factory() as db:
        engine.update_settings(db,{'paper_capital':10000.});db.commit()
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    assert stored(cash,t['id'])['status']=='open' and fill_events(cash,t['id'])==1


@pytest.mark.parametrize('order_type',['MARKET','LIMIT'])
def test_new_cash_sell_never_opens_a_naked_short(cash,order_type):
    result=submit(cash,side='SELL',order_type=order_type)
    assert result['trades']==[] and result['results'][0]['success'] is False
    assert 'existing paper stock position' in result['results'][0]['error']
    with cash.factory() as db:assert db.scalar(select(func.count()).select_from(Trade))==0


@pytest.mark.parametrize('state',[{'known':True,'open':False},{'known':False,'open':False},{'known':False,'open':True}])
def test_cash_limit_creation_requires_verified_open_session(cash,state):
    cash.session.update(state)
    result=submit(cash,limit=95.)
    assert result['trades']==[] and result['results'][0]['success'] is False
    with cash.factory() as db:assert db.scalar(select(func.count()).select_from(Trade))==0


def test_cash_limit_rejects_a_price_outside_the_official_tick_grid(cash):
    result=submit(cash,limit=.03)
    assert result['trades']==[] and result['results'][0]['success'] is False
    with cash.factory() as db:assert db.scalar(select(func.count()).select_from(Trade))==0


def test_cash_limit_accepts_exact_decimal_tick_multiple_and_caps_slippage(cash):
    with cash.factory() as db:
        engine.update_settings(db,{'slippage_bps':1000.});db.commit()
    t=opened(cash,limit=100.15)
    assert t['status']=='open' and t['legs'][0]['entry_price']==pytest.approx(100.15)
    assert t['legs'][0]['entry_price']<=100.15


@pytest.mark.parametrize('tick',[None,0.,-.05,float('nan'),float('inf'),True,'bad','__absent__'],
    ids=['none','zero','negative','nan','infinity','boolean','bad-string','absent-field'])
def test_cash_limit_rejects_unverified_or_invalid_official_tick(cash,tick):
    if tick=='__absent__':cash.row.pop('tick_size')
    else:cash.row['tick_size']=tick
    cash.catalog.revision+=1
    result=submit(cash,limit=95.)
    assert result['trades']==[] and result['results'][0]['success'] is False
    with cash.factory() as db:assert db.scalar(select(func.count()).select_from(Trade))==0


def test_pending_cash_limit_rechecks_changed_official_tick_before_fill(cash):
    t=opened(cash,limit=95.)
    cash.row['tick_size']=.03;cash.catalog.revision+=1
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    waiting=stored(cash,t['id'])
    assert waiting['status']=='pending' and waiting['legs'][0]['filled_lots']==0
    assert waiting['legs'][0]['charges']==0 and not waiting.get('valuation_error')
    assert fill_events(cash,t['id'])==0
    cash.row['tick_size']=.05;cash.catalog.revision+=1
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9)
    filled=stored(cash,t['id'])
    assert filled['status']=='open' and fill_events(cash,t['id'])==1
    assert filled['legs'][0]['entry_price']<=95.


def test_genuine_pending_cash_limit_is_independent_of_dashboard_sample_view(cash):
    t=opened(cash,limit=95.)
    with cash.factory() as db:
        engine.update_settings(db,{'demo_mode':True});db.commit()
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9,demo=True)
    assert stored(cash,t['id'])['status']=='pending' and fill_events(cash,t['id'])==0
    incoming(cash,ltp=94.8,bid=94.7,ask=94.9,demo=False)
    filled=stored(cash,t['id'])
    assert filled['status']=='open' and filled['legs'][0]['filled_lots']==10
    assert filled['legs'][0]['entry_price']<=95. and fill_events(cash,t['id'])==1
