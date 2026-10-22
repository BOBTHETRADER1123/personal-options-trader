"""Read-only Dhan option chains and a durable, exchange-derived F&O universe.

Only public instrument downloads and the two documented option-chain data
endpoints are called. Sample numbers are deliberately fictional and isolated.
"""
from __future__ import annotations

import asyncio
import copy
import csv
import math
import os
import time
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Literal
from weakref import WeakKeyDictionary

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import JSON, String, delete, insert, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import engine
from .database import Base, get_db, iso_now
from .instruments import DHAN_MASTER_URL, IST, normalize_row
from .models import QuoteEvent, Trade
from .security import require_session

router = APIRouter(prefix='/api/option-chain', tags=['Read-only option chain'], dependencies=[Depends(require_session)])
_write_lock = asyncio.Lock()
_master_lock = asyncio.Lock()
_request_lock = asyncio.Lock()
_attempts: dict[str, float] = {}
_expiry_cache: dict[str, tuple[float, list[str]]] = {}
_master_indexes = WeakKeyDictionary()
_selected_masters = WeakKeyDictionary()
_snapshots = WeakKeyDictionary()
MIN_INTERVAL = 3.0
MASTER_REFRESH_SECONDS = 300
MAX_SELECTED_CHAINS = 8
SOURCE = 'https://dhanhq.co/docs/v2/option-chain/'


class ChainMaster(Base):
    __tablename__ = 'option_chain_masters'
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(100), index=True)
    exchange: Mapped[str] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(12))
    metadata_json: Mapped[dict] = mapped_column(JSON)
    contracts: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String(50))


class ChainSnapshot(Base):
    __tablename__ = 'option_chain_snapshots'
    key: Mapped[str] = mapped_column(String(220), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String(50))


def configure(write_lock):
    global _write_lock
    _write_lock = write_lock


def number(value, *, positive=False):
    try:
        result = float(value)
        if not math.isfinite(result) or (positive and result < 0):
            return None
        return result
    except (TypeError, ValueError, OverflowError):
        return None


def percent(change, previous):
    return round(change / previous * 100, 4) if change is not None and previous is not None and previous > 0 else None


def age(value):
    try:
        return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds())
    except (TypeError, ValueError):
        return float('inf')


def configured():
    return bool(os.getenv('DHAN_ACCESS_TOKEN') and os.getenv('DHAN_CLIENT_ID'))


def mode_value(db, mode):
    return mode or ('sample' if engine.get_settings(db)['demo_mode'] else 'live')


def download_master():
    # Streaming CSV avoids retaining a second full raw download in memory.
    with urllib.request.urlopen(DHAN_MASTER_URL, timeout=60) as response:
        def lines():
            count = 0
            for line in response:
                count += len(line)
                if count > 100 * 1024 * 1024:
                    raise ValueError('Official master exceeded the bounded download size.')
                yield line.decode('utf-8-sig')
        return parse_master(csv.DictReader(lines()))


