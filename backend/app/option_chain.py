"""Read-only Kotak Neo option-chain views.

Live identities come only from the selected broker's official master. Prices
are that broker's received WebSocket marks; unavailable fields remain null.
Legacy chain tables remain defined so historical databases load without loss.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from weakref import WeakKeyDictionary

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import engine
from .database import Base, get_db, iso_now, bounded_read, SessionLocal
from .instruments import BROKERS, IST, normalize_row, ContractCatalog
from .security import require_session
from .models import LatestQuote, Trade
from .brokers import KotakFeed
from .master_diagnostics import safe_master_traceback
from .kotak_master import download as official_master_download, MasterRefreshError, SEGMENTS

log=logging.getLogger('personal_options_trader.master')

router = APIRouter(prefix='/api/option-chain', tags=['Read-only option chain'], dependencies=[Depends(require_session)])
_write_lock = asyncio.Lock()
_master_lock = asyncio.Lock()
_refresh_feeds = None
_requested: dict[str, tuple[float, set[str]]] = {}
_attempts: dict[str, float] = {}
_universes = WeakKeyDictionary()
_refresh_states = WeakKeyDictionary()
_master_summaries = WeakKeyDictionary()
_master_jobs: dict[str, asyncio.Task] = {}
MIN_INTERVAL = 3.0
MASTER_REFRESH_SECONDS = 300  # Reuse a validated current catalog; not a failure cooldown.
MASTER_RETRY_SECONDS = 15
CHAIN_SUBSCRIPTION_SECONDS = 600
SOURCES = {'kotak': 'https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md'}


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
        raise HTTPException(422, 'Only Kotak Neo is enabled. Retired broker history remains readable.')


def configured(broker='kotak'):
    validate_broker(broker)
    return bool(KotakFeed(None, None).configured())


def mode_value(db, mode):
    return mode or ('sample' if engine.get_settings(db)['demo_mode'] else 'live')


def requested_contract_ids():
    now = time.monotonic()
    for key in list(_requested):
        if _requested[key][0] <= now:
            _requested.pop(key, None)
    return set().union(*(value[1] for value in _requested.values())) if _requested else set()


def parse_master(rows, broker='kotak', today=None, segment=None):
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
    futures = {}
    for c in rows:
        if c['option_type'] == 'FUT':
            futures.setdefault((c['exchange'], c['underlying']), []).append(c)
    for group in groups.values():
        group['expiries'] = sorted({c['expiry'] for c in group['contracts']})
        group['lot_size'] = next(c['lot_size'] for c in group['contracts'] if c['expiry'] == group['expiries'][0])
        group['contract_count'] = len(group['contracts'])
        group['reference_futures'] = sorted(futures.get((group['exchange'], group['symbol']), []), key=lambda c: (c['expiry'], c['id']))
        group['updated_at'] = min((c['imported_at'] for c in group['contracts'] if c.get('imported_at')), default=None)
    cache[broker] = (identity, groups)
    return groups


def _refresh_state(broker):
    if engine.catalog is None:
        return {}
    states = _refresh_states.setdefault(engine.catalog, {})
    if broker not in states:
        try:
            saved = json.loads(engine.catalog.path.with_suffix('.refresh-status.json').read_text(encoding='utf-8'))
            # This app-created sidecar contains structured diagnostics only.
            states[broker] = saved.get(broker, {}) if isinstance(saved, dict) else {}
        except (OSError, ValueError, TypeError):
            states[broker] = {}
    return states[broker]


def _master_summary(broker):
    """Status needs small metadata, without constructing the option-chain universe."""
    catalog = engine.catalog
    if catalog is None:
        return 0, None
    today = datetime.now(IST).date().isoformat()
    key = (catalog.revision, today, broker)
    cached = _master_summaries.get(catalog)
    if cached is not None and cached[0] == key:
        return cached[1]
    groups, legacy_stamp = set(), None
    for row in catalog._live:
        if (row.get('broker') != broker or row.get('expiry', '') < today
                or row.get('option_type') not in {'CE', 'PE'}):
            continue
        groups.add((row['exchange'], row['underlying']))
        stamp = row.get('imported_at')
        if stamp and (legacy_stamp is None or stamp < legacy_stamp):
            legacy_stamp = stamp
    result = (len(groups), legacy_stamp)
    _master_summaries[catalog] = (key, result)
    return result


def master_status(db=None, broker='kotak'):
    validate_broker(broker)
    count, legacy_stamp = _master_summary(broker)
    metadata = (getattr(engine.catalog, 'master_metadata', {}) or {}).get(broker, {})
    state = _refresh_state(broker)
    stamp = metadata.get('last_successful_refresh_at')
    segments = metadata.get('segments', {})
    today = datetime.now(IST).date().isoformat()
    verified = bool(stamp and age(stamp) <= 86400 and set(segments) == set(SEGMENTS)
                    and all(value.get('source_date') == today for value in segments.values()))
    job = _master_jobs.get(broker)
    running = _master_lock.locked() or (job is not None and not job.done())
    return {'status': 'ready' if count else 'not_loaded', 'broker': broker,
            'updated_at': stamp or legacy_stamp, 'last_successful_refresh_at': stamp,
            'last_attempt_at': state.get('last_attempt_at'), 'last_error': state.get('last_error'),
            'refresh_stage': state.get('refresh_stage') if running else None,
            'last_completed_at': state.get('last_completed_at'),
            'refresh_in_progress': running,
            'retry_after_seconds': max(0, math.ceil(MASTER_RETRY_SECONDS - (time.monotonic() - _attempts.get(broker, -MASTER_RETRY_SECONDS)))),
            'segment_status': segments,
            'sources': [{'segment': segment, **details} for segment, details in segments.items()],
            'source': 'selected_broker_instrument_master', 'underlying_count': count,
            'stale': not verified, 'new_trades_allowed': verified, 'read_only': True}


def download_master(broker):
    validate_broker(broker)
    return official_master_download()


def prepare_master(broker, contracts, retained_ids=None):
    """Validate a complete replacement before atomic publication; no trade writes."""
    path = engine.catalog.path
    path.parent.mkdir(parents=True, exist_ok=True)
    retained_ids = set(retained_ids or ())
    imported = list(contracts)
    if not imported:
        raise MasterRefreshError('empty_master', 'cache_validation')
    new_ids = {row['id'] for row in imported}
    if len(new_ids) != len(imported):
        raise MasterRefreshError('duplicate_contract_id', 'cache_validation')
    # Saved journal legs are immutable history. Missing old identities can still
    # be marked/exited, but must not certify a new exposure after a refresh.
    previous = engine.catalog._live
    retained = [{**row, 'master_active': False} for row in previous
                if row.get('broker') == broker and row['id'] in retained_ids and row['id'] not in new_ids]
    rows = [row for row in previous if row.get('broker') != broker] + imported + retained
    metadata = dict(getattr(engine.catalog, 'master_metadata', {}) or {})
    metadata[broker] = dict(getattr(contracts, 'metadata', {}))
    payload = {'updated_at': iso_now(), 'instruments': rows, 'master_refresh': metadata}
    encoded = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    if len(encoded) > 256 * 1024 * 1024:
        raise MasterRefreshError('cache_size_limit', 'cache_validation')
    temporary = path.with_suffix('.candidate.json')
    try:
        with temporary.open('wb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        candidate = ContractCatalog(temporary)
        if candidate.error:
            raise MasterRefreshError('invalid_normalized_cache', 'cache_validation')
        return candidate
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _assert_active_identities(candidate, trades):
    current = {row['id']: row for row in candidate._live}
    keys = ('expiry', 'underlying', 'exchange', 'segment', 'option_type', 'strike', 'lot_size')
    for trade in trades:
        if trade.get('demo') is not False or trade.get('status') not in {'open', 'pending'}:
            continue
        for leg in trade.get('legs', []):
            if leg.get('status') not in {'open', 'pending'}:
                continue
            row = current.get(leg.get('instrument_id'))
            if row and any(leg.get(key) != row.get(key) for key in keys):
                raise MasterRefreshError('active_contract_identity_changed', 'cache_validation')


def publish_master(candidate):
    candidate.path.replace(engine.catalog.path)
    # The expensive encoding, fsync and validation already happened outside
    # the writer lock. Publication is one rename plus reference replacement.
    engine.catalog._live = candidate._live
    engine.catalog.master_metadata = candidate.master_metadata
    engine.catalog.error = None
    engine.catalog.revision += 1


def save_master(broker, contracts, retained_ids=None):
    candidate = prepare_master(broker, contracts, retained_ids)
    publish_master(candidate)


def _referenced_trades(db, broker):
    with bounded_read(db):
        return db.scalars(select(Trade.data).where(Trade.broker == broker)).all()


def _persist_refresh_state(broker, state):
    # Persist only sanitized, bounded app-generated diagnostics, never payloads.
    path = engine.catalog.path.with_suffix('.refresh-status.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({broker: state}, separators=(',', ':')).encode('utf-8')
    if len(data) > 8192:
        raise ValueError('Master diagnostic state exceeds bound')
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as output:
        output.write(data); output.flush(); os.fsync(output.fileno())
    temporary.replace(path)


async def refresh_master(db, force=False, broker='kotak'):
    validate_broker(broker)
    if _master_lock.locked():
        return {**master_status(db, broker), 'message': 'An official contract refresh is already in progress.'}
    async with _master_lock:
        current = master_status(db, broker)
        if current['new_trades_allowed'] and current['updated_at'] and age(current['updated_at']) < (MASTER_REFRESH_SECONDS if force else 86400):
            return {**current, 'refresh_in_progress': False}
        remaining = math.ceil(MASTER_RETRY_SECONDS - (time.monotonic() - _attempts.get(broker, -MASTER_RETRY_SECONDS)))
        if remaining > 0:
            return {**current, 'refresh_in_progress': False, 'retry_after_seconds': remaining,
                    'message': f'Please wait {remaining} seconds before another contract refresh. Use Reconnect market feed for a dropped connection.'}
        _attempts[broker] = time.monotonic()
        state = _refresh_state(broker)
        state.update(last_attempt_at=iso_now(), last_error=None, refresh_stage='downloading')
        db.rollback()  # Release reader before network work or acquiring writer.
        try:
            contracts = await asyncio.to_thread(download_master, broker)
            for publication_attempt in range(3):
                saved = _referenced_trades(db, broker)
                retained = {leg.get('instrument_id') for trade in saved for leg in trade.get('legs', [])}
                state['refresh_stage'] = 'validating_cache'
                candidate = await asyncio.to_thread(prepare_master, broker, contracts, retained)
                try:
                    async with _write_lock:
                        # Recheck trades created while staging, releasing the
                        # read transaction before the atomic file publication.
                        latest = _referenced_trades(db, broker)
                        required = {leg.get('instrument_id') for trade in latest for leg in trade.get('legs', [])}
                        available = {row['id'] for row in candidate._live}
                        previous = {row['id'] for row in engine.catalog._live}
                        if (required & previous) - available:
                            continue
                        _assert_active_identities(candidate, latest)
                        state['refresh_stage'] = 'publishing'
                        publish_master(candidate)
                        break
                finally:
                    candidate.path.unlink(missing_ok=True)
            else:
                raise MasterRefreshError('catalog_changed_during_refresh', 'cache_publication')
            state['last_error'] = None
        except Exception as exc:
            details = exc.details if isinstance(exc, MasterRefreshError) else {'code': 'unexpected_refresh_error', 'stage': 'refresh', 'exception_type': type(exc).__name__}
            state['last_error'] = {**details, 'at': iso_now()}
            log.error('Official Kotak master refresh failed; existing contracts retained. diagnostic=%s\n%s',
                      json.dumps(state['last_error'], sort_keys=True), safe_master_traceback(exc))
        # A failed refresh can be retried after a short cooldown from completion,
        # with one in-flight import; the validated catalog cache remains separate.
        _attempts[broker] = time.monotonic()
        state.update(last_completed_at=iso_now(), refresh_stage=None)
        try:
            await asyncio.to_thread(_persist_refresh_state, broker, dict(state))
        except OSError as exc:
            log.error('Could not persist safe master diagnostics.\n%s', safe_master_traceback(exc))
        result = {**master_status(db, broker), 'refresh_in_progress': False}
        if state['last_error']:
            result.update(status='cached' if result['underlying_count'] else 'unavailable',
                          message='Official Kotak master refresh failed; existing contracts and paper-trade history retained. See the safe failure stage.')
        return result


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
    price_reference = raw.get('price_reference')
    reference_price = number(price_reference.get('price'), positive=True) if price_reference and not price_reference.get('stale', True) else None
    atm_price = spot if spot is not None else reference_price
    atm = min(rows, key=lambda strike: abs(strike-atm_price)) if rows and atm_price is not None else None
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
            'updated_at': stamp, 'as_of': stamp, 'spot': spot, 'price_reference': price_reference,
            'atm_price': atm_price, 'atm_basis': 'spot' if spot is not None else 'futures_reference' if reference_price is not None else None,
            'stale_after_seconds': raw.get('stale_after_seconds'), 'summary': summary, 'metrics': summary, 'lot_size': summary['lot_size'],
            'rows': values, 'instruments': instruments, 'quotes': quotes, 'refresh_after_seconds': MIN_INTERVAL}



@router.get('/underlyings')
def underlyings(q: str = Query('', max_length=100), mode: Literal['sample','live'] | None = None,
                      broker: Literal['kotak'] = 'kotak', db: Session = Depends(get_db)):
    validate_broker(broker)
    selected_mode = mode_value(db, mode)
    groups = sample_universe(engine.catalog, broker) if selected_mode == 'sample' else [{k:v for k,v in g.items() if k not in {'contracts', 'reference_futures'}} for g in live_universe(broker).values()]
    query = q.strip().upper()
    rows = [r for r in groups if not query or query in r['symbol'].upper() or query in r['name'].upper()]
    rows.sort(key=lambda r: (r['kind'] != 'index', r['exchange'] != 'NSE', r['symbol']))
    return {'underlyings': rows, 'total': len(groups), 'filtered_total': len(rows), 'mode': selected_mode,
            'demo': selected_mode == 'sample', 'master': master_status(db, broker), 'configured': configured(broker),
            'message': 'Import or refresh this brokerÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢s official master to list every entitled NSE/BSE/MCX option. Missing live fields remain unavailable.'}


@router.get('/master/status')
def get_master_status(broker: Literal['kotak'] = 'kotak'):
    return master_status(broker=broker)


async def _run_master_job(broker):
    # This session belongs to the job, never to a disconnected browser request.
    try:
        with SessionLocal() as db:
            await refresh_master(db, force=True, broker=broker)
    except asyncio.CancelledError:
        state = _refresh_state(broker)
        state.update(last_completed_at=iso_now(), refresh_stage=None,
                     last_error={'code': 'refresh_interrupted', 'stage': 'server_shutdown', 'at': iso_now()})
        await asyncio.to_thread(_persist_refresh_state, broker, dict(state))
        raise
    except Exception as exc:
        state = _refresh_state(broker)
        state.update(last_completed_at=iso_now(), refresh_stage=None,
                     last_error={'code': 'refresh_job_failed', 'stage': 'refresh',
                                 'exception_type': type(exc).__name__, 'at': iso_now()})
        log.error('Kotak master job failed.\\n%s', safe_master_traceback(exc))


async def shutdown_master_jobs():
    jobs = [job for job in _master_jobs.values() if not job.done()]
    for job in jobs:
        job.cancel()
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
    _master_jobs.clear()


@router.post('/master/refresh')
async def reload_master(response: Response, broker: Literal['kotak'] = 'kotak'):
    # Enqueue once and return immediately, so proxy/browser timeouts cannot own
    # the lifetime of a large official CSV download and atomic publication.
    current = await asyncio.to_thread(master_status, broker=broker)
    if current['refresh_in_progress']:
        response.status_code = 202
        return {'ok': True, 'accepted': True, 'master': current}
    if current['new_trades_allowed'] and age(current['updated_at']) < MASTER_REFRESH_SECONDS:
        return {'ok': True, 'accepted': False, 'master': current}
    if current['retry_after_seconds']:
        response.status_code = 429
        response.headers['Retry-After'] = str(current['retry_after_seconds'])
        return {'detail': 'Please wait briefly before another contract refresh.', 'master': current}
    # Recheck after the thread await to fence two requests admitted together.
    running = _master_jobs.get(broker)
    if running is None or running.done():
        _master_jobs[broker] = asyncio.create_task(_run_master_job(broker), name='kotak-master-refresh')
    response.status_code = 202
    return {'ok': True, 'accepted': True, 'master': {
        **current, 'refresh_in_progress': True, 'refresh_stage': 'queued',
    }}


def cached_live_data(db, contracts, expiry, stale_after, reference_contract=None):
    oc, stamps = {}, []
    active = [c for c in contracts if c['expiry'] == expiry]
    broker = active[0]['broker'] if active else None
    # One bounded database lookup, not two queries for every missing strike.
    state = engine._cache(db)
    wanted = [*active, *([reference_contract] if reference_contract else [])]
    wanted_keys = {(broker, c['id']) for c in wanted}
    with engine._cache_lock:
        marks = {key: dict(state['quotes'][key]) for key in wanted_keys if key in state['quotes']}
    missing = [c['id'] for c in wanted if (broker, c['id']) not in marks]
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
    reference = None
    if reference_contract:
        quote = marks.get((broker, reference_contract['id']))
        if quote and quote.get('demo') is False and quote.get('broker') == broker:
            price = number(quote.get('ltp'), positive=True)
            if price is not None and price > 0:
                reference = {'kind': 'futures_reference', 'price': price, 'broker': broker,
                    'instrument_id': reference_contract['id'], 'symbol': reference_contract['symbol'],
                    'expiry': reference_contract['expiry'], 'quote_at': quote.get('received_at'),
                    'exchange_timestamp': quote.get('exchange_timestamp'), 'stale': age(quote.get('received_at')) > stale_after,
                    'note': 'Nearest master-listed futures expiry on or after this option expiry; reference only, not cash spot.'}
    # This timestamp describes the latest option activity. Each illiquid row
    # keeps its own original timestamp and stale flag, rather than making all
    # other fresh strikes appear disconnected. Reference-only ticks do not
    # make an otherwise stale option chain fresh.
    return {'data': {'oc': oc}, 'price_reference': reference, 'stale_after_seconds': stale_after}, max((s for s in stamps if s), default=None)


def finish_read_transaction(db):
    """End a completed read without clearing the broker quote cache.

    The callers are read-only, or have already committed a paper mutation.
    Refuse pending ORM changes instead of accidentally publishing them.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError('Save pending paper state before refreshing market data.')
    if db.in_transaction():db.commit()


