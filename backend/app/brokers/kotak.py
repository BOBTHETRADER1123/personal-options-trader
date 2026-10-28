"""Kotak Neo current async SFeed using the official kotakneoapi SDK.

No legacy on_message/client.subscribe or order feed is used. Ack waiting is
disabled so the SDK cannot intentionally coalesce pre-ack ticks; symbols are
resolved by the imported master instead of waiting for symbol acknowledgments.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time

from .base import FeedAuthenticationError, FeedProtocolError, ReadOnlyFeed, epoch_timestamp, normalized_quote, optional_price, utc_now


def parse_sfeed(message, contracts: dict, *, received_at=None, received_ns=None) -> dict | None:
    row = message.model_dump() if hasattr(message, "model_dump") else message
    if not isinstance(row, dict):
        raise FeedProtocolError("Kotak SFeed unexpected message")
    if row.get("type") not in {"scrip", "scrip_lite", "index"}:
        return None
    key = (str(row.get("exchange_segment", "")), str(row.get("instrument_token", "")))
    contract = contracts.get(key)
    if not contract:
        raise FeedProtocolError("Kotak SFeed returned an unmapped instrument token")
    last_price = row.get("last_traded_price")
    if isinstance(last_price, bool):
        raise FeedProtocolError("Invalid market-feed price")
    # The official SFeed decoder exposes the scaled LTP, including zero for an
    # untraded strike. No usable LTP means no new quote, not a broken socket.
    # Keep the previous mark and its timestamp unchanged. Invalid/negative or
    # nonfinite prices still fail validation; broker auth errors remain SDK
    # exceptions handled by stream(), independently of these market records.
    if isinstance(last_price, str) and not last_price.strip():
        last_price = None
    price = optional_price(last_price)
    if price is None:
        return None
    buy, sell = row.get("buy") or [], row.get("sell") or []
    bid = optional_price(buy[0].get("price")) if buy else None
    ask = optional_price(sell[0].get("price")) if sell else None
    return normalized_quote("kotak", contract, price, received_at=received_at,
                            received_ns=received_ns, bid=bid, ask=ask,
                            volume=row.get("volume_traded_today"), open_interest=row.get("open_interest"),
                            exchange_timestamp=epoch_timestamp(row.get("last_update_time") or row.get("last_trade_time")),
                            previous_close=row.get("close_price"), sequence=None, event_kind=f"sfeed_{row['type']}", raw_message=row)


class KotakFeed(ReadOnlyFeed):
    broker = "kotak"

    def configured(self):
        if os.environ.get("KOTAK_SID") and os.environ.get("KOTAK_UCC"):
            return True
        return all(os.environ.get(k) for k in ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE_NUMBER", "KOTAK_UCC", "KOTAK_MPIN")) and bool(os.environ.get("KOTAK_TOTP") or os.environ.get("KOTAK_TOTP_SECRET"))

    async def stream(self, contracts):
        try:
            # Raw SDK authentication errors can include response bodies. The
            # app emits fixed sanitized states instead of SDK request traces.
            os.environ["NEO_LOG_LEVEL"] = "NOLOG"
            os.environ["NEO_LOG_FILE_ENABLED"] = "false"
            from neo_api_client import NeoAPI
            from neo_api_client.logger import setup_logging
            from neo_api_client.websocket.feed import SFeedWebSocket, WsToken
            from neo_api_client.websocket.feed.exceptions import AuthenticationError, SubscriptionError
            setup_logging(level="NOLOG", file_enabled=False)
        except ImportError as exc:
            raise FeedProtocolError("Install backend/requirements-brokers.txt to enable Kotak SFeed") from exc
        if len(contracts) > 3000:
            raise FeedProtocolError("Kotak SFeed subscriptions exceed 3000; narrow the watchlist")
        mapping = {(r["segment"], str(r["token"])): r for r in contracts}
        if os.environ.get("KOTAK_SID"):
            ws = SFeedWebSocket(user=os.environ["KOTAK_UCC"], auth=os.environ["KOTAK_SID"],
                               ack_wait_timeout=0, max_reconnect_attempts=0, max_connect_retries=0)
        else:
            totp = os.environ.get("KOTAK_TOTP")
            if os.environ.get("KOTAK_TOTP_SECRET"):
                import pyotp
                totp = pyotp.TOTP(os.environ["KOTAK_TOTP_SECRET"]).now()
            client = NeoAPI(consumer_key=os.environ["KOTAK_CONSUMER_KEY"], environment="prod", timeout=20)
            try:
                await asyncio.to_thread(client.totp_login, mobile_number=os.environ["KOTAK_MOBILE_NUMBER"],
                                        ucc=os.environ["KOTAK_UCC"], totp=totp)
                await asyncio.to_thread(client.totp_validate, mpin=os.environ["KOTAK_MPIN"])
                if not getattr(client.configuration, "edit_sid", None):
                    raise ValueError("Authentication did not establish a valid session")
            except Exception as exc:
                # SDK exceptions can contain login responses. Surface a fixed
                # explanation instead of returning secrets to the dashboard.
                raise FeedAuthenticationError("Kotak TOTP login failed; refresh the daily TOTP/session on the backend") from None
            ws = client.create_websocket(ack_wait_timeout=0, max_reconnect_attempts=0, max_connect_retries=0)
        tokens = [WsToken(r["segment"], str(r["token"])) for r in contracts]
        try:
            async with ws:
                await ws.subscribe_depth(tokens)
                await self.status("connected", f"SFeed subscribed to {len(contracts)} contracts; awaiting actual quotes", subscriptions=len(contracts))
                try:
                    async for message in ws:
                        quote = parse_sfeed(message, mapping, received_at=utc_now(), received_ns=time.time_ns())
                        if quote:
                            await self.on_quote(quote)
                finally:
                    # Best effort unsubscribe on shutdown/reconfiguration. A
                    # closed socket removes its subscriptions at the broker.
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(2):
                            await ws.unsubscribe_depth(tokens)
        except AuthenticationError:
            raise FeedAuthenticationError("Kotak SFeed session rejected; renew the daily TOTP/session on the backend") from None
        except SubscriptionError:
            raise FeedProtocolError("Kotak rejected SFeed subscriptions; verify token master, permissions and 3000-token limit") from None