def parse_master(rows, today=None):
    """Use underlying IDs from the native detailed master, never symbol guesses."""
    today = today or datetime.now(IST).date().isoformat()
    groups, seen = {}, set()
    for raw in rows:
        if raw.get('EXCH_ID') not in {'NSE', 'BSE'} or raw.get('SEGMENT') != 'D' or raw.get('INSTRUMENT') not in {'OPTIDX', 'OPTSTK'}:
            continue
        contract = normalize_row('dhan', raw)
        contract.pop('imported_at', None)
        if contract['expiry'] < today:
            continue
        underlying_id = str(raw.get('UNDERLYING_SECURITY_ID', '')).strip()
        if not underlying_id.isdigit() or int(underlying_id) <= 0:
            raise ValueError('The official option master is missing an underlying security ID. Existing catalog retained.')
        kind = 'index' if raw['INSTRUMENT'] == 'OPTIDX' else 'stock'
        key = f"{contract['exchange']}:{contract['underlying']}"
        segment = 'IDX_I' if kind == 'index' else f"{contract['exchange']}_EQ"
        signature = (key, contract['expiry'], contract['strike'], contract['option_type'])
        if signature in seen:
            raise ValueError('The official master contains duplicate option identities. Existing catalog retained.')
        seen.add(signature)
        contract.update(underlying_security_id=int(underlying_id), underlying_segment=segment, chain_key=key, underlying_kind=kind)
        group = groups.setdefault(key, {'key': key, 'symbol': contract['underlying'], 'name': str(raw['UNDERLYING_SYMBOL']).strip(),
                                       'exchange': contract['exchange'], 'kind': kind, 'type': kind,
                                       'underlying_security_id': int(underlying_id), 'underlying_segment': segment, 'contracts': []})
        if group['underlying_security_id'] != int(underlying_id) or group['kind'] != kind:
            raise ValueError('Conflicting underlying identity in broker master. Existing catalog retained.')
        group['contracts'].append(contract)
    if not groups:
        raise ValueError('No current stock or index options were found in the official master. Existing catalog retained.')
    for group in groups.values():
        contracts = group['contracts']
        group['expiries'] = sorted({c['expiry'] for c in contracts})
        group['lot_size'] = next(c['lot_size'] for c in contracts if c['expiry'] == group['expiries'][0])
        group['contract_count'] = len(contracts)
    return groups


def master_status(db):
    index = master_index(db)
    dates, total = index['updated_at'], len(index['rows'])
    return {'status': 'ready' if total else 'not_loaded', 'updated_at': dates, 'source': DHAN_MASTER_URL,
            'underlying_count': total, 'stale': bool(dates and age(dates) > 86400), 'read_only': True}


def master_index(db):
    """Read small metadata once; never pull the entire contract universe to list it."""
    bind = db.get_bind()
    cached = _master_indexes.get(bind)
    if cached is None:
        rows = db.execute(select(ChainMaster.metadata_json, ChainMaster.updated_at)).all()
        cached = {'rows': [r.metadata_json for r in rows], 'updated_at': min((r.updated_at for r in rows), default=None),
                  'last_attempt': 0.0, 'last_error': None}
        _master_indexes[bind] = cached
    return cached


def selected_master(db, key):
    cache = _selected_masters.setdefault(db.get_bind(), OrderedDict())
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    row = db.execute(select(ChainMaster.metadata_json, ChainMaster.contracts, ChainMaster.updated_at).where(ChainMaster.key == key)).first()
    if row is None:
        return None
    value = (row.metadata_json, row.contracts, row.updated_at)
    cache[key] = value
    while len(cache) > MAX_SELECTED_CHAINS:
        cache.popitem(last=False)
    return value


def write_master(db, values):
    # Replacing the complete master is atomic. A failed import retains the last
    # verified universe; no full old contract JSON is selected to perform it.
    db.execute(delete(ChainMaster))
    for offset in range(0, len(values), 20):
        db.execute(insert(ChainMaster), values[offset:offset+20])
    db.commit()


async def refresh_master(db, force=False):
    async with _master_lock:
        current = master_status(db)
        index = master_index(db)
        if current['updated_at'] and age(current['updated_at']) < (MASTER_REFRESH_SECONDS if force else 86400):
            return current
        if time.monotonic()-index['last_attempt'] < MASTER_REFRESH_SECONDS and index['last_error']:
            if current['underlying_count']:
                return {**current, 'status': 'cached', 'message': index['last_error']}
            raise HTTPException(503, index['last_error'])
        index['last_attempt'] = time.monotonic()
        # Release the read transaction while a public CSV is downloaded.
        db.rollback()
        try:
            groups = await asyncio.to_thread(download_master)
        except Exception as exc:
            index['last_error'] = 'The official instrument catalog could not be downloaded. Try Refresh catalog again in five minutes.'
            if current['underlying_count']:
                return {**current, 'status': 'cached', 'message': f'Official master refresh unavailable ({type(exc).__name__}); last verified catalog retained.'}
            raise HTTPException(503, index['last_error']) from None
        stamp = iso_now()
        values = []
        for key, group in groups.items():
            metadata = {k: v for k, v in group.items() if k != 'contracts'}
            values.append({'key': key, 'symbol': group['symbol'], 'exchange': group['exchange'], 'kind': group['kind'],
                           'metadata_json': metadata, 'contracts': group['contracts'], 'updated_at': stamp})
        async with _write_lock:
            await asyncio.to_thread(write_master, db, values)
        _master_indexes[db.get_bind()] = {'rows': [v['metadata_json'] for v in values], 'updated_at': stamp,
                                         'last_attempt': time.monotonic(), 'last_error': None}
        _selected_masters.pop(db.get_bind(), None)
        _snapshots.pop(db.get_bind(), None)
        return master_status(db)


