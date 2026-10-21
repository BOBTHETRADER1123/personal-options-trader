"""Read-only feed utilities. Adapters intentionally expose no order interface."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

QuoteCallback = Callable[[dict], Awaitable[None]]
StatusCallback = Callable[[str, dict], Awaitable[None]]


class FeedAuthenticationError(RuntimeError):
    pass


class FeedProtocolError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def epoch_timestamp(value, milliseconds=False) -> str | None:
    try:
        number = float(value)
        if milliseconds:
            number /= 1000
        if not math.isfinite(number) or number <= 0:
            return None
        return datetime.fromtimestamp(number, timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def optional_price(value) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise FeedProtocolError("Non-finite or negative feed price")
    return number if number > 0 else None


def normalized_quote(broker: str, contract: dict, ltp, *, received_at=None, received_ns=None, **fields) -> dict:
    price = optional_price(ltp)
    if price is None:
        raise FeedProtocolError("Quote has no valid last price")
    return {"broker": broker, "instrument_id": contract["id"], "underlying": contract["underlying"],
            "exchange": contract["exchange"], "segment": contract["segment"], "symbol": contract["symbol"],
            "expiry": contract["expiry"], "strike": contract["strike"], "option_type": contract["option_type"],
            "lot_size": contract["lot_size"], "token": contract["token"], "ltp": price,
            "received_at": received_at or utc_now(), "received_ns": received_ns or time.time_ns(),
            "demo": False, "source": broker, **fields}


class ReadOnlyFeed:
    broker: str

    def __init__(self, on_quote: QuoteCallback, on_status: StatusCallback):
        self.on_quote, self.on_status = on_quote, on_status

    async def status(self, status: str, detail: str, **extra):
        await self.on_status(self.broker, {"status": status, "detail": detail, **extra})

    async def stream(self, contracts: list[dict]):
        raise NotImplementedError

    def configured(self) -> bool:
        raise NotImplementedError
