"""Read-only chain behavior, current master identity and bounded storage tests."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import OperationalError

from app import engine, option_chain as chain
from app.database import Base, configure_sqlite, get_db, iso_now
from app.instruments import ContractCatalog
from app.models import QuoteEvent, UserSession
from app.security import COOKIE, digest


def native(token, kind='CE', *, symbol='NIFTY', exchange='NSE', instrument='OPTIDX', underlying_id='13',
           strike='25000', expiry='2030-10-31'):
    return {'EXCH_ID': exchange, 'SEGMENT': 'D', 'INSTRUMENT': instrument, 'SECURITY_ID': str(token),
            'DISPLAY_NAME': f'{symbol} {expiry} {strike} {kind}', 'UNDERLYING_SYMBOL': symbol,
            'UNDERLYING_SECURITY_ID': underlying_id, 'SM_EXPIRY_DATE': expiry, 'STRIKE_PRICE': strike,
            'OPTION_TYPE': kind, 'LOT_SIZE': '50', 'TICK_SIZE': '.05'}


def groups():
    return chain.parse_master([native(101), native(102, 'PE'),
                               native(201, symbol='RELIANCE', instrument='OPTSTK', underlying_id='2885', strike='1400'),
                               native(202, 'PE', symbol='RELIANCE', instrument='OPTSTK', underlying_id='2885', strike='1400'),
                               native(301, exchange='BSE', symbol='SENSEX', underlying_id='51', strike='80000')], today='2026-10-22')


@pytest.fixture
def db(tmp_path, monkeypatch):
    sql = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    configure_sqlite(sql)
    Base.metadata.create_all(sql)
    old_catalog = engine.catalog
    engine.set_catalog(ContractCatalog(tmp_path/'absent.json'))
    chain.configure(asyncio.Lock())
    chain._attempts.clear()
    chain._expiry_cache.clear()
    monkeypatch.delenv('DHAN_ACCESS_TOKEN', raising=False)
    monkeypatch.delenv('DHAN_CLIENT_ID', raising=False)
    with sessionmaker(sql, expire_on_commit=False)() as session:
        engine.update_settings(session, {'demo_mode': False, 'brokerage_per_order': 0, 'charges_percent': 0, 'fill_basis': 'ltp'})
        session.commit()
        yield session
    engine.set_catalog(old_catalog)
    chain._master_indexes.pop(sql, None)
    chain._selected_masters.pop(sql, None)
    chain._snapshots.pop(sql, None)
    sql.dispose()


def load_master(db, monkeypatch):
    monkeypatch.setattr(chain, 'download_master', groups)
    return asyncio.run(chain.refresh_master(db))


def broker_snapshot():
    return {'status': 'success', 'data': {'last_price': 25010, 'oc': {'25000.000000': {
        'ce': {'security_id': 101, 'last_price': 110, 'previous_close_price': 100, 'oi': 2000, 'previous_oi': 1000,
               'volume': 800, 'previous_volume': 400, 'top_bid_price': 109.5, 'top_ask_price': 110.5,
               'implied_volatility': 12.5, 'greeks': {'delta': .55, 'gamma': .002, 'theta': -4.2, 'vega': 7.8}},
        'pe': {'security_id': 102, 'last_price': 90, 'previous_close_price': 100, 'oi': 4000, 'previous_oi': 3000,
               'volume': 600, 'previous_volume': 0, 'implied_volatility': 13.5, 'greeks': {'delta': -.45}}
    }}}}


def test_official_master_uses_all_stock_and_index_identities():
    result = groups()
    assert set(result) == {'NSE:NIFTY', 'NSE:RELIANCE', 'BSE:SENSEX'}
    assert result['NSE:RELIANCE']['underlying_security_id'] == 2885
    assert result['NSE:RELIANCE']['underlying_segment'] == 'NSE_EQ'
    assert result['NSE:RELIANCE']['kind'] == 'stock'
    assert result['BSE:SENSEX']['underlying_segment'] == 'IDX_I'
    assert result['NSE:NIFTY']['expiries'] == ['2030-10-31']
    assert result['NSE:NIFTY']['contracts'][0]['id'] == 'dhan:NSE_FNO:101'
    assert result['NSE:NIFTY']['lot_size'] == 50


def test_invalid_or_duplicate_identity_rejected_instead_of_guessing():
    with pytest.raises(ValueError, match='underlying security ID'):
        chain.parse_master([native(101, underlying_id='')], today='2026-10-22')
    with pytest.raises(ValueError, match='duplicate'):
        chain.parse_master([native(101), native(999)], today='2026-10-22')
    assert not any(c['expiry'] == '2020-01-01' for g in chain.parse_master(
        [native(101), native(999, expiry='2020-01-01')], today='2026-10-22').values() for c in g['contracts'])


def test_quote_mapping_preserves_missing_fields_and_derives_ratios():
    group = groups()['NSE:NIFTY']
    result = chain.build_chain(group, group['contracts'], '2030-10-31', broker_snapshot(), iso_now())
    row = result['rows'][0]
    assert row['atm'] and row['itm_call'] and not row['itm_put']
    assert row['pcr'] == 2 and result['summary']['pcr'] == 2
    assert result['summary']['atm_iv'] == 13
    assert row['ce']['buildup'] == 'Long buildup'
    assert row['pe']['buildup'] == 'Short buildup'
    assert row['ce']['oi_change_percent'] == 100
    assert row['ce']['volume_change_percent'] == 100
    assert row['pe']['volume_change_percent'] is None
    assert row['ce']['delta'] == .55 and row['pe']['delta'] == -.45
    assert row['pe']['theta'] is None and row['pe']['bid'] is None
    assert result['quotes'][0]['instrument_id'] == 'dhan:NSE_FNO:101'
    assert result['quantity_unit'] == 'broker_reported'


def test_mismatched_token_cannot_price_a_different_contract():
    raw = broker_snapshot()
    raw['data']['oc']['25000.000000']['ce']['security_id'] = 999
    group = groups()['NSE:NIFTY']
    result = chain.build_chain(group, group['contracts'], '2030-10-31', raw, iso_now())
    assert result['rows'][0]['ce']['ltp'] is None
    assert len(result['quotes']) == 1
    assert result['quotes'][0]['instrument_id'].endswith(':102')


def test_missing_credentials_show_official_contracts_without_invented_prices(db, monkeypatch):
    load_master(db, monkeypatch)
    result = asyncio.run(chain.option_chain('NSE:RELIANCE', mode='live', broker='dhan', db=db))
    assert result['status'] == 'credentials_required'
    assert result['spot'] is None and not result['trading_enabled']
    assert result['rows'][0]['ce']['ltp'] is None and result['rows'][0]['pe']['oi'] is None
    assert result['quotes'] == []
    assert len(engine.catalog.all(demo=False)) == 2  # selected stock only
    assert db.scalar(select(func.count()).select_from(QuoteEvent)) == 0


def test_metadata_and_selected_contracts_are_not_reread_on_each_poll(db, monkeypatch):
    load_master(db, monkeypatch)
    asyncio.run(chain.option_chain('NSE:NIFTY', mode='live', broker='dhan', db=db))
    statements = []
    def count(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', count)
    try:
        for _ in range(3):
            result = asyncio.run(chain.underlyings(q='RELI', mode='live', broker='dhan', db=db))
            asyncio.run(chain.option_chain('NSE:NIFTY', mode='live', broker='dhan', db=db))
        assert result['total'] == 3 and result['underlyings'][0]['symbol'] == 'RELIANCE'
        assert not any('option_chain_masters' in s for s in statements)
        assert not any('option_chain_snapshots' in s for s in statements)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', count)


def test_failed_master_download_retains_verified_catalog_and_backs_off(db, monkeypatch):
    load_master(db, monkeypatch)
    index = chain.master_index(db)
    index['updated_at'] = '2020-01-01T00:00:00+00:00'
    calls = []
    def fail():
        calls.append(True)
        raise OSError('offline')
    monkeypatch.setattr(chain, 'download_master', fail)
    first = asyncio.run(chain.refresh_master(db))
    second = asyncio.run(chain.refresh_master(db))
    assert first['status'] == second['status'] == 'cached'
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(chain.ChainMaster)) == 3


def test_live_chain_is_read_only_cached_and_not_written_as_tick_history(db, monkeypatch):
    load_master(db, monkeypatch)
    monkeypatch.setenv('DHAN_CLIENT_ID', 'offline-test-client')
    monkeypatch.setenv('DHAN_ACCESS_TOKEN', 'offline-test-token')
    posts, marks = [], []
    async def post(path, body):
        posts.append((path, body))
        return {'data': ['2030-10-31'], 'status': 'success'} if path.endswith('expirylist') else broker_snapshot()
    monkeypatch.setattr(chain, 'dhan_post', post)
    monkeypatch.setattr(engine, 'cache_quote', lambda _db, quote, persist=False: marks.append((quote, persist)))
    first = asyncio.run(chain.option_chain('NSE:NIFTY', mode='live', broker='dhan', db=db))
    second = asyncio.run(chain.option_chain('NSE:NIFTY', mode='live', broker='dhan', db=db))
    assert first['status'] == 'connected' and second['cached']
    assert posts == [('/optionchain/expirylist', {'UnderlyingScrip': 13, 'UnderlyingSeg': 'IDX_I'}),
                     ('/optionchain', {'UnderlyingScrip': 13, 'UnderlyingSeg': 'IDX_I', 'Expiry': '2030-10-31'})]
    assert len(marks) == 2 and all(persist is False for _, persist in marks)
    assert db.scalar(select(func.count()).select_from(chain.ChainSnapshot)) == 0
    assert db.scalar(select(func.count()).select_from(QuoteEvent)) == 0


def test_failed_live_refresh_keeps_timestamped_data_but_disables_entries(db, monkeypatch):
    load_master(db, monkeypatch)
    monkeypatch.setenv('DHAN_CLIENT_ID', 'offline-test-client')
    monkeypatch.setenv('DHAN_ACCESS_TOKEN', 'offline-test-token')
    group = groups()['NSE:NIFTY']
    stamp = (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
    cached = chain.build_chain(group, group['contracts'], '2030-10-31', broker_snapshot(), stamp)
    cached.update(status='connected', stale=False)
    chain._snapshots[db.get_bind()] = {'NSE:NIFTY:2030-10-31': cached}
    async def fail(*_):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(chain, 'dhan_post', fail)
    result = asyncio.run(chain.option_chain('NSE:NIFTY', mode='live', broker='dhan', db=db))
    assert result['status'] == 'stale' and result['stale'] and not result['trading_enabled']
    assert result['updated_at'] == stamp and result['rows'][0]['ce']['ltp'] == 110


def test_snapshot_cache_is_bounded_and_never_persists_chain_payloads(db):
    for i in range(20):
        chain.persist_snapshot(db, f'underlying-{i}', {'quotes': [], 'updated_at': iso_now()})
    assert len(chain._snapshots[db.get_bind()]) == chain.MAX_SELECTED_CHAINS
    assert db.scalar(select(func.count()).select_from(chain.ChainSnapshot)) == 0


def test_browsing_another_stock_keeps_active_but_discards_previous_untraded_chain(db, monkeypatch):
    load_master(db, monkeypatch)
    catalog = engine.catalog
    nifty = groups()['NSE:NIFTY']['contracts']
    reliance = groups()['NSE:RELIANCE']['contracts']
    catalog.select_chain_contracts(nifty, set())
    catalog.select_chain_contracts(reliance, {nifty[0]['id']})
    assert {c['id'] for c in catalog.all(demo=False)} == {nifty[0]['id'], reliance[0]['id'], reliance[1]['id']}
    restored = ContractCatalog(catalog.path)
    restored.restore_live_contracts([{'demo': False, 'status': 'open', 'legs': [{**nifty[0], 'status': 'open'}]}])
    assert restored.find(nifty[0]['id'])['token'] == '101'


def test_option_chain_router_requires_owner_and_csrf_for_catalog_write(db):
    app = FastAPI()
    app.include_router(chain.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.get('/api/option-chain/underlyings?mode=sample').status_code == 401
        db.add(UserSession(token_hash=digest('offline-owner'), csrf_token='offline-csrf', expires_at=time.time()+3600))
        db.commit()
        client.cookies.set(COOKIE, 'offline-owner')
        assert client.get('/api/option-chain/underlyings?mode=sample').status_code == 200
        assert client.post('/api/option-chain/master/refresh').status_code == 403


def test_only_documented_market_data_post_paths_are_allowed():
    with pytest.raises(ValueError, match='market-data'):
        asyncio.run(chain.dhan_post('/orders', {}))


def test_database_quota_outage_keeps_health_available_and_pauses_sql_workers(monkeypatch, tmp_path):
    from app import main, brokers, instruments, reporting
    attempts, work = [], []
    def unavailable():
        attempts.append(True)
        raise OperationalError('offline-test', {}, OSError('Database unavailable'))
    class OfflineFeeds:
        def __init__(self, **_):pass
        async def start(self, *_):work.append('feed')
        async def stop(self):pass
    async def capture():work.append('capture')
    async def reports(_):work.append('reports')
    monkeypatch.setattr(main, 'database_ready', True)
    monkeypatch.setattr(main, 'initialize_database', unavailable)
    monkeypatch.setattr(main, 'background_loop', capture)
    monkeypatch.setattr(main, 'DATABASE_RETRY_SECONDS', 60)
    monkeypatch.setattr(reporting, 'reporting_loop', reports)
    monkeypatch.setattr(brokers, 'FeedManager', OfflineFeeds)
    monkeypatch.setattr(instruments, 'ContractCatalog', lambda: ContractCatalog(tmp_path/'absent.json'))
    with TestClient(main.app) as client:
        health = client.get('/api/health')
        assert health.status_code == 200 and health.json()['ok']
        assert health.json()['database_ready'] is False
        session = client.get('/api/session')
        assert session.status_code == 503 and session.headers['retry-after'] == '60'
        assert session.json()['database_ready'] is False
        assert len(attempts) == 1 and work == []


def test_database_supervisor_retries_with_backoff_then_restarts_workers(monkeypatch):
    from app import main
    attempts, work = [], []
    class OfflineFeeds:
        async def stop(self):pass
    async def initialize():
        attempts.append(True)
        if len(attempts) < 2:return False
        main.database_ready = True
        return True
    async def capture():
        work.append('capture')
        main.stop_event.set()
    async def reports(_):work.append('reports')
    async def scenario():
        monkeypatch.setattr(main, 'stop_event', asyncio.Event())
        await asyncio.wait_for(main.database_services(reports), timeout=1)
    monkeypatch.setattr(main, 'database_ready', False)
    monkeypatch.setattr(main, 'DATABASE_RETRY_SECONDS', .01)
    monkeypatch.setattr(main, 'feed_manager', OfflineFeeds())
    monkeypatch.setattr(main, 'try_initialize_database', initialize)
    monkeypatch.setattr(main, 'background_loop', capture)
    asyncio.run(scenario())
    assert len(attempts) == 2 and work == ['capture', 'reports']