def ensure_sample_stocks(catalog):
    if catalog is None or any(c['underlying'] == 'RELIANCE' for c in catalog._demo):
        return
    expiries = sorted({c['expiry'] for c in catalog.all(demo=True)})[:2]
    # Fictional examples only. They are neither real lot sizes nor current prices.
    for symbol, spot, step, lot in [('RELIANCE', 1400, 20, 100), ('HDFCBANK', 1900, 20, 100), ('INFY', 1600, 20, 100), ('SBIN', 800, 10, 100)]:
        for expiry in expiries:
            for offset in range(-12, 13):
                strike = spot + offset * step
                for kind in ('CE', 'PE'):
                    intrinsic = max(0, (spot - strike) * (1 if kind == 'CE' else -1))
                    for broker in engine.BROKERS:
                        identifier = f'DEMO:{symbol}:{expiry}:{strike}:{kind}'
                        catalog._demo.append({'id': identifier, 'instrument_id': identifier, 'broker': broker, 'token': 'DEMO',
                            'symbol': f'{symbol} {expiry} {strike} {kind}', 'underlying': symbol, 'exchange': 'NSE',
                            'segment': 'NSE_FNO' if broker == 'dhan' else 'nse_fo' if broker == 'kotak' else 'D',
                            'expiry': expiry, 'strike': strike, 'option_type': kind, 'lot_size': lot, 'tick_size': .05,
                            'demo': True, 'source': 'synthetic_demo', 'spot': spot, 'base_price': round(intrinsic + max(step*.2, step*2.8-abs(offset)*step*.18), 2),
                            'price_multiplier': 1.0, 'multiplier_verified': True, 'ltp': intrinsic + step})


def sample_universe(catalog, broker='dhan'):
    ensure_sample_stocks(catalog)
    groups = {}
    indices = {'NIFTY', 'BANKNIFTY', 'SENSEX'}
    for c in catalog.all(demo=True) if catalog else []:
        if c['broker'] != broker or c['exchange'] not in {'NSE', 'BSE'}:
            continue
        key = f"{c['exchange']}:{c['underlying']}"
        group = groups.setdefault(key, {'key': key, 'symbol': c['underlying'], 'name': c['underlying'], 'exchange': c['exchange'],
                                       'kind': 'index' if c['underlying'] in indices else 'stock', 'type': 'index' if c['underlying'] in indices else 'stock',
                                       'expiries': [], 'lot_size': c['lot_size'], 'contract_count': 0, 'demo': True})
        group['expiries'].append(c['expiry'])
        group['contract_count'] += 1
    for group in groups.values():
        group['expiries'] = sorted(set(group['expiries']))
    return list(groups.values())


