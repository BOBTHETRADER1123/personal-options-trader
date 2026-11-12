"""Recycled official tokens must never relabel saved paper positions.

These regression tests use a temporary DELETE-journal SQLite database and
synthetic contract/quote fixtures. They do not authenticate or use live feeds.
"""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import engine, option_chain as chain
from app.brokers.kotak import parse_sfeed
from app.instruments import ContractCatalog, IST, contract_master_status
from app.kotak_master import MasterBatch, SEGMENTS
from app.models import LatestQuote, PnlSnapshot, Trade
from test_chain_delete_journal import journal


IDENTIFIER = 'kotak:nse_fo:12345'


def contract(expiry, *, strike=22700):
    moment = datetime.now(timezone.utc)
    return {
        'id': IDENTIFIER, 'instrument_id': IDENTIFIER, 'broker': 'kotak',
        'token': '12345', 'underlying': 'NIFTY', 'symbol': 'NIFTY TEST CE',
        'exchange': 'NSE', 'segment': 'nse_fo', 'expiry': expiry,
        'strike': strike, 'option_type': 'CE', 'instrument_type': 'option',
        'lot_size': 65, 'tick_size': .05, 'price_multiplier': 1.0,
        'multiplier_verified': True, 'demo': False, 'source': 'broker_master',
        'master_active': True, 'master_refresh_at': moment.isoformat(),
        'master_source_date': moment.astimezone(IST).date().isoformat(),
        'imported_at': moment.isoformat(),
    }


def master_batch(row):
    return MasterBatch([copy.deepcopy(row)], {
        'last_successful_refresh_at': row['master_refresh_at'],
        'segments': {segment: {'source_date': row['master_source_date'],
                               'active_count': 1} for segment in SEGMENTS},
    })


def install(catalog, row):
    catalog.path.write_text(json.dumps({'instruments': [row], 'master_refresh': {}}),
                            encoding='utf-8')
    catalog.reload()
    assert catalog.error is None


def seed(factory, old, status='open'):
    saved = {'id': 'saved-token-reuse', 'broker': 'kotak', 'demo': False,
             'status': status, 'legs': [{**copy.deepcopy(old), 'status': status,
                 'side': 'SELL', 'lots': 1, 'open_lots': 1, 'entry_price': 185.1}]}
    history = {'net_pnl': -42.5, 'legs': [{'instrument_id': old['instrument_id'],
                                         'expiry': old['expiry'], 'ltp': 190.0}]}
    with factory() as db:
        db.add(Trade(id=saved['id'], broker='kotak', status=status, data=copy.deepcopy(saved)))
        db.add(PnlSnapshot(trade_id=saved['id'], snapshot_second=123,
                           timestamp=old['master_refresh_at'], data=copy.deepcopy(history)))
        db.commit()
    return saved, history


@pytest.fixture
def setup(journal, tmp_path, monkeypatch):
    catalog = ContractCatalog(tmp_path / 'master.json')
    monkeypatch.setattr(engine, 'catalog', catalog)
    monkeypatch.setattr(chain, '_master_lock', asyncio.Lock())
    monkeypatch.setattr(chain, '_write_lock', asyncio.Lock())
    monkeypatch.setattr(chain, '_master_jobs', {})
    monkeypatch.setattr(chain, '_attempts', {})
    monkeypatch.setattr(chain, 'MASTER_REFRESH_SECONDS', 0)
    today = datetime.now(IST).date()
    expired = contract((today - timedelta(days=9)).isoformat())
    current = contract((today + timedelta(days=21)).isoformat(), strike=23000)
    monkeypatch.setattr(chain, 'download_master', lambda broker: master_batch(current))
    return journal, catalog, expired, current


def refresh(factory):
    chain._attempts.clear()
    with factory() as db:
        return asyncio.run(chain.refresh_master(db, True))


@pytest.mark.parametrize('previous_already_recycled', [False, True])
def test_expired_recycled_token_retains_identity_and_sql_history(setup, previous_already_recycled):
    factory, catalog, old, current = setup
    install(catalog, current if previous_already_recycled else old)
    saved, history = seed(factory, old)
    result = refresh(factory)
    assert result['last_error'] is None
    assert result['new_trades_allowed'] is True
    retired = catalog.find(IDENTIFIER, 'kotak')
    assert retired['expiry'] == old['expiry'] and retired['strike'] == old['strike']
    assert retired['master_active'] is False
    assert contract_master_status(retired)['ready'] is False
    fresh = next(row for row in catalog._live if row.get('master_active') is True)
    assert fresh['id'].startswith(IDENTIFIER + ':contract:')
    assert fresh['instrument_id'] == fresh['id']
    assert fresh['token'] == current['token'] and fresh['expiry'] == current['expiry']
    assert contract_master_status(fresh)['ready'] is True
    assert [row['id'] for row in catalog.select_live([IDENTIFIER, fresh['id']], ['kotak'])] == [fresh['id']]
    assert len({row['id'] for row in catalog._live}) == 2
    with factory() as db:
        assert db.get(Trade, saved['id']).data == saved
        assert db.get(PnlSnapshot, (saved['id'], 123)).data == history
        assert len(db.scalars(select(PnlSnapshot)).all()) == 1


