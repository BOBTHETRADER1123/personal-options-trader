"""Bounded ephemeral websocket tickets and one shared dashboard serialization.

One application worker owns these objects. They contain no broker credentials,
never write SQLite, and produce no payload/token logs. Database authorization
remains the caller's responsibility before registering a viewer and in each
batched snapshot callback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Callable, Mapping


class TicketRegistry:
    """One-use short-lived tickets, retaining only SHA-256 token digests."""

    def __init__(self, ttl: float = 30, max_items: int = 1024, *, clock=time.time):
        if not math.isfinite(ttl) or not 0 < ttl <= 30 or max_items < 1:
            raise ValueError('Invalid websocket ticket bounds.')
        self.ttl, self.max_items, self.clock = ttl, max_items, clock
        self._tickets: OrderedDict[str, tuple[float, float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now):
        for key in list(self._tickets):
            if self._tickets[key][0] <= now:
                self._tickets.pop(key, None)

    def issue(self, session_hash: str, session_expiry: float) -> dict:
        now = self.clock()
        if (not isinstance(session_hash, str) or not session_hash or len(session_hash) > 128
                or isinstance(session_expiry, bool) or not math.isfinite(session_expiry)
                or session_expiry <= now):
            raise ValueError('A valid owner session is required.')
        raw = secrets.token_urlsafe(32)
        expiry = min(now + self.ttl, session_expiry)
        key = hashlib.sha256(raw.encode()).hexdigest()
        with self._lock:
            self._prune(now)
            while len(self._tickets) >= self.max_items:
                self._tickets.popitem(last=False)
            self._tickets[key] = (expiry, session_expiry, session_hash)
        return {'ticket': raw, 'expires_in': max(1, math.ceil(expiry - now))}

    def consume(self, raw: str | None) -> tuple[float, str] | None:
        if not isinstance(raw, str) or not 16 <= len(raw) <= 256:
            return None
        key = hashlib.sha256(raw.encode()).hexdigest()
        now = self.clock()
        with self._lock:
            self._prune(now)
            item = self._tickets.pop(key, None)
        if item is None or item[0] <= now or item[1] <= now:
            return None
        return item[1], item[2]


_VOLATILE = frozenset({'server_time', 'feed_age', 'exchange_quote_age'})


def dashboard_fingerprint(data) -> str:
    """Ignore only elapsed display ages; quote identity and stale flips remain."""
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in _VOLATILE}
        if isinstance(value, (list, tuple)):
            return [stable(item) for item in value]
        return value
    encoded = json.dumps(stable(data), sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class DashboardSnapshot:
    encoded: str
    fingerprint: str
    sessions: Mapping[str, float]
    built_at: float
    checked_hashes: frozenset[str]


class SharedDashboardPublisher:
    """Build/encode once per interval, only when an actual viewer asks for it.

    register() requires an already independently verified session expiry. New
    hashes can reuse a recent dashboard until the next scheduled batch, without
    forcing a second build in the same second. A hash explicitly rejected by
    the latest batch is never restored from that registration alone.
    """

    def __init__(self, interval: float = 1.0, max_clients: int = 256,
                 *, clock=time.monotonic, wall_clock=time.time):
        if not math.isfinite(interval) or interval < 1 or max_clients < 1:
            raise ValueError('Invalid dashboard publisher bounds.')
        self.interval, self.max_clients = interval, max_clients
        self.clock, self.wall_clock = clock, wall_clock
        self._lock = asyncio.Lock()
        self._registrations: dict[int, tuple[str, float]] = {}
        self._generation = 0
        self._next_build = 0.0
        self._snapshot: DashboardSnapshot | None = None
        self._failure: Exception | None = None

    def register(self, session_hash: str, checked_session_expiry: float) -> int:
        if (not isinstance(session_hash, str) or not session_hash or len(session_hash) > 128
                or isinstance(checked_session_expiry, bool)
                or not math.isfinite(checked_session_expiry)
                or checked_session_expiry <= self.wall_clock()):
            raise ValueError('A valid owner session is required.')
        if len(self._registrations) >= self.max_clients:
            raise ValueError('Too many workspace streams.')
        self._generation += 1
        token = self._generation
        self._registrations[token] = (session_hash, checked_session_expiry)
        # The caller has just checked this new session against the durable
        # owner session store. Do not overwrite an explicit batch rejection.
        if self._snapshot and session_hash not in self._snapshot.checked_hashes:
            sessions = dict(self._snapshot.sessions)
            sessions[session_hash] = checked_session_expiry
            self._snapshot = replace(self._snapshot, sessions=sessions)
        return token

    def unregister(self, registration: int) -> None:
        self._registrations.pop(registration, None)

    @property
    def active_clients(self) -> int:
        return len(self._registrations)

    async def snapshot(self, build: Callable[[set[str]], dict]) -> DashboardSnapshot | None:
        if not self._registrations:
            return None
        async with self._lock:
            if not self._registrations:
                return None
            now = self.clock()
            if now < self._next_build:
                if self._failure is not None:raise self._failure
                if self._snapshot is not None:return self._snapshot
            generation = self._generation
            hashes = {row[0] for row in self._registrations.values()}
            self._next_build = now + self.interval
            try:
                result = await asyncio.to_thread(build, hashes)
            except Exception as error:
                self._failure = error
                raise
            self._failure = None
            data = result['data']
            supplied = result['sessions']
            if not isinstance(supplied, Mapping):
                raise ValueError('Invalid dashboard session batch.')
            sessions = {}
            for key in hashes:
                expiry = supplied.get(key)
                if (isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                        and math.isfinite(expiry) and expiry > self.wall_clock()):
                    sessions[key] = expiry
            # A viewer may finish its independent session check while the
            # shared read is running. It must not force a second full build.
            for registration, (key, expiry) in self._registrations.items():
                if registration > generation and key not in hashes and expiry > self.wall_clock():
                    sessions[key] = expiry
            encoded = json.dumps({'type': 'dashboard', 'data': data},
                                 separators=(',', ':'), ensure_ascii=False, allow_nan=False)
            snapshot = DashboardSnapshot(encoded=encoded, fingerprint=dashboard_fingerprint(data),
                                         sessions=sessions, built_at=now, checked_hashes=frozenset(hashes))
            self._snapshot = snapshot
            self._next_build = now + self.interval
            return snapshot