def normalize_leg(raw, contract, stamp):
    raw = raw if isinstance(raw, dict) else {}
    greeks = raw.get('greeks') if isinstance(raw.get('greeks'), dict) else {}
    ltp = number(raw.get('last_price'), positive=True)
    previous = number(raw.get('previous_close_price'), positive=True)
    oi = number(raw.get('oi'), positive=True)
    previous_oi = number(raw.get('previous_oi'), positive=True)
    volume = number(raw.get('volume'), positive=True)
    previous_volume = number(raw.get('previous_volume'), positive=True)
    change = ltp-previous if ltp is not None and previous is not None else None
    oi_change = oi-previous_oi if oi is not None and previous_oi is not None else None
    buildup = None
    if change is not None and oi_change is not None and change != 0 and oi_change != 0:
        buildup = ('Long buildup' if change > 0 else 'Short buildup') if oi_change > 0 else ('Short covering' if change > 0 else 'Long unwinding')
    return {'instrument_id': contract['id'], 'security_id': contract.get('token'), 'symbol': contract['symbol'], 'broker': contract['broker'],
            'expiry': contract['expiry'], 'strike': contract['strike'], 'option_type': contract['option_type'], 'lot_size': contract['lot_size'],
            'ltp': ltp, 'bid': number(raw.get('top_bid_price'), positive=True), 'ask': number(raw.get('top_ask_price'), positive=True),
            'oi': oi, 'oi_change': oi_change, 'oi_change_percent': percent(oi_change, previous_oi), 'volume': volume,
            'volume_change_percent': percent(volume-previous_volume if volume is not None and previous_volume is not None else None, previous_volume),
            'iv': number(raw.get('implied_volatility'), positive=True), **{g: number(greeks.get(g)) for g in ('delta', 'gamma', 'theta', 'vega')},
            'ltp_change': change, 'ltp_change_percent': percent(change, previous), 'change': change, 'change_percent': percent(change, previous),
            'buildup': buildup, 'quote_at': stamp if raw else None, 'demo': contract.get('demo', False), 'contract': dict(contract)}


def sample_raw(contract, spot):
    """Deterministic fictional reference data, never described as broker data."""
    strike = contract['strike']
    base = float(contract['base_price'])
    step = max(1, spot * .001)
    position = (strike-spot) / step
    ce = contract['option_type'] == 'CE'
    delta = max(.03, min(.97, .5-position*.025))
    seed = sum(map(ord, contract['id']))
    previous = base * (1.02 if seed % 2 else .98)
    previous_oi = 10000 + seed*9
    return {'last_price': base, 'previous_close_price': previous, 'oi': previous_oi+(seed%5-2)*1000,
            'previous_oi': previous_oi, 'volume': 5000+seed*7, 'previous_volume': 4000+seed*5,
            'top_bid_price': max(.05, base-.15), 'top_ask_price': base+.15, 'implied_volatility': 12+abs(position)*.15,
            'greeks': {'delta': delta if ce else delta-1, 'gamma': .001, 'theta': -2.5, 'vega': 8.5}}


