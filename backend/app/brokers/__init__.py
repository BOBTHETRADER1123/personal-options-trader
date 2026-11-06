"""Independent read-only adapters with bounded retries and explicit feed state."""
from __future__ import annotations

import asyncio
import random
import time

from .base import FeedAuthenticationError, FeedProtocolError
from .kotak import KotakFeed


class FeedManager:
    def __init__(self, on_quote, on_status=None):
        self.on_quote = on_quote
        self.on_status = on_status
        self._tasks: list[asyncio.Task] = []
        self._contracts: dict[str, list] = {}
        self._states: dict[str, dict] = {}
        self._first_quote_at: dict[str, float] = {}
        self._stopping = False
        self._adapters = [KotakFeed(self._quote, self._status)]
        for adapter in self._adapters:
            self._states[adapter.broker] = {"broker": adapter.broker, "status": "not_configured", "configured": bool(adapter.configured()),
                                           "detail": "Add backend market-data credentials", "last_quote_at": None, "source": "live", "received_events": 0, "reconnect_count": 0}

    async def margin_required(self, contract, **inputs):
        """Owner-triggered single-order diagnostic on the existing Kotak session."""
        return await self._adapters[0].margin_required(contract, **inputs)

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
        broker = quote["broker"]
        state = self._states[broker]
        if broker not in self._first_quote_at:
            self._first_quote_at[broker] = time.monotonic()
        state.update(last_quote_at=quote["received_at"], received_events=state["received_events"] + 1)
        if state["status"] != "connected":
            await self._status(broker, {"status": "connected", "detail": "Receiving Kotak market quotes",
                                        "awaiting_since": None, "retry_in_seconds": None})

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
            self._contracts[adapter.broker] = contracts
            self._tasks.append(asyncio.create_task(self._run(adapter, contracts), name=f"market-feed-{adapter.broker}"))

    async def reconfigure(self, instruments):
        """Update token demand in place; retain the single active collector."""
        running = [task for task in self._tasks if not task.done()]
        if not running:
            self._tasks.clear()
            await self.start(instruments)
            return
        desired = [r for r in instruments if r.get("broker") == "kotak" and not r.get("demo")]
        if not desired:
            await self.stop()
            return
        # _run retains this list across its bounded retry loop. A later socket
        # therefore restores current active legs, not an obsolete option chain.
        contracts = self._contracts.get("kotak")
        if contracts is not None:
            contracts[:] = desired
        adapter = self._adapters[0]
        try:
            await adapter.reconfigure(desired)
        except asyncio.CancelledError:
            raise
        except FeedProtocolError as exc:
            await self.stop()
            await self._status(adapter.broker, {"status": "error", "detail": str(exc),
                                                "subscriptions": 0, "awaiting_since": None})
        except Exception as exc:
            # A failed subscription change needs a fresh transport. Cancel and
            # join the old task first: two sockets must never run concurrently.
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            self._states[adapter.broker]["reconnect_count"] += 1
            await self._status(adapter.broker, {"status": "reconnecting", "detail": f"Subscription update interrupted ({type(exc).__name__}); retrying"})
            self._contracts[adapter.broker] = desired
            self._tasks.append(asyncio.create_task(self._run(adapter, desired), name=f"market-feed-{adapter.broker}"))

    async def _run(self, adapter, contracts):
        attempt = 0
        state = self._states[adapter.broker]
        while not self._stopping:
            received_before = state["received_events"]
            self._first_quote_at.pop(adapter.broker, None)
            try:
                await self._status(adapter.broker, {"status": "connecting", "detail": "Connecting read-only market feed",
                                                   "awaiting_since": None, "retry_in_seconds": None})
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
                first_quote_at = self._first_quote_at.get(adapter.broker)
                if (first_quote_at is not None and state["received_events"] - received_before >= 2
                        and time.monotonic() - first_quote_at >= 30):
                    # A short burst followed by another drop belongs to the
                    # same outage. Reset after a sustained quote connection.
                    attempt = 0
                attempt += 1
                self._states[adapter.broker]["reconnect_count"] += 1
                delay = min(60, 2 ** min(attempt, 6)) + random.uniform(0, 1)
                await self._status(adapter.broker, {"status": "reconnecting", "detail": f"Connection interrupted ({type(exc).__name__}); retrying", "retry_in_seconds": round(delay, 1), "awaiting_since": None})
                await asyncio.sleep(delay)

    async def mark_market_closed(self):
        """Label verified-session idling without concealing a genuine fault."""
        for adapter in self._adapters:
            state = self._states[adapter.broker]
            if state["configured"] and state["status"] not in {"authentication_required", "error"}:
                await self._status(adapter.broker, {"status": "market_closed", "retry_in_seconds": None,
                    "detail": "No selected contract is inside a verified exchange session; the feed is idle by design."})

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
