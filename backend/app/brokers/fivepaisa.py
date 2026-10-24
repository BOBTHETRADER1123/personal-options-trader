"""Official 5paisa Xstream MarketFeedV3 WebSocket (read only)."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import time
from urllib.parse import quote

from .base import FeedAuthenticationError, FeedProtocolError, ReadOnlyFeed, epoch_timestamp, normalized_quote, optional_price, utc_now

EXCHANGES = {"NSE": "N", "BSE": "B", "MCX": "M"}


def feed_url(token: str, code: str) -> str:
    """Official SDK's allowlisted feed routing; JWT payload is a hint only.

    We do not authenticate a JWT locally or follow a token-supplied URL. The
    actual feed authenticates the token. Unknown/malformed routing uses the
    documented default endpoint, never an arbitrary host.
    """
    host = "openfeed.5paisa.com"
    try:
        part = token.split(".")[1]
        if len(part) > 16384:
            raise ValueError("Token routing payload exceeds bound")
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        host = {"A": "aopenfeed.5paisa.com", "B": "bopenfeed.5paisa.com", "C": "openfeed.5paisa.com"}.get(payload.get("RedirectServer"), host)
    except (ValueError, TypeError, IndexError, AttributeError, UnicodeError):
        pass
    return f"wss://{host}/feeds/api/chat?Value1=" + quote(f"{token}|{code}", safe="|")


def tick_timestamp(value) -> str | None:
    if isinstance(value, str):
        match = re.fullmatch(r"/?Date\((-?\d+)(?:[+-]\d+)?\)/?", value)
        return epoch_timestamp(match[1], milliseconds=True) if match else None
    return epoch_timestamp(value, milliseconds=True) if value is not None else None


def parse_message(data: str | bytes | list | dict, contracts: dict, *, received_at=None, received_ns=None, open_interest=None) -> list[dict]:
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise FeedProtocolError("5paisa returned invalid JSON") from exc
    rows = data if isinstance(data, list) else [data]
    results = []
    for row in rows:
        if not isinstance(row, dict):
            raise FeedProtocolError("5paisa unexpected market-data record")
        if "OpenInterest" in row and open_interest is not None:
            identity = (str(row.get("Exch", "")), str(row.get("ExchType", "")), str(row.get("Token", "")))
            if identity in contracts:
                open_interest[identity] = {"value": row["OpenInterest"], "received_at": received_at or utc_now()}
        if "LastRate" not in row:
            # Authentication messages have no stable documented shape. Match
            # their meaning without exposing the message, tokens or URL.
            text = json.dumps(row).lower()
            if any(term in text for term in ("expired", "unauthorized", "invalid token", "invalid access")):
                raise FeedAuthenticationError("5paisa session rejected; refresh the backend access token")
            continue
        key = (str(row.get("Exch", "")), str(row.get("ExchType", "")), str(row.get("Token", "")))
        contract = contracts.get(key)
        if not contract:
            raise FeedProtocolError("5paisa returned an unmapped contract token")
        oi = (open_interest or {}).get(key, {})
        results.append(normalized_quote("fivepaisa", contract, row["LastRate"], received_at=received_at,
                                        received_ns=received_ns, bid=optional_price(row.get("BidRate")),
                                        ask=optional_price(row.get("OffRate")), volume=row.get("TotalQty"),
                                        previous_close=row.get("PClose"), open_interest=oi.get("value"),
                                        open_interest_received_at=oi.get("received_at"),
                                        exchange_timestamp=tick_timestamp(row.get("TickDt")),
                                        exchange_time_raw=row.get("Time"), sequence=None,
                                        event_kind="MarketFeedV3", raw_message=row))
    return results


class FivepaisaFeed(ReadOnlyFeed):
    broker = "fivepaisa"

    def configured(self):
        return bool(os.environ.get("FIVEPAISA_CLIENT_CODE") and os.environ.get("FIVEPAISA_ACCESS_TOKEN"))

    async def stream(self, contracts):
        from websockets.asyncio.client import connect
        code, token = os.environ["FIVEPAISA_CLIENT_CODE"], os.environ["FIVEPAISA_ACCESS_TOKEN"]
        url = feed_url(token, code)
        mapping = {(EXCHANGES[r["exchange"]], r["segment"], str(r["token"])): r for r in contracts}
        if len(mapping) > int(os.getenv("FIVEPAISA_MAX_SUBSCRIPTIONS", "3000")):
            raise FeedProtocolError("5paisa watchlist exceeds the configured subscription budget; narrow it to your permitted account limit")
        feed_data = [{"Exch": EXCHANGES[r["exchange"]], "ExchType": r["segment"], "ScripCode": int(r["token"])} for r in contracts]
        oi = {}
        # Never emit the URL or raw exceptions: both may reveal access tokens.
        try:
            async with connect(url, open_timeout=20, close_timeout=5, ping_interval=20, ping_timeout=20,
                               max_queue=32, max_size=8_388_608) as socket:
                methods = ("MarketFeedV3", "GetScripInfoForFuture")
                for method in methods:
                    await socket.send(json.dumps({"Method": method, "Operation": "Subscribe", "ClientCode": code, "MarketFeedData": feed_data}))
                await self.status("connected", f"Subscribed to {len(contracts)} contracts; awaiting actual quotes", subscriptions=len(contracts))
                try:
                    async for data in socket:
                        received_at, received_ns = utc_now(), time.time_ns()
                        for item in parse_message(data, mapping, received_at=received_at, received_ns=received_ns, open_interest=oi):
                            await self.on_quote(item)
                finally:
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(2):
                            for method in methods:
                                await socket.send(json.dumps({"Method": method, "Operation": "Unsubscribe", "ClientCode": code, "MarketFeedData": feed_data}))
        except Exception as exc:
            # A rejected HTTP handshake is authentication failure, not an
            # endless reconnect. The exception URL is deliberately discarded.
            if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403}:
                raise FeedAuthenticationError("5paisa access token rejected; use the broker's daily OAuth login again") from None
            raise