def build_chain(metadata, contracts, expiry, raw=None, stamp=None, demo=False):
    raw = raw or {}
    data = raw.get('data') or {}
    option_data = data.get('oc') or {}
    strike_data = {number(k): v for k, v in option_data.items()}
    spot = number(data.get('last_price'), positive=True)
    if demo:
        spot = number(contracts[0].get('spot')) if contracts else None
    rows, instruments, quotes = {}, [], []
    for c in contracts:
        if c['expiry'] != expiry:
            continue
        instruments.append(c)
        strike = float(c['strike'])
        row = rows.setdefault(strike, {'strike': strike, 'ce': None, 'pe': None})
        # Dhan serializes strike keys with six decimals; parsing float avoids
        # locale/string formatting assumptions and preserves one-sided strikes.
        native = strike_data.get(strike, {})
        fields = sample_raw(c, spot) if demo else (native or {}).get(c['option_type'].lower(), {})
        # Reject a response contract mismatch instead of valuing another token.
        if fields and not demo and str(fields.get('security_id')) != str(c['token']):
            fields = {}
        leg = normalize_leg(fields, c, stamp)
        row[c['option_type'].lower()] = leg
        if leg['ltp'] is not None and leg['ltp'] > 0:
            quotes.append({**{k: c.get(k) for k in ('underlying','exchange','segment','symbol','expiry','strike','option_type')},
                           'broker': c['broker'], 'instrument_id': c['id'], 'ltp': leg['ltp'], 'bid': leg['bid'], 'ask': leg['ask'],
                           'open_interest': leg['oi'], 'volume': leg['volume'], 'received_at': stamp, 'demo': demo,
                           'source': 'synthetic_option_chain' if demo else 'dhan_option_chain', 'exchange_timestamp': None,
                           'iv': leg['iv'], **{g: leg[g] for g in ('delta','gamma','theta','vega')}})
    values = sorted(rows.values(), key=lambda r: r['strike'])
    atm = min(rows, key=lambda strike: abs(strike-spot)) if rows and spot is not None else None
    for row in values:
        ce, pe = row['ce'], row['pe']
        row.update(atm=row['strike'] == atm, itm_call=spot is not None and row['strike'] < spot, itm_put=spot is not None and row['strike'] > spot,
                   pcr=pe['oi']/ce['oi'] if ce and pe and ce['oi'] is not None and ce['oi'] > 0 and pe['oi'] is not None else None)
    call_values = [r['ce']['oi'] for r in values if r['ce'] and r['ce']['oi'] is not None]
    put_values = [r['pe']['oi'] for r in values if r['pe'] and r['pe']['oi'] is not None]
    calls, puts = sum(call_values) if call_values else None, sum(put_values) if put_values else None
    atm_ivs = [leg['iv'] for row in values if row['atm'] for leg in (row['ce'], row['pe']) if leg and leg['iv'] is not None]
    summary = {'atm_iv': sum(atm_ivs)/len(atm_ivs) if atm_ivs else None, 'pcr': puts/calls if calls is not None and calls > 0 and puts is not None else None,
               'lot_size': instruments[0]['lot_size'] if instruments else metadata.get('lot_size'),
               'days_to_expiry': max(0, (datetime.fromisoformat(expiry).date()-datetime.now(IST).date()).days),
               'total_call_oi': calls, 'total_put_oi': puts}
    return {'underlying': metadata, 'expiry': expiry, 'expiries': metadata['expiries'], 'mode': 'sample' if demo else 'live', 'demo': demo,
            'source': 'Fictional sample option chain' if demo else 'Dhan option-chain Data API', 'source_url': SOURCE,
            'quantity_unit': 'units' if demo else 'broker_reported',
            'updated_at': stamp, 'as_of': stamp, 'spot': spot, 'summary': summary, 'metrics': summary, 'lot_size': summary['lot_size'],
            'rows': values, 'instruments': instruments, 'quotes': quotes, 'refresh_after_seconds': MIN_INTERVAL}


async def dhan_post(path, body):
    if path not in {'/optionchain', '/optionchain/expirylist'}:
        raise ValueError('Only option-chain market-data endpoints are supported')
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        response = await client.post('https://api.dhan.co/v2'+path, json=body,
            headers={'access-token': os.environ['DHAN_ACCESS_TOKEN'], 'client-id': os.environ['DHAN_CLIENT_ID']})
        if response.status_code in {401,403}:
            raise HTTPException(503, 'Dhan market-data access requires a valid session and Data API permission.')
        if response.status_code == 429:
            raise HTTPException(429, 'Dhan option-chain rate limit reached. Wait before refreshing.')
        if response.status_code != 200:
            raise HTTPException(503, 'The Dhan market-data service is unavailable. Last received data is retained.')
        try:
            payload = response.json()
        except ValueError:
            raise HTTPException(503, 'Dhan returned an unreadable market-data response.') from None
        if not isinstance(payload, dict) or payload.get('status') != 'success':
            raise HTTPException(503, 'Dhan did not return a successful option-chain data response.')
        return payload


async def active_expiries(metadata):
    key = metadata['key']
    cached = _expiry_cache.get(key)
    if cached and time.monotonic()-cached[0] < 300:
        return cached[1]
    payload = await dhan_post('/optionchain/expirylist', {'UnderlyingScrip': metadata['underlying_security_id'], 'UnderlyingSeg': metadata['underlying_segment']})
    data = payload.get('data')
    if not isinstance(data, list):
        raise HTTPException(503, 'Dhan returned an unreadable expiry list.')
    expiries = sorted({v for v in data if isinstance(v, str) and v in metadata['expiries']})
    if not expiries:
        raise HTTPException(503, 'The broker expiry list and current instrument master do not match. Refresh the catalog.')
    _expiry_cache[key] = (time.monotonic(), expiries)
    return expiries