def chain_settings(db, mode):
    settings=engine.get_settings(db)
    finish_read_transaction(db)
    return settings, mode or ('sample' if settings['demo_mode'] else 'live')


def live_chain_result(db, metadata, selected, expiry, stale_after, reference_contract=None):
    raw,stamp=cached_live_data(db,selected,expiry,stale_after,reference_contract)
    finish_read_transaction(db)
    return build_chain(metadata,selected,expiry,raw=raw,stamp=stamp)


@router.get('')
async def option_chain(underlying: str = Query(..., min_length=1, max_length=180), expiry: str | None = None,
                       mode: Literal['sample','live'] | None = None, broker: Literal['kotak'] = 'kotak',
                       db: Session = Depends(get_db)):
    validate_broker(broker)
    settings,selected_mode = await asyncio.to_thread(chain_settings,db,mode)
    demo = selected_mode == 'sample'
    workspace_matches = bool(settings['demo_mode']) == demo
    if demo:
        choices = sample_universe(engine.catalog, broker)
        metadata = next((r for r in choices if r['key'] == underlying or r['symbol'] == underlying), None)
        contracts = [c for c in engine.catalog.all(demo=True) if metadata and c['broker'] == broker and c['exchange'] == metadata['exchange'] and c['underlying'] == metadata['symbol'] and c['option_type'] in {'CE','PE'}]
    else:
        group = live_universe(broker).get(underlying)
        metadata = {k:v for k,v in group.items() if k not in {'contracts', 'reference_futures'}} if group else None
        contracts = group['contracts'] if group else []
    if not metadata:
        raise HTTPException(404, 'Choose an underlying from this brokerÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢s instrument catalog. Refresh its master if missing.')
    metadata = {**metadata, 'broker': broker}
    expiry = expiry or metadata['expiries'][0]
    if expiry not in metadata['expiries']:
        raise HTTPException(422, 'Choose an active expiry from this underlyingÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢s master.')
    selected = [c for c in contracts if c['expiry'] == expiry]
    if demo:
        result = build_chain(metadata, selected, expiry, demo=True, stamp=iso_now())
        result.update(status='sample', stale=False, trading_enabled=workspace_matches,
                      message='Fictional prices, Greeks and quantities for practice; no live market data is implied.')
    else:
        # Only the most recently viewed chain per broker receives a temporary
        # subscription. Active positions are retained independently by main.
        # Subscribe one exact master-listed future as a labelled reference,
        # without inventing an index/spot token or borrowing another broker.
        reference_contract = next((c for c in group.get('reference_futures', []) if c['expiry'] >= expiry), None)
        wanted = {c['id'] for c in selected}
        if reference_contract:
            wanted.add(reference_contract['id'])
        _requested[broker] = (time.monotonic() + CHAIN_SUBSCRIPTION_SECONDS, wanted)
        if _refresh_feeds:
            await _refresh_feeds(db)
        result = await asyncio.to_thread(live_chain_result,db,metadata,selected,expiry,settings['stale_after_seconds'],reference_contract)
        fresh = [q for row in result['rows'] for q in (row['ce'], row['pe']) if q and q['ltp'] is not None and not q['stale']]
        result.update(status='connected' if fresh else 'stale' if result['quotes'] else 'awaiting_quotes' if configured(broker) else 'credentials_required',
                      stale=not bool(fresh), trading_enabled=workspace_matches and bool(fresh) and master_status(db, broker)['new_trades_allowed'],
                      message='Prices are from Kotak Neo only. A labelled nearest futures reference is used to locate nearby strikes when available; it is not cash spot. Unavailable fields stay blank and stale rows keep their original timestamps.',
                      capabilities={'ltp': True, 'bid_ask': True, 'open_interest': True, 'spot': False, 'greeks': False,
                                    'futures_reference': reference_contract is not None,
                                    'greeks_note': 'Kotak SFeed messages do not carry Greeks; unavailable values remain blank.'})
    result.update(read_only=True, mode_matches_workspace=workspace_matches, master=master_status(db, broker))
    return result
