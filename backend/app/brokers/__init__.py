"""Independent read-only adapters with bounded retries and explicit feed state."""
from __future__ import annotations

import asyncio
import random

from .base import FeedAuthenticationError, FeedProtocolError, utc_now
from .kotak import KotakFeed


class FeedManager:
    def __init__(self, on_quote, on_status=None):
        self.on_quote = on_quote
        self.on_status = on_status
        self._tasks: list[asyncio.Task] = []
        self._states: dict[str, dict] = {}
        self._stopping = False
        self._adapters = [KotakFeed(self._quote, self._status)]
        for adapter in self._adapters:
            self._states[adapter.broker] = {"broker": adapter.broker, "status": "not_configured", "configured": bool(adapter.configured()),
                                           "detail": "Add backend market-data credentials", "last_quote_at": None, "source": "live", "received_events": 0, "reconnect_count": 0}

    def statuses(self) -> dict[str, dict]:
        return {key: dict(value) for key, value in self._states.items()}

    async def _status(self, broker, state):
        self._states[broker].update(state)
        if self.on_status:
            await self.on_status(broker, dict(self._states[broker]))

    async def _quote(self, quote):
        # The selected recording policy owns persistence/buffering; never send
        # this broker's receipt through another broker's price cache.
        await self.on_quote(quote)
        state = self._states[quote["broker"]]
        state.update({"last_quote_at": quote["received_at"], "status": "connected", "received_events": state["received_events"] + 1})

    async def start(self, instruments):
        if self._tasks:
            return
        self._stopping = False
        # The caller supplies the bounded union of watched contracts and active
        # trade legs. Filtering by the watchlist again would drop an unattended
        # position as soon as its option-chain page is closed.
        for adapter in self._adapters:
            available = bool(adapter.configured())
            self._states[adapter.broker]['configured'] = available
            if not available:
                await self._status(adapter.broker, {"status": "not_configured", "detail": "Backend credentials required"})
                continue
            contracts = [r for r in instruments if r.get("broker") == adapter.broker and not r.get("demo")]
            if not contracts:
                await self._status(adapter.broker, {"status": "awaiting_instruments", "detail": "Import a current master and configure the feed watchlist"})
                continue
            self._tasks.append(asyncio.create_task(self._run(adapter, contracts), name=f"market-feed-{adapter.broker}"))

    async def _run(self, adapter, contracts):
        attempt = 0
        while not self._stopping:
            try:
                await self._status(adapter.broker, {"status": "connecting", "detail": "Connecting read-only market feed"})
                await adapter.stream(contracts)
                if not self._stopping:
                    raise ConnectionError("Feed ended")
            except asyncio.CancelledError:
                raise
            except FeedAuthenticationError as exc:
                await self._status(adapter.broker, {"status": "authentication_required", "detail": str(exc)})
                return
            except FeedProtocolError as exc:
                await self._status(adapter.broker, {"status": "error", "detail": str(exc)})
                # Protocol/configuration errors need operator correction.
                return
            except Exception as exc:
                attempt += 1
                self._states[adapter.broker]["reconnect_count"] += 1
                delay = min(60, 2 ** min(attempt, 6)) + random.uniform(0, 1)
                await self._status(adapter.broker, {"status": "reconnecting", "detail": f"Connection interrupted ({type(exc).__name__}); retrying", "retry_in_seconds": round(delay, 1)})
                await asyncio.sleep(delay)

    async def stop(self):
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for broker, state in self._states.items():
            if state["configured"] and state["status"] not in {"authentication_required", "error"}:
                await self._status(broker, {"status": "disconnected", "detail": "Market-data connection stopped"})


__all__ = ["FeedManager", "KotakFeed"]
