"""Read-only chains use exact identities and independent broker price caches."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import engine, option_chain as chain
from app.database import Base, get_db, iso_now
from app.instruments import ContractCatalog, normalize_row
from app.models import LatestQuote, UserSession
from app.security import COOKIE, digest


def native(token, kind='CE', *, symbol='NIFTY', exchange='N', strike='25000', expiry='2030-10-31'):
    return {'Exch': exchange, 'ExchType': 'D', 'ScripCode': str(token), 'Name': f'{symbol} {expiry} {strike} {kind}',
            'SymbolRoot': symbol, 'Expiry': expiry, 'StrikeRate': strike, 'ScripType': kind, 'LotSize': '50', 'TickSize': '.05'}


def contracts(broker='fivepaisa'):
    rows = [normalize_row('fivepaisa', native(101)), normalize_row('fivepaisa', native(102, 'PE')),
            normalize_row('fivepaisa', native(201, symbol='RELIANCE', strike='1400')),
            normalize_row('fivepaisa', native(301, exchange='B', symbol='SENSEX', strike='80000'))]
    if broker == 'kotak':
        rows = [{**row, 'broker': 'kotak', 'id': f"kotak:{'nse_fo' if row['exchange']=='NSE' else 'bse_fo'}:{row['token']}",
                 'instrument_id': f"kotak:{'nse_fo' if row['exchange']=='NSE' else 'bse_fo'}:{row['token']}",
                 'segment': 'nse_fo' if row['exchange']=='NSE' else 'bse_fo'} for row in rows]
    return rows


@pytest.fixture
def db(tmp_path, monkeypatch):
    sql = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(sql)
    old_catalog = engine.catalog
    catalog = ContractCatalog(tmp_path/'instruments.json')
    catalog.merge_live_contracts(contracts()+contracts('kotak'))
    engine.set_catalog(catalog)
    chain.configure(asyncio.Lock())
    chain._attempts.clear();chain._requested.clear()
    for name in ('KOTAK_CONSUMER_KEY','KOTAK_SID','KOTAK_UCC','KOTAK_TOTP','KOTAK_TOTP_SECRET','FIVEPAISA_CLIENT_CODE','FIVEPAISA_ACCESS_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    with sessionmaker(sql, expire_on_commit=False)() as session:
        engine.update_settings(session, {'demo_mode': False, 'brokerage_per_order': 0, 'charges_percent': 0, 'fill_basis': 'ltp'})
        session.commit()
        yield session
        engine.clear_cache(session)
    engine.set_catalog(old_catalog)
    chain.configure(asyncio.Lock());chain._requested.clear();sql.dispose()


def get(db, broker='fivepaisa', **kwargs):
    return asyncio.run(chain.option_chain('NSE:NIFTY', mode='live', broker=broker, db=db, **kwargs))


def mark(db, broker='fivepaisa', price=110, stamp=None):
    c = contracts(broker)[0]
    value = {**c, 'instrument_id': c['id'], 'ltp': price, 'bid': price-.5, 'ask': price+.5,
             'demo': False, 'received_at': stamp or iso_now(), 'exchange_timestamp': '2030-01-01T00:00:00+00:00', 'open_interest': 1000}
    engine.cache_quote(db, value, persist=True)
    db.commit()
    return value


def test_official_native_master_lists_stocks_indices_and_mcx_without_guessing_tokens():
    groups = chain.parse_master([native(101), native(102,'PE'), native(201,symbol='RELIANCE',strike='1400'),
                                 native(301,exchange='B',symbol='SENSEX',strike='80000'), native(401,exchange='M',symbol='GOLD',strike='75000')])
    assert set(groups) == {'NSE:NIFTY','NSE:RELIANCE','BSE:SENSEX','MCX:GOLD'}
    assert groups['NSE:RELIANCE']['kind']=='stock' and groups['MCX:GOLD']['kind']=='commodity'
    assert groups['NSE:NIFTY']['contracts'][0]['id']=='fivepaisa:NSE:D:101'
    assert groups['NSE:NIFTY']['expiries']==['2030-10-31']
    with pytest.raises(ValueError,match='Duplicate'):
        chain.parse_master([native(101),native(101)])


def test_broker_removed_from_all_active_chain_entry_points(db):
    with pytest.raises(HTTPException) as error:get(db,'dhan')
    assert error.value.status_code==422
    with pytest.raises(HTTPException):chain.configured('dhan')


def test_missing_credentials_show_master_but_no_fake_live_prices(db):
    result = get(db)
    assert result['status']=='credentials_required' and result['stale'] and not result['trading_enabled']
    assert result['rows'][0]['ce']['ltp'] is None and result['rows'][0]['ce']['delta'] is None
    assert result['quotes']==[] and result['spot'] is None
    assert chain.requested_contract_ids()=={c['id'] for c in contracts() if c['underlying']=='NIFTY'}


def test_independent_prices_never_borrow_from_other_broker(db):
    five=mark(db,price=111)
    kotak=get(db,'kotak')
    assert kotak['quotes']==[] and kotak['rows'][0]['ce']['ltp'] is None
    mark(db,'kotak',price=122)
    assert get(db,'kotak')['rows'][0]['ce']['ltp']==122
    result=get(db)
    assert result['rows'][0]['ce']['ltp']==111
    assert result['rows'][0]['ce']['quote_at']==five['received_at']
    assert result['rows'][0]['ce']['exchange_timestamp']==five['exchange_timestamp']
    assert all(q['broker']=='fivepaisa' for q in result['quotes'])


def test_stale_marks_keep_original_timestamp_and_disable_trade(db):
    stamp=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    mark(db,stamp=stamp)
    result=get(db)
    assert result['status']=='stale' and result['stale'] and not result['trading_enabled']
    assert result['rows'][0]['ce']['quote_at']==stamp
    assert result['rows'][0]['ce']['stale'] and result['rows'][0]['ce']['ltp']==110


def test_selected_chain_subscription_requests_refresh_without_browser_dependency(db):
    calls=[]
    async def refresh(session):calls.append(chain.requested_contract_ids())
    chain.configure(asyncio.Lock(),refresh)
    get(db)
    assert len(calls)==1 and calls[0]==chain.requested_contract_ids()
    key=chain._requested['fivepaisa']
    chain._requested['fivepaisa']=(0,key[1])
    assert chain.requested_contract_ids()==set()


def test_unavailable_greeks_remain_null_and_are_explained(db):
    mark(db)
    result=get(db)
    leg=result['rows'][0]['ce']
    assert all(leg[key] is None for key in ('delta','gamma','vega','theta','iv'))
    assert result['capabilities']['greeks'] is False
    assert 'NSE-only' in result['capabilities']['greeks_note']


def test_live_chain_no_database_writes_or_per_strike_queries(db):
    mark(db)
    queries=[]
    def capture(conn,cursor,statement,params,context,many):queries.append(statement)
    event.listen(db.get_bind(),'before_cursor_execute',capture)
    try:get(db)
    finally:event.remove(db.get_bind(),'before_cursor_execute',capture)
    assert not any(q.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries)
    assert len([q for q in queries if 'latest_quotes' in q])<=1
    assert not any('quote_events' in q for q in queries)


def test_simulated_chain_only_two_brokers_and_not_live_execution(db):
    result=asyncio.run(chain.option_chain('NSE:RELIANCE',mode='sample',broker='kotak',db=db))
    assert result['demo'] and result['status']=='sample' and not result['trading_enabled']
    assert result['mode_matches_workspace'] is False
    assert all(q['broker']=='kotak' and q['demo'] for q in result['quotes'])
    assert 'Fictional' in result['message']


def test_futures_not_shown_as_option_chain(db):
    future=normalize_row('fivepaisa',native(999,'XX',strike='0'))
    engine.catalog.merge_live_contracts([future])
    result=get(db)
    assert all(c['option_type'] in {'CE','PE'} for c in result['instruments'])
    assert engine.catalog.find(future['id'])['option_type']=='FUT'


def test_refresh_is_broker_scoped_and_failed_download_preserves_file(db,monkeypatch):
    old=contracts('kotak');path=engine.catalog.path
    path.write_text(json.dumps({'instruments': old}))
    monkeypatch.setattr(chain,'download_master',lambda broker:contracts(broker))
    result=asyncio.run(chain.refresh_master(db,force=True,broker='fivepaisa'))
    assert result['status']=='ready'
    assert {c['broker'] for c in engine.catalog.all(False)}=={'kotak','fivepaisa'}
    before=path.read_bytes()
    chain._attempts.clear()
    def fail(broker):raise OSError('credentials must never be echoed')
    monkeypatch.setattr(chain,'download_master',fail)
    # Force a stale master so refresh actually attempts the failing download.
    for c in engine.catalog._live:c['imported_at']='2020-01-01T00:00:00+00:00'
    engine.catalog.revision += 1
    result=asyncio.run(chain.refresh_master(db,force=True,broker='fivepaisa'))
    assert result['status']=='cached' and path.read_bytes()==before
    assert 'credentials must never' not in result['message']


def test_invalid_expiry_rejected(db):
    with pytest.raises(HTTPException) as error:get(db,expiry='2099-01-01')
    assert error.value.status_code==422
