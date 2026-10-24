"""Server-time collector fencing and maintained exchange-session calendars.

No session advisory lock is used: Neon's transaction-pooled connections may
change between statements. A write transaction locks the lease row and checks
its generation before performing capture, so an expired owner cannot commit a
second collector's snapshots. The unique snapshot key is a second safeguard.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select

from .database import run_transaction
from .models import SchedulerLease

IST = ZoneInfo('Asia/Kolkata')
UTC = timezone.utc


def database_seconds(db) -> float:
    """Actual database-server wall time, rather than a client clock or tx start."""
    if db.get_bind().dialect.name == 'postgresql':
        return float(db.scalar(select(func.extract('epoch', func.clock_timestamp()))))
    return float(db.scalar(select((func.julianday('now') - 2440587.5) * 86400.0)))


@dataclass(frozen=True)
class LeaseToken:
    name: str
    owner: str
    generation: int


def acquire_or_renew(db, owner: str, name='market_collector', ttl_seconds=15) -> LeaseToken | None:
    """Atomic CAS, using DB time. Caller commits; TTL must exceed one capture."""
    ttl_seconds = max(5, min(120, int(ttl_seconds)))
    now = database_seconds(db)
    if db.get_bind().dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(SchedulerLease).values(name=name, owner=owner, generation=1,
                                               expires_at=now + ttl_seconds)
    statement = statement.on_conflict_do_update(
        index_elements=['name'],
        set_={'owner': owner, 'expires_at': now + ttl_seconds,
              'generation': case(((SchedulerLease.owner == owner) & (SchedulerLease.expires_at > now), SchedulerLease.generation),
                                 else_=SchedulerLease.generation + 1)},
        where=(SchedulerLease.owner == owner) | (SchedulerLease.expires_at <= now),
    ).returning(SchedulerLease.generation)
    generation = db.scalar(statement)
    return LeaseToken(name, owner, generation) if generation is not None else None


def guard_lease(db, token: LeaseToken) -> bool:
    """Hold the row lock through the capture commit (same transaction required)."""
    row = db.scalar(select(SchedulerLease).where(SchedulerLease.name == token.name).with_for_update())
    return bool(row and row.owner == token.owner and row.generation == token.generation
                and row.expires_at > database_seconds(db))


class CollectorLease:
    """One application-process identity; renew before starting/resuming feeds.

    root integration: lease.renew() every <=5 seconds; stop feeds when false.
    Inside capture's transaction call guard_lease(db, lease.token) or pass token
    to engine.take_snapshots(db, lease=lease.token). No token means no ownership.
    """
    def __init__(self, session_factory, name='market_collector', ttl_seconds=15):
        self.session_factory, self.name, self.ttl_seconds = session_factory, name, ttl_seconds
        self.owner, self.token = uuid.uuid4().hex, None

    def renew(self) -> bool:
        previous=self.token
        self.token = run_transaction(lambda db: acquire_or_renew(db, self.owner, self.name,
                                                                self.ttl_seconds), self.session_factory)
        if self.token is not None and self.token!=previous:
            # A warm standby must restore persisted positions/settings instead
            # of carrying a pre-takeover in-process ledger cache into capture.
            from .engine import clear_cache
            clear_cache()
        return self.token is not None

    def release(self) -> None:
        token = self.token
        self.token = None
        if token is None:
            return
        def release(db):
            row = db.scalar(select(SchedulerLease).where(SchedulerLease.name == token.name).with_for_update())
            if row and row.owner == token.owner and row.generation == token.generation:
                row.expires_at = database_seconds(db)
        run_transaction(release, self.session_factory)


class ExchangeCalendar:
    """Date-bounded, source-labelled schedules; unknown sessions fail closed.

    Schema: {timezone:'Asia/Kolkata', rules:[{exchange, segments?:[],
    underlyings?:[], valid_from, valid_through, sources:[official URLs],
    verified_at, weekdays:[0..4], sessions:[['09:15','15:40']],
    holidays:['YYYY-MM-DD'], overrides:{'YYYY-MM-DD':[['18:00','19:00']]}}]}.
    Overrides take precedence over weekends/holidays. MCX must use an explicit
    commodity-specific rule; no universal commodity close is guessed.
    """
    def __init__(self, data=None, path=None):
        self.error = None
        if data is None:
            selected = Path(path or os.getenv('MARKET_CALENDAR_FILE', '') or
                            Path(__file__).resolve().parents[1] / 'calendars' / 'exchange_sessions.json')
            try:
                data = json.loads(selected.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                self.error = 'Official exchange calendar is unavailable; recording paused.'
                data = {}
        self.rules = data.get('rules', []) if isinstance(data, dict) else []

    def _rule(self, leg, day: date):
        matches = []
        for rule in self.rules:
            if str(leg.get('exchange', '')).upper() != str(rule.get('exchange', '')).upper():
                continue
            if rule.get('segments') and str(leg.get('segment', '')).upper() not in [str(s).upper() for s in rule['segments']]:
                continue
            if rule.get('underlyings') and str(leg.get('underlying', '')).upper() not in [str(s).upper() for s in rule['underlyings']]:
                continue
            if str(leg.get('exchange', '')).upper() == 'MCX' and not rule.get('underlyings'):
                continue
            if not rule.get('sources') or not rule.get('verified_at'):
                continue
            if not rule.get('valid_from', '9999') <= day.isoformat() <= rule.get('valid_through', '0000'):
                continue
            matches.append(rule)
        return max(matches, key=lambda r: bool(r.get('underlyings')) * 2 + bool(r.get('segments'))) if matches else None

    def intervals(self, leg, day: date):
        rule = self._rule(leg, day)
        if not rule:
            return None
        if day.isoformat() in rule.get('overrides', {}):
            intervals = rule['overrides'][day.isoformat()]
        elif day.weekday() not in rule.get('weekdays', [0, 1, 2, 3, 4]) or day.isoformat() in rule.get('holidays', []):
            intervals = []
        else:
            intervals = rule.get('sessions', [])
        result = []
        for opening, closing in intervals:
            if (rule.get('expiry_day_index_close') and str(leg.get('expiry',''))[:10]==day.isoformat()
                and str(leg.get('underlying','')).upper() in rule.get('index_underlyings',[])):
                closing=min(closing,rule['expiry_day_index_close'])
            if rule.get('us_daylight_close') and datetime.combine(day,time(12),ZoneInfo('America/New_York')).dst():closing=rule['us_daylight_close']
            start = datetime.combine(day, time.fromisoformat(opening), IST)
            end = datetime.combine(day, time.fromisoformat(closing), IST)
            if end <= start:
                end += timedelta(days=1)
            result.append((int(start.timestamp()), int(end.timestamp())))
        return result

    def state(self, leg, moment: datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        day = moment.astimezone(IST).date()
        if leg.get('expiry') and str(leg['expiry'])[:10]<day.isoformat():return {'open':False,'known':True,'reason':'contract_expired_settlement_required'}
        intervals = self.intervals(leg, day)
        rule = self._rule(leg, day)
        if intervals is None:
            return {'open': False, 'known': False, 'reason': self.error or 'No verified calendar for this contract and date.'}
        second = int(moment.timestamp())
        return {'open': any(start <= second < end for start, end in intervals), 'known': True,
                'reason': 'session_open' if any(start <= second < end for start, end in intervals) else 'exchange_closed',
                'sources': rule['sources'], 'verified_at': rule['verified_at']}

    def missing_intervals(self, legs, start_second: int, end_second: int):
        """Return union of actual session seconds, excluding scheduled closures."""
        if end_second < start_second:
            return []
        start_day = datetime.fromtimestamp(start_second, UTC).astimezone(IST).date()
        end_day = datetime.fromtimestamp(end_second, UTC).astimezone(IST).date()
        intervals = []
        day = start_day
        # A bounded recovery scan; a longer outage becomes an explicitly
        # unclassified gap, not ten years of fabricated one-second observations.
        if (end_day - start_day).days > 3660:
            return [(start_second, end_second, 'calendar_range_unclassified')]
        while day <= end_day:
            for leg in legs:
                ranges = self.intervals(leg, day)
                if ranges is None:
                    continue
                for opening, closing in ranges:
                    lo, hi = max(start_second, opening), min(end_second, closing - 1)
                    if lo <= hi:
                        intervals.append((lo, hi, 'recording_interrupted'))
            day += timedelta(days=1)
        merged = []
        for lo, hi, reason in sorted(intervals):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]), reason)
            else:
                merged.append((lo, hi, reason))
        return merged