def install_contracts(db, contracts, revision):
    if engine.catalog is None:
        raise HTTPException(503, 'The instrument catalog is not ready.')
    identity = (contracts[0].get('chain_key'), revision) if contracts else None
    if identity != getattr(engine.catalog, '_selected_chain_identity', None):
        active_ids = {leg['instrument_id'] for row in db.scalars(select(Trade).where(Trade.status.in_(['open', 'pending'])))
                      for leg in row.data.get('legs', []) if leg.get('status') in {'open', 'pending'}}
        engine.catalog.select_chain_contracts(contracts, active_ids)
        engine.catalog._selected_chain_identity = identity


def journal_chain(db, quotes):
    """Keep a bounded latest view; durable receipts are only for active trades."""
    active = [leg for row in db.scalars(select(Trade).where(Trade.status.in_(['open','pending'])))
              if row.data.get('demo') is False for leg in row.data.get('legs', [])]
    identities = {engine.contract_identity(leg) for leg in active}
    for quote in quotes:
        if engine.contract_identity(quote) in identities:
            engine.record_quote(db, quote, process=False)
        else:
            # Browsing hundreds of strikes must not journal hundreds of price
            # histories. A later paper entry can use this timestamped mark.
            engine.cache_quote(db, quote, persist=False)
    db.commit()
    while engine.recover_pending_quotes(db):
        db.commit()


def persist_snapshot(db, key, result):
    journal_chain(db, result['quotes'])
    cache = _snapshots.setdefault(db.get_bind(), OrderedDict())
    cache[key] = result
    cache.move_to_end(key)
    while len(cache) > MAX_SELECTED_CHAINS:
        cache.popitem(last=False)


@router.get('/underlyings')
async def underlyings(q: str = Query('', max_length=100), mode: Literal['sample','live'] | None = None,
                      broker: Literal['dhan','kotak','fivepaisa'] = 'dhan', db: Session = Depends(get_db)):
    selected_mode = mode_value(db, mode)
    if selected_mode == 'sample':
        rows = sample_universe(engine.catalog, broker)
        status = {**master_status(db), 'sample_notice': 'Sample contracts and stock examples are fictional; choose Market data for the complete official F&O universe.'}
    else:
        if broker != 'dhan':
            return {'underlyings': [], 'total': 0, 'mode': 'live', 'demo': False, 'configured': False,
                    'master': master_status(db), 'message': 'The full option-chain service currently uses Dhan market data. Other broker feeds remain independent.'}
        status = await refresh_master(db)
        rows = master_index(db)['rows']
    query = q.strip().upper()
    filtered = [r for r in rows if not query or query in r['symbol'].upper() or query in r['name'].upper()]
    filtered.sort(key=lambda r: (r['kind'] != 'index', r['exchange'] != 'NSE', r['symbol']))
    return {'underlyings': filtered, 'total': len(rows), 'filtered_total': len(filtered), 'mode': selected_mode,
            'demo': selected_mode == 'sample', 'master': status, 'configured': configured() if broker == 'dhan' else False}


@router.post('/master/refresh')
async def reload_master(db: Session = Depends(get_db)):
    return {'ok': True, 'master': await refresh_master(db, force=True)}


