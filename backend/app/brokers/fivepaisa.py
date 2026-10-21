"""Official 5paisa Xstream MarketFeedV3 WebSocket (read only)."""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import quote

from .base import FeedAuthenticationError, FeedProtocolError, ReadOnlyFeed, epoch_timestamp, normalized_quote, optional_price, utc_now

EXCHANGES = {"NSE": "N", "BSE": "B", "MCX": "M"}


def tick_timestamp(value) -> str | None:
    if isinstance(value, str):
        match = re.fullmatch(r"/?Date\((-?\d+)(?:[+-]\d+)?\)/?", value)
        return epoch_timestamp(match[1], milliseconds=True) if match else None
    return epoch_timestamp(value, milliseconds=True) if value is not None else None


def parse_message(data: str | bytes | list | dict, contracts: dict, *, received_at=None, received_ns=None) -> list[dict]:
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
        results.append(normalized_quote("fivepaisa", contract, row["LastRate"], received_at=received_at,
                                        received_ns=received_ns, bid=optional_price(row.get("BidRate")),
                                        ask=optional_price(row.get("OffRate")), volume=row.get("TotalQty"),
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
        url = "wss://openfeed.5paisa.com/feeds/api/chat?Value1=" + quote(f"{token}|{code}", safe="|")
        mapping = {(EXCHANGES[r["exchange"]], r["segment"], str(r["token"])): r for r in contracts}
        # Never emit the URL or raw exceptions: both may reveal access tokens.
        async with connect(url, open_timeout=20, close_timeout=5, ping_interval=20, ping_timeout=20,
                           max_queue=32, max_size=8_388_608) as socket:
            await socket.send(json.dumps({"Method": "MarketFeedV3", "Operation": "Subscribe", "ClientCode": code,
                                           "MarketFeedData": [{"Exch": EXCHANGES[r["exchange"]], "ExchType": r["segment"], "ScripCode": int(r["token"])} for r in contracts]}))
            await self.status("connected", f"Subscribed to {len(contracts)} contracts; awaiting actual quotes", subscriptions=len(contracts))
            async for data in socket:
                received_at, received_ns = utc_now(), time.time_ns()
                for item in parse_message(data, mapping, received_at=received_at, received_ns=received_ns):
                    await self.on_quote(item)