def test_recycled_identity_stays_stable_across_repeat_refresh_and_restart(setup, monkeypatch):
    factory, catalog, old, current = setup
    install(catalog, current)
    saved, history = seed(factory, old)
    assert refresh(factory)['last_error'] is None
    first = next(row['id'] for row in catalog._live if row.get('master_active') is True)
    assert refresh(factory)['last_error'] is None
    assert next(row['id'] for row in catalog._live if row.get('master_active') is True) == first
    restarted = ContractCatalog(catalog.path)
    assert restarted.error is None
    monkeypatch.setattr(engine, 'catalog', restarted)
    assert refresh(factory)['last_error'] is None
    assert next(row['id'] for row in restarted._live if row.get('master_active') is True) == first
    assert restarted.find(IDENTIFIER, 'kotak')['expiry'] == old['expiry']
    with factory() as db:
        assert db.get(Trade, saved['id']).data == saved
        assert db.get(PnlSnapshot, (saved['id'], 123)).data == history


def test_recycled_token_quotes_have_separate_storage_identity(setup):
    factory, catalog, old, current = setup
    install(catalog, current)
    saved, history = seed(factory, old)
    assert refresh(factory)['last_error'] is None
    fresh = next(row for row in catalog._live if row.get('master_active') is True)
    received = datetime.now(timezone.utc).isoformat()
    quote = parse_sfeed({'type': 'scrip', 'exchange_segment': 'nse_fo',
                         'instrument_token': '12345', 'last_traded_price': 210.0},
                        {('nse_fo', '12345'): fresh}, received_at=received)
    assert quote['instrument_id'] == fresh['id'] and quote['instrument_id'] != IDENTIFIER
    old_quote = {'broker': 'kotak', 'instrument_id': IDENTIFIER, 'ltp': 190.0,
                 'received_at': received, 'expiry': old['expiry']}
    with factory() as db:
        db.add(LatestQuote(broker='kotak', instrument_id=IDENTIFIER,
                            received_at=received, data=copy.deepcopy(old_quote)))
        db.add(LatestQuote(broker='kotak', instrument_id=fresh['id'],
                            received_at=received, data=quote))
        db.commit()
        assert db.get(LatestQuote, ('kotak', IDENTIFIER)).data == old_quote
        assert db.get(LatestQuote, ('kotak', fresh['id'])).data['ltp'] == 210.0
        assert db.get(Trade, saved['id']).data == saved
        assert db.get(PnlSnapshot, (saved['id'], 123)).data == history


@pytest.mark.parametrize('status', ['open', 'pending'])
def test_unexpired_active_identity_change_still_fails_atomically(setup, status):
    factory, catalog, _, current = setup
    original = {**copy.deepcopy(current), 'strike': current['strike'] - 100}
    install(catalog, original)
    saved, history = seed(factory, original, status)
    before, revision = catalog.path.read_bytes(), catalog.revision
    result = refresh(factory)
    assert result['last_error']['code'] == 'active_contract_identity_changed'
    assert result['last_error']['stage'] == 'cache_validation'
    assert catalog.path.read_bytes() == before and catalog.revision == revision
    assert catalog.find(IDENTIFIER, 'kotak')['strike'] == original['strike']
    with factory() as db:
        assert db.get(Trade, saved['id']).data == saved
        assert db.get(PnlSnapshot, (saved['id'], 123)).data == history



def test_existing_qualified_position_keeps_identity_without_historical_base_row(setup):
    factory, catalog, _, current = setup
    identifier = chain._qualified_contract_id(current)
    qualified = {**current, 'id': identifier, 'instrument_id': identifier}
    install(catalog, qualified)
    saved, history = seed(factory, qualified)
    result = refresh(factory)
    assert result['last_error'] is None
    active = [row for row in catalog._live if row.get('master_active') is True]
    assert [row['id'] for row in active] == [identifier]
    assert catalog.find(identifier, 'kotak')['master_active'] is True
    assert catalog.find(IDENTIFIER, 'kotak') is None
    assert [row['id'] for row in catalog.select_live([identifier], ['kotak'])] == [identifier]
    with factory() as db:
        assert db.get(Trade, saved['id']).data == saved
        assert db.get(PnlSnapshot, (saved['id'], 123)).data == history
