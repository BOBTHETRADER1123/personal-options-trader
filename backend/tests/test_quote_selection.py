"""Owner-only bounded live quote demand; no broker network is used."""
import asyncio
import secrets
import threading
import time
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import engine,main
from app.database import Base,configure_sqlite,get_db
from app.instruments import ContractCatalog
from app.models import Trade,UserSession
from app.security import COOKIE,digest


def contract(broker,token):
    identifier=f'{broker}:MCX:D:{token}'
    return {'id':identifier,'instrument_id':identifier,'broker':broker,'token':str(token),'underlying':'CRUDEOILM',
            'exchange':'MCX','segment':'D','symbol':f'OFFLINE MINI {token}','expiry':'2030-10-31',
            'strike':6000,'option_type':'CE','lot_size':10,'tick_size':.05,'demo':False}


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    sql=create_engine(f'sqlite:///{tmp_path/"quotes.db"}',connect_args={'check_same_thread':False})
    configure_sqlite(sql,'DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False);token=secrets.token_urlsafe(48)
    with factory() as db:
        db.add(UserSession(token_hash=digest(token),csrf_token='offline-csrf',expires_at=time.time()+600));db.commit()
        engine.update_settings(db,{'demo_mode':False,'stale_after_seconds':10});db.commit()
    previous=engine.catalog;catalog=ContractCatalog(tmp_path/'empty-master.json')
    rows=[contract('kotak',1),contract('kotak',101),contract('kotak',2),contract('kotak',102)]
    catalog.merge_live_contracts(rows);engine.set_catalog(catalog)
    monkeypatch.setattr(main,'catalog',catalog);monkeypatch.setattr(main,'database_ready',True)
    monkeypatch.setattr(main,'_quote_selection_requests',{})
    monkeypatch.delenv('FEED_INSTRUMENT_IDS',raising=False)
    calls=[]
    async def refresh(db,force=False):
        assert not db.in_transaction()
        calls.append(main.requested_quote_selection_ids())
    monkeypatch.setattr(main,'refresh_selected_feeds',refresh)
    def override():
        with factory() as db:yield db
    main.app.dependency_overrides[get_db]=override
    c=TestClient(main.app)
    yield SimpleNamespace(client=c,token=token,factory=factory,rows=rows,calls=calls)
    c.close();main.app.dependency_overrides.clear();engine.set_catalog(previous);sql.dispose()


def signed(f):
    f.client.cookies.set(COOKIE,f.token)
    f.client.headers['X-CSRF-Token']='offline-csrf'
    return f.client


def item(row):return {'broker':row['broker'],'instrument_id':row['id']}


def mark(f,row,price,*,stamp=None,**extra):
    value={**row,'ltp':price,'bid':price-.1,'ask':price+.1,'received_at':stamp or datetime.now(timezone.utc).isoformat(),
           'exchange_timestamp':'2026-10-27T10:00:00+00:00','source':row['broker'],**extra}
    with f.factory() as db:engine.cache_quote(db,value,persist=True);db.commit()
    return value


def test_selection_requires_owner_and_csrf(fixture):
    f=fixture;body={'instruments':[item(f.rows[0])]}
    assert f.client.post('/api/quotes/selection',json=body).status_code==401
    f.client.cookies.set(COOKIE,f.token)
    assert f.client.post('/api/quotes/selection',json=body).status_code==403
    assert not f.calls and not main.requested_quote_selection_ids()


def test_selection_is_bounded_and_validates_exact_broker_master(fixture):
    f=fixture;c=signed(f);one=item(f.rows[0])
    for body in ({'instruments':[one]*41},{'instruments':[{'broker':'dhan','instrument_id':'x'}]},
                 {'instruments':[{'broker':'fivepaisa','instrument_id':one['instrument_id']}]},
                 {'instruments':[{'broker':'kotak','instrument_id':'missing'}]}):
        assert c.post('/api/quotes/selection',json=body).status_code==422
    assert not f.calls and not main.requested_quote_selection_ids()


def test_only_requested_broker_quotes_and_public_fields_are_returned(fixture):
    f=fixture;first=mark(f,f.rows[0],12.5,raw_message={'private':'must-not-leave'},access_token='offline-private')
    mark(f,f.rows[1],21.5)
    result=signed(f).post('/api/quotes/selection',json={'instruments':[item(f.rows[0])]*2})
    assert result.status_code==200
    payload=result.json();assert payload['requested']==1 and payload['subscription_ttl_seconds']==30
    assert len(payload['quotes'])==1
    q=payload['quotes'][0]
    assert q['broker']=='kotak' and q['ltp']==12.5 and q['instrument_id']==f.rows[0]['id']
    assert q['received_at']==first['received_at'] and q['exchange_timestamp']==first['exchange_timestamp']
    assert not q['stale'] and q['quote_age_seconds']>=0
    assert 'raw_message' not in q and 'access_token' not in q and 'must-not-leave' not in result.text
    assert f.calls==[{f.rows[0]['id']}]


def test_missing_and_stale_quotes_are_never_replaced_by_another_contract(fixture):
    f=fixture;c=signed(f);mark(f,f.rows[0],99)
    legacy={**f.rows[1],'broker':'fivepaisa'}
    mark(f,legacy,999)  # Saved retired marks cannot satisfy a Kotak request.
    missing=c.post('/api/quotes/selection',json={'instruments':[item(f.rows[1])]}).json()
    assert missing['quotes']==[]
    original=(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()
    mark(f,f.rows[1],17,stamp=original)
    stale=c.post('/api/quotes/selection',json={'instruments':[item(f.rows[1])]}).json()['quotes'][0]
    assert stale['broker']=='kotak' and stale['ltp']==17 and stale['stale'] and stale['received_at']==original
    mark(f,f.rows[1],18,exchange='NSE')
    assert c.post('/api/quotes/selection',json={'instruments':[item(f.rows[1])]}).json()['quotes']==[]


def test_selection_ttl_and_explicit_clear_preserve_other_session_demand(fixture,monkeypatch):
    f=fixture;now=[1000.0]
    monkeypatch.setattr(main,'time',SimpleNamespace(monotonic=lambda:now[0]))
    c=signed(f)
    assert c.post('/api/quotes/selection',json={'instruments':[item(f.rows[0])]}).status_code==200
    main._quote_selection_requests['other-offline-session']=(1040,{f.rows[1]['id']})
    now[0]=1029;assert main.requested_quote_selection_ids()=={f.rows[0]['id'],f.rows[1]['id']}
    now[0]=1030;assert main.requested_quote_selection_ids()=={f.rows[1]['id']}
    c.post('/api/quotes/selection',json={'instruments':[item(f.rows[0])]})
    assert c.post('/api/quotes/selection',json={'instruments':[]}).status_code==200
    assert main.requested_quote_selection_ids()=={f.rows[1]['id']}


def test_sample_workspace_does_not_create_live_demand(fixture):
    f=fixture
    with f.factory() as db:engine.update_settings(db,{'demo_mode':True});db.commit()
    result=signed(f).post('/api/quotes/selection',json={'instruments':[item(f.rows[0])]})
    assert result.status_code==409 and not f.calls and not main.requested_quote_selection_ids()


def test_demand_unions_selected_chain_and_active_positions(fixture,monkeypatch):
    f=fixture
    monkeypatch.setattr(main.option_chain,'requested_contract_ids',lambda:{f.rows[0]['id']})
    main._quote_selection_requests['offline-session']=(time.monotonic()+30,{f.rows[1]['id']})
    main._quote_selection_requests['expired-session']=(time.monotonic()-1,{f.rows[3]['id']})
    with f.factory() as db:
        db.add(Trade(id='active-offline',broker='kotak',status='open',data={'demo':False,'legs':[{'instrument_id':f.rows[2]['id'],'status':'open'}]}))
        db.add(Trade(id='closed-offline',broker='fivepaisa',status='closed',data={'demo':False,'legs':[{'instrument_id':f.rows[3]['id'],'status':'open'}]}))
        db.commit();selected=main.selected_feed_contracts(db)
    assert {row['id'] for row in selected}=={row['id'] for row in f.rows[:3]}


def test_quote_resolution_and_reads_are_off_event_loop(fixture,monkeypatch):
    f=fixture;loop=[];worker=[]
    resolve=main.resolve_quote_selection;read=main.read_selected_quotes
    def resolution(*args):worker.append(threading.get_ident());return resolve(*args)
    def quotes(*args):worker.append(threading.get_ident());return read(*args)
    monkeypatch.setattr(main,'resolve_quote_selection',resolution);monkeypatch.setattr(main,'read_selected_quotes',quotes)
    async def refresh(db,force=False):loop.append(threading.get_ident())
    monkeypatch.setattr(main,'refresh_selected_feeds',refresh)
    assert signed(f).post('/api/quotes/selection',json={'instruments':[item(f.rows[0])]}).status_code==200
    assert len(worker)==2 and loop and all(value!=loop[0] for value in worker)
