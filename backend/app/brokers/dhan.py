"""DhanHQ v2 Full Data WebSocket, from the official binary protocol.

References: https://dhanhq.co/docs/v2/live-market-feed/ and official
https://github.com/dhan-oss/DhanHQ-py/blob/main/src/dhanhq/marketfeed.py
"""
from __future__ import annotations

import json
import os
import struct
import time
from urllib.parse import urlencode

from .base import FeedAuthenticationError, FeedProtocolError, ReadOnlyFeed, epoch_timestamp, normalized_quote, optional_price, utc_now

SEGMENTS = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO"}
DISCONNECT_REASONS = {804: "Subscription limit exceeded", 805: "Connection limit reached", 806: "Data API subscription required", 807: "Token expired", 808: "Authentication rejected", 809: "Token invalid", 810: "Client ID invalid"}


def parse_packet(packet: bytes, contracts: dict[tuple[str, str], dict], *, received_at=None, received_ns=None) -> dict | None:
    if len(packet) < 8:
        raise FeedProtocolError("Dhan packet header truncated")
    code, size, segment_code, token = struct.unpack_from("<BHBI", packet)
    if size != len(packet):
        raise FeedProtocolError("Dhan packet length mismatch")
    if code == 50:
        if size < 10:
            raise FeedProtocolError("Dhan disconnect packet truncated")
        reason = struct.unpack_from("<H", packet, 8)[0]
        message = DISCONNECT_REASONS.get(reason, f"Broker disconnected ({reason})")
        if reason in {806, 807, 808, 809, 810}:
            raise FeedAuthenticationError(message)
        raise FeedProtocolError(message)
    contract = contracts.get((SEGMENTS.get(segment_code, ""), str(token)))
    if contract is None or code not in {2, 4, 8}:
        return None
    if code == 2:
        if size < 16:
            raise FeedProtocolError("Dhan ticker packet truncated")
        ltp, stamp = struct.unpack_from("<fI", packet, 8)
        fields = {"exchange_timestamp": epoch_timestamp(stamp)}
    elif code == 4:
        if size < 50:
            raise FeedProtocolError("Dhan quote packet truncated")
        parsed = struct.unpack_from("<fHIfIIIffff", packet, 8)
        ltp = parsed[0]
        fields = {"exchange_timestamp": epoch_timestamp(parsed[2]), "last_quantity": parsed[1],
                  "volume": parsed[4], "bid": None, "ask": None}
    else:
        if size < 162:
            raise FeedProtocolError("Dhan full packet truncated")
        parsed = struct.unpack_from("<fHIfIIIIIIffff", packet, 8)
        ltp = parsed[0]
        bid_qty, ask_qty, bid_orders, ask_orders, bid, ask = struct.unpack_from("<IIHHff", packet, 62)
        fields = {"exchange_timestamp": epoch_timestamp(parsed[2]), "last_quantity": parsed[1],
                  "volume": parsed[4], "open_interest": parsed[7], "bid": optional_price(bid), "ask": optional_price(ask),
                  "bid_quantity": bid_qty, "ask_quantity": ask_qty}
    return normalized_quote("dhan", contract, ltp, received_at=received_at, received_ns=received_ns,
                            **fields, sequence=None, event_kind=f"dhan_packet_{code}", raw_packet_hex=packet.hex())


def parse_frame(data: bytes, contracts: dict, *, received_at=None, received_ns=None) -> list[dict]:
    """Handle one or several concatenated protocol packets without sampling."""
    quotes, offset = [], 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise FeedProtocolError("Dhan frame trailing bytes")
        size = struct.unpack_from("<H", data, offset + 1)[0]
        if size < 8 or offset + size > len(data):
            raise FeedProtocolError("Dhan frame invalid packet length")
        quote = parse_packet(data[offset:offset + size], contracts, received_at=received_at, received_ns=received_ns)
        if quote:
            quotes.append(quote)
        offset += size
    return quotes


class DhanFeed(ReadOnlyFeed):
    broker = "dhan"

    def configured(self):
        return bool(os.environ.get("DHAN_CLIENT_ID") and os.environ.get("DHAN_ACCESS_TOKEN"))

    async def stream(self, contracts):
        from websockets.asyncio.client import connect
        if len(contracts) > 5000:
            raise FeedProtocolError("Dhan subscriptions exceed 5000 on this connection; narrow the watchlist")
        mapping = {(r["segment"], str(r["token"])): r for r in contracts}
        params = urlencode({"version": "2", "token": os.environ["DHAN_ACCESS_TOKEN"],
                            "clientId": os.environ["DHAN_CLIENT_ID"], "authType": "2"})
        # Never log this URL: it contains the access token.
        async with connect("wss://api-feed.dhan.co?" + params, ping_interval=20, ping_timeout=20,
                           close_timeout=5, open_timeout=20, max_queue=32, max_size=8_388_608) as socket:
            for start in range(0, len(contracts), 100):
                batch = contracts[start:start + 100]
                await socket.send(json.dumps({"RequestCode": 21, "InstrumentCount": len(batch),
                                               "InstrumentList": [{"ExchangeSegment": row["segment"], "SecurityId": str(row["token"])} for row in batch]}))
            await self.status("connected", f"Subscribed to {len(contracts)} contracts; awaiting actual quotes", subscriptions=len(contracts))
            async for data in socket:
                received_at, received_ns = utc_now(), time.time_ns()
                if not isinstance(data, bytes):
                    # Protocol documents binary data. Treat unexpected text as
                    # a visible feed error instead of inventing a quote.
                    raise FeedProtocolError("Dhan returned unexpected text; check session and permissions")
                for quote in parse_frame(data, mapping, received_at=received_at, received_ns=received_ns):
                    await self.on_quote(quote)