@router.get('')
async def option_chain(underlying: str = Query(..., min_length=1, max_length=180), expiry: str | None = None,
                       mode: Literal['sample','live'] | None = None, broker: Literal['dhan','kotak','fivepaisa'] = 'dhan',
                       db: Session = Depends(get_db)):
    selected_mode = mode_value(db, mode)
    demo = selected_mode == 'sample'
    workspace_matches = bool(engine.get_settings(db)['demo_mode']) == demo
    if demo:
        choices = sample_universe(engine.catalog, broker)
        metadata = next((r for r in choices if r['key'] == underlying or r['symbol'] == underlying), None)
        if not metadata:
            raise HTTPException(404, 'Choose a sample underlying from the catalog.')
        contracts = [c for c in engine.catalog.all(demo=True) if c['underlying'] == metadata['symbol'] and c['exchange'] == metadata['exchange'] and c['broker'] == broker]
    else:
        if broker != 'dhan':
            raise HTTPException(422, 'Full live option chains use the Dhan market-data source. No broker order is sent.')
        await refresh_master(db)
        master = selected_master(db, underlying)
        if not master:
            raise HTTPException(404, 'Choose a stock or index from the current official F&O catalog.')
        metadata, contracts, revision = master
        metadata = copy.deepcopy(metadata)
        install_contracts(db, contracts, revision)
    expiry = expiry or metadata['expiries'][0]
    if expiry not in metadata['expiries']:
        raise HTTPException(422, 'Choose an active expiry from this underlying’s master.')
    base = build_chain(metadata, contracts, expiry, demo=demo, stamp=iso_now() if demo else None)
    base.update(status='sample' if demo else 'credentials_required', stale=not demo, mode_matches_workspace=workspace_matches,
                trading_enabled=demo and workspace_matches, read_only=True)
    if demo:
        base['message'] = 'Fictional prices, Greeks, lot sizes and stock examples for practice. No market data is implied.'
        return base
    if not configured():
        base['message'] = 'All listed contracts come from the official instrument master. Add Dhan Data API credentials on the backend for live prices, OI and Greeks.'
        return base
    key = f'{underlying}:{expiry}'
    async with _request_lock:
        cache = _snapshots.setdefault(db.get_bind(), OrderedDict())
        cached = cache.get(key)
        if cached and age(cached['updated_at']) < MIN_INTERVAL:
            cache.move_to_end(key)
            return {**cached, 'stale': age(cached['updated_at']) > 10, 'cached': True,
                    'mode_matches_workspace': workspace_matches, 'trading_enabled': workspace_matches}
        attempt_key = (db.get_bind(), key)
        if time.monotonic()-_attempts.get(attempt_key, -MIN_INTERVAL) < MIN_INTERVAL:
            if cached:
                return {**cached, 'stale': True, 'cached': True, 'trading_enabled': False, 'mode_matches_workspace': workspace_matches,
                        'message': 'Refresh is rate limited; the timestamp shows the last received broker snapshot.'}
            raise HTTPException(429, 'Wait three seconds before requesting this option chain again.')
        _attempts[attempt_key] = time.monotonic()
        if len(_attempts) > 256:
            del _attempts[next(iter(_attempts))]
        try:
            metadata['expiries'] = await active_expiries(metadata)
            if expiry not in metadata['expiries']:
                raise HTTPException(422, 'The broker no longer lists this expiry. Refresh the catalog.')
            raw = await dhan_post('/optionchain', {'UnderlyingScrip': metadata['underlying_security_id'], 'UnderlyingSeg': metadata['underlying_segment'], 'Expiry': expiry})
            if not isinstance((raw.get('data') or {}).get('oc'), dict):
                raise HTTPException(503, 'Dhan returned an option chain without strike data.')
            stamp = iso_now()
            result = build_chain(metadata, contracts, expiry, raw, stamp)
            if not result['quotes']:
                raise HTTPException(503, 'The broker returned no usable prices for the selected expiry. No values were invented.')
            result.update(status='connected', stale=False, cached=False, read_only=True, mode_matches_workspace=workspace_matches, trading_enabled=workspace_matches)
            async with _write_lock:
                await asyncio.to_thread(persist_snapshot, db, key, result)
            return result
        except (httpx.HTTPError, HTTPException) as exc:
            if cached:
                return {**cached, 'status': 'stale', 'stale': True, 'cached': True, 'trading_enabled': False,
                        'mode_matches_workspace': workspace_matches, 'message': 'The broker snapshot could not be refreshed. Last received data is retained.'}
            if isinstance(exc, HTTPException):
                base['status'] = 'rate_limited' if exc.status_code == 429 else 'unavailable'
                base['message'] = exc.detail
                return base
            return {**base, 'status': 'unavailable', 'message': 'The broker snapshot is unavailable. No values were invented.'}
