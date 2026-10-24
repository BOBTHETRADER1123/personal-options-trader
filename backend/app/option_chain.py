"""Two independent, read-only Kotak Neo and 5paisa option-chain views.

Live identities come only from the selected broker's official master. Prices
are that broker's received WebSocket marks; unavailable fields remain null.
Legacy chain tables remain defined so historical databases load without loss.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from weakref import WeakKeyDictionary

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import engine
from .database import Base, get_db, iso_now
from .instruments import BROKERS, FIVEPAISA_MASTER_URL, IST, normalize_row
from .security import require_session
from .models import LatestQuote
from .brokers import KotakFeed, FivepaisaFeed

router = APIRouter(prefix='/api/option-chain', tags=['Read-only option chain'], dependencies=[Depends(require_session)])
_write_lock = asyncio.Lock()
_master_lock = asyncio.Lock()
_refresh_feeds = None
_requested: dict[str, tuple[float, set[str]]] = {}
_attempts: dict[str, float] = {}
_universes = WeakKeyDictionary()
MIN_INTERVAL = 3.0
MASTER_REFRESH_SECONDS = 300
CHAIN_SUBSCRIPTION_SECONDS = 600
SOURCES = {'kotak': 'https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md',
           'fivepaisa': 'https://xstream.5paisa.com/dev-docs/market-data-system/web-socket'}


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


def configure(write_lock, refresh_selected_feeds=None):
    global _write_lock, _refresh_feeds
    _write_lock, _refresh_feeds = write_lock, refresh_selected_feeds


def number(value, *, positive=False):
    try:
        result = float(value)
        return None if not math.isfinite(result) or (positive and result < 0) else result
    except (TypeError, ValueError, OverflowError):
        return None


def percent(change, previous):
    return round(change / previous * 100, 4) if change is not None and previous is not None and previous > 0 else None


def age(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError, AttributeError):
        return float('inf')


def validate_broker(broker):
    if broker not in BROKERS:
        raise HTTPException(422, 'Only Kotak Neo and 5paisa are enabled. Historical records remain readable.')


def configured(broker='kotak'):
    validate_broker(broker)
    return bool((KotakFeed if broker == 'kotak' else FivepaisaFeed)(None, None).configured())


def mode_value(db, mode):
    return mode or ('sample' if engine.get_settings(db)['demo_mode'] else 'live')


def requested_contract_ids():
    now = time.monotonic()
    for key in list(_requested):
        if _requested[key][0] <= now:
            _requested.pop(key, None)
    return set().union(*(value[1] for value in _requested.values())) if _requested else set()


def parse_master(rows, broker='fivepaisa', today=None, segment=None):
    validate_broker(broker)
    today = today or datetime.now(IST).date().isoformat()
    groups, seen = {}, set()
    for raw in rows:
        try:
            contract = normalize_row(broker, raw, segment)
        except ValueError as exc:
            if str(exc) in {'Not an option contract', 'Unsupported derivative segment'}:
                continue
            raise
        if contract['expiry'] < today:
            continue
        # Preserve futures in the downloadable catalog but option-chain groups
        # contain options only, with their exact master expiry and lot sizes.
        if contract['option_type'] not in {'CE', 'PE'}:
            continue
        signature = (contract['exchange'], contract['underlying'], contract['expiry'], contract['strike'], contract['option_type'])
        if signature in seen:
            raise ValueError('Duplicate contract identity in broker master; previous master retained.')
        seen.add(signature)
        key = f"{contract['exchange']}:{contract['underlying']}"
        kind = 'commodity' if contract['exchange'] == 'MCX' else 'index' if contract['underlying'] in {'NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX', 'NIFTYNXT50'} else 'stock'
        contract['chain_key'] = key
        group = groups.setdefault(key, {'key': key, 'broker': broker, 'symbol': contract['underlying'], 'name': contract['underlying'], 'exchange': contract['exchange'], 'kind': kind, 'type': kind, 'contracts': []})
        group['contracts'].append(contract)
    for group in groups.values():
        group['expiries'] = sorted({c['expiry'] for c in group['contracts']})
        group['lot_size'] = next(c['lot_size'] for c in group['contracts'] if c['expiry'] == group['expiries'][0])
        group['contract_count'] = len(group['contracts'])
    return groups


def live_universe(broker):
    validate_broker(broker)
    if engine.catalog is None:
        return {}
    today = datetime.now(IST).date().isoformat()
    cache = _universes.setdefault(engine.catalog, {})
    identity = (engine.catalog.revision, today)
    if broker in cache and cache[broker][0] == identity:
        return cache[broker][1]
    rows = [c for c in engine.catalog._live if c['broker'] == broker and c['expiry'] >= today]
    # Existing normalized identities are retained to support open positions.
    groups = {}
    indices = {'NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX', 'NIFTYNXT50'}
    for c in rows:
        if c['option_type'] not in {'CE', 'PE'}:
            continue
        key = f"{c['exchange']}:{c['underlying']}"
        kind = 'commodity' if c['exchange'] == 'MCX' else 'index' if c['underlying'] in indices else 'stock'
        group = groups.setdefault(key, {'key': key, 'broker': broker, 'symbol': c['underlying'], 'name': c['underlying'], 'exchange': c['exchange'], 'kind': kind, 'type': kind, 'contracts': []})
        group['contracts'].append(c)
    for group in groups.values():
        group['expiries'] = sorted({c['expiry'] for c in group['contracts']})
        group['lot_size'] = next(c['lot_size'] for c in group['contracts'] if c['expiry'] == group['expiries'][0])
        group['contract_count'] = len(group['contracts'])
        group['updated_at'] = min((c['imported_at'] for c in group['contracts'] if c.get('imported_at')), default=None)
    cache[broker] = (identity, groups)
    return groups


def master_status(db=None, broker='kotak'):
    rows = live_universe(broker)
    dates = [row['updated_at'] for row in rows.values() if row.get('updated_at')]
    stamp = min(dates, default=None)
    return {'status': 'ready' if rows else 'not_loaded', 'broker': broker, 'updated_at': stamp,
            'source': 'selected_broker_instrument_master', 'underlying_count': len(rows),
            'stale': bool(stamp and age(stamp) > 86400), 'read_only': True}


def download_master(broker):
    validate_broker(broker)
    segments = ('nse_fo', 'bse_fo', 'mcx_fo') if broker == 'kotak' else (None,)
    client = None
    if broker == 'kotak':
        os.environ['NEO_LOG_LEVEL'] = 'NOLOG'
        os.environ['NEO_LOG_FILE_ENABLED'] = 'false'
        from neo_api_client import NeoAPI
        from neo_api_client.logger import setup_logging
        setup_logging(level='NOLOG', file_enabled=False)
        if not os.getenv('KOTAK_CONSUMER_KEY'):
            raise ValueError('Kotak consumer key required for official instrument master')
        client = NeoAPI(consumer_key=os.environ['KOTAK_CONSUMER_KEY'], environment='prod', timeout=30)
    result = []
    total = 0
    for segment in segments:
        url = client.scrip_master(exchange_segment=segment) if client else FIVEPAISA_MASTER_URL
        if not isinstance(url, str) or not url.startswith('https://'):
            raise ValueError('Broker did not return an HTTPS master URL')
        with urllib.request.urlopen(url, timeout=90) as response:
            raw = response.read(100 * 1024 * 1024 + 1)
        total += len(raw)
        if total > 100 * 1024 * 1024:
            raise ValueError('Broker master exceeds 100 MiB bounded download size')
        for row in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))):
            try:
                result.append(normalize_row(broker, row, segment))
            except ValueError as exc:
                if str(exc) not in {'Not an option contract', 'Unsupported derivative segment'}:
                    raise
    if not result:
        raise ValueError('No validated current derivatives found; previous master retained')
    return result


def save_master(broker, contracts):
    path = engine.catalog.path
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'instruments': []}
    old = previous.get('instruments', []) if isinstance(previous, dict) else previous
    rows = [r for r in old if r.get('broker') != broker] + contracts
    encoded = json.dumps({'updated_at': iso_now(), 'instruments': rows}, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > 256 * 1024 * 1024:
        raise ValueError('Instrument cache exceeds 256 MiB bound')
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(encoded, encoding='utf-8')
    temporary.replace(path)
    engine.catalog.reload()


async def refresh_master(db, force=False, broker='kotak'):
    validate_broker(broker)
    async with _master_lock:
        state = master_status(db, broker)
        if state['updated_at'] and age(state['updated_at']) < (MASTER_REFRESH_SECONDS if force else 86400):
            return state
        if time.monotonic() - _attempts.get(broker, -MASTER_REFRESH_SECONDS) < MASTER_REFRESH_SECONDS:
            return {**state, 'message': 'Refresh is rate limited to one attempt per five minutes.'}
        _attempts[broker] = time.monotonic()
        db.rollback()  # Do not hold a Neon transaction during a master download.
        try:
            contracts = await asyncio.to_thread(download_master, broker)
            async with _write_lock:
                await asyncio.to_thread(save_master, broker, contracts)
            return master_status(db, broker)
        except Exception as exc:
            return {**state, 'status': 'cached' if state['underlying_count'] else 'unavailable',
                    'message': f'Official {broker} master unavailable ({type(exc).__name__}); existing contracts retained.'}


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
                            'segment': 'nse_fo' if broker == 'kotak' else 'D',
                            'expiry': expiry, 'strike': strike, 'option_type': kind, 'lot_size': lot, 'tick_size': .05,
                            'demo': True, 'source': 'synthetic_demo', 'spot': spot, 'base_price': round(intrinsic + max(step*.2, step*2.8-abs(offset)*step*.18), 2),
                            'price_multiplier': 1.0, 'multiplier_verified': True, 'ltp': intrinsic + step})


def sample_universe(catalog, broker='kotak'):
    ensure_sample_stocks(catalog)
    groups = {}
    indices = {'NIFTY', 'BANKNIFTY', 'SENSEX'}
    for c in catalog.all(demo=True) if catalog else []:
        if c['broker'] != broker or c['exchange'] not in {'NSE', 'BSE', 'MCX'} or c['option_type'] not in {'CE', 'PE'}:
            continue
        key = f"{c['exchange']}:{c['underlying']}"
        group = groups.setdefault(key, {'key': key, 'symbol': c['underlying'], 'name': c['underlying'], 'exchange': c['exchange'],
                                       'kind': 'commodity' if c['exchange'] == 'MCX' else 'index' if c['underlying'] in indices else 'stock', 'type': 'commodity' if c['exchange'] == 'MCX' else 'index' if c['underlying'] in indices else 'stock',
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
            'buildup': buildup, 'quote_at': raw.get('received_at', stamp) if raw else None, 'exchange_timestamp': raw.get('exchange_timestamp'), 'stale': raw.get('stale', not bool(raw)), 'demo': contract.get('demo', False), 'contract': dict(contract)}


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
        # Numeric strike matching is independent of locale/string formatting.
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
                           'open_interest': leg['oi'], 'volume': leg['volume'], 'received_at': leg['quote_at'], 'demo': demo,
                           'source': 'synthetic_option_chain' if demo else c['broker'], 'exchange_timestamp': leg['exchange_timestamp'],
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
            'source': 'Fictional sample option chain' if demo else metadata.get('broker', '') + ' independent market feed', 'source_url': SOURCES.get(metadata.get('broker'), ''),
            'quantity_unit': 'units' if demo else 'broker_reported',
            'updated_at': stamp, 'as_of': stamp, 'spot': spot, 'summary': summary, 'metrics': summary, 'lot_size': summary['lot_size'],
            'rows': values, 'instruments': instruments, 'quotes': quotes, 'refresh_after_seconds': MIN_INTERVAL}



@router.get('/underlyings')
async def underlyings(q: str = Query('', max_length=100), mode: Literal['sample','live'] | None = None,
                      broker: Literal['kotak','fivepaisa'] = 'kotak', db: Session = Depends(get_db)):
    validate_broker(broker)
    selected_mode = mode_value(db, mode)
    groups = sample_universe(engine.catalog, broker) if selected_mode == 'sample' else [{k:v for k,v in g.items() if k != 'contracts'} for g in live_universe(broker).values()]
    query = q.strip().upper()
    rows = [r for r in groups if not query or query in r['symbol'].upper() or query in r['name'].upper()]
    rows.sort(key=lambda r: (r['kind'] != 'index', r['exchange'] != 'NSE', r['symbol']))
    return {'underlyings': rows, 'total': len(groups), 'filtered_total': len(rows), 'mode': selected_mode,
            'demo': selected_mode == 'sample', 'master': master_status(db, broker), 'configured': configured(broker),
            'message': 'Import or refresh this broker’s official master to list every entitled NSE/BSE/MCX option. Missing live fields remain unavailable.'}


@router.post('/master/refresh')
async def reload_master(broker: Literal['kotak','fivepaisa'] = 'kotak', db: Session = Depends(get_db)):
    result = await refresh_master(db, force=True, broker=broker)
    if _refresh_feeds:
        await _refresh_feeds(db)
    return {'ok': result['status'] in {'ready', 'cached'}, 'master': result}


def cached_live_data(db, contracts, expiry, stale_after):
    oc, stamps = {}, []
    active = [c for c in contracts if c['expiry'] == expiry]
    broker = active[0]['broker'] if active else None
    # One bounded database lookup, not two queries for every missing strike.
    state = engine._cache(db)
    with engine._cache_lock:
        marks = {key: dict(value) for key, value in state['quotes'].items() if key[0] == broker}
    missing = [c['id'] for c in active if (broker, c['id']) not in marks]
    if missing:
        for row in db.scalars(select(LatestQuote).where(LatestQuote.broker == broker, LatestQuote.instrument_id.in_(missing))):
            marks[(row.broker, row.instrument_id)] = row.data
    for contract in active:
        if contract['expiry'] != expiry:
            continue
        quote = marks.get((contract['broker'], contract['id']))
        if not quote or quote.get('demo') is not False or quote.get('broker') != contract['broker']:
            continue
        stamp = quote.get('received_at')
        stamps.append(stamp)
        raw = {'security_id': contract['token'], 'last_price': quote.get('ltp'), 'top_bid_price': quote.get('bid'),
               'top_ask_price': quote.get('ask'), 'oi': quote.get('open_interest'), 'volume': quote.get('volume'),
               'previous_close_price': quote.get('previous_close'), 'previous_oi': quote.get('previous_open_interest'),
               'received_at': stamp, 'exchange_timestamp': quote.get('exchange_timestamp'), 'stale': age(stamp) > stale_after}
        # No Greeks from a different broker and no made-up Black-Scholes values.
        oc.setdefault(str(contract['strike']), {})[contract['option_type'].lower()] = raw
    return {'data': {'oc': oc}}, min((s for s in stamps if s), default=None)


@router.get('')
async def option_chain(underlying: str = Query(..., min_length=1, max_length=180), expiry: str | None = None,
                       mode: Literal['sample','live'] | None = None, broker: Literal['kotak','fivepaisa'] = 'kotak',
                       db: Session = Depends(get_db)):
    validate_broker(broker)
    selected_mode = mode_value(db, mode)
    demo = selected_mode == 'sample'
    settings = engine.get_settings(db)
    workspace_matches = bool(settings['demo_mode']) == demo
    if demo:
        choices = sample_universe(engine.catalog, broker)
        metadata = next((r for r in choices if r['key'] == underlying or r['symbol'] == underlying), None)
        contracts = [c for c in engine.catalog.all(demo=True) if metadata and c['broker'] == broker and c['exchange'] == metadata['exchange'] and c['underlying'] == metadata['symbol'] and c['option_type'] in {'CE','PE'}]
    else:
        group = live_universe(broker).get(underlying)
        metadata = {k:v for k,v in group.items() if k != 'contracts'} if group else None
        contracts = group['contracts'] if group else []
    if not metadata:
        raise HTTPException(404, 'Choose an underlying from this broker’s instrument catalog. Refresh its master if missing.')
    metadata = {**metadata, 'broker': broker}
    expiry = expiry or metadata['expiries'][0]
    if expiry not in metadata['expiries']:
        raise HTTPException(422, 'Choose an active expiry from this underlying’s master.')
    selected = [c for c in contracts if c['expiry'] == expiry]
    if demo:
        result = build_chain(metadata, selected, expiry, demo=True, stamp=iso_now())
        result.update(status='sample', stale=False, trading_enabled=workspace_matches,
                      message='Fictional prices, Greeks and quantities for practice; no live market data is implied.')
    else:
        # Only the most recently viewed chain per broker receives a temporary
        # subscription. Active positions are retained independently by main.
        _requested[broker] = (time.monotonic() + CHAIN_SUBSCRIPTION_SECONDS, {c['id'] for c in selected})
        if _refresh_feeds:
            await _refresh_feeds(db)
        raw, stamp = cached_live_data(db, selected, expiry, settings['stale_after_seconds'])
        result = build_chain(metadata, selected, expiry, raw=raw, stamp=stamp)
        fresh = [q for row in result['rows'] for q in (row['ce'], row['pe']) if q and q['ltp'] is not None and not q['stale']]
        result.update(status='connected' if fresh else 'stale' if result['quotes'] else 'awaiting_quotes' if configured(broker) else 'credentials_required',
                      stale=not bool(fresh), trading_enabled=workspace_matches and bool(fresh),
                      message='Prices are from the selected broker only. OI, spot and Greeks remain unavailable when its feed does not supply them; stale rows retain their original quote timestamps.',
                      capabilities={'ltp': True, 'bid_ask': True, 'open_interest': True, 'spot': False, 'greeks': False,
                                    'greeks_note': '5paisa offers a separate NSE-only Greeks feed; binary wire format/entitlements need broker verification. Kotak depth messages do not carry Greeks.'})
    result.update(read_only=True, mode_matches_workspace=workspace_matches)
    return result
