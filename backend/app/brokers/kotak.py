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

    def __init__(self, on_quote, on_status):
        super().__init__(on_quote, on_status)
        self._subscription_lock = asyncio.Lock()
        self._socket = None
        self._desired_contracts = []
        self._subscribed = {}
        self._mapping = {}

    async def _apply_subscriptions(self, contracts):
        from neo_api_client.websocket.feed import WsToken
        desired = {(r["segment"], str(r["token"])): r for r in contracts}
        if len(desired) > 3000:
            raise FeedProtocolError("Kotak SFeed subscriptions exceed 3000; narrow the watchlist")
        removed = self._subscribed.keys() - desired.keys()
        added = desired.keys() - self._subscribed.keys()
        # Retained position tokens remain subscribed throughout a view change.
        # Remove first so a large chain change cannot exceed the token budget.
        if removed:
            await self._socket.unsubscribe_scrips([WsToken(*key) for key in sorted(removed)])
            for key in removed:
                self._subscribed.pop(key, None)
                self._mapping.pop(key, None)
        # Install new metadata before subscribing: ticks may arrive while send
        # yields. Queued ticks for a removed token are ignored by stream().
        self._mapping.update(desired)
        if added:
            await self._socket.subscribe_scrips([WsToken(*key) for key in sorted(added)])
        self._subscribed.update(desired)
        await self.status("connected", f"SFeed subscribed to {len(desired)} contracts; awaiting actual quotes", subscriptions=len(desired))

    async def reconfigure(self, contracts):
        """Change view demand without closing the broker's existing socket."""
        desired = list(contracts)
        if len(desired) > 3000:
            raise FeedProtocolError("Kotak SFeed subscriptions exceed 3000; narrow the watchlist")
        self._desired_contracts = desired
        async with self._subscription_lock:
            if self._socket is None:
                # Initial authentication is still pending; stream reads this
                # latest desired list immediately before its first subscribe.
                return
            try:
                async with asyncio.timeout(10):
                    await self._apply_subscriptions(desired)
            except FeedProtocolError:
                raise
            except Exception:
                raise ConnectionError("Kotak SFeed subscription update failed") from None

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
        self._desired_contracts = list(contracts)
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
        try:
            async with ws:
                try:
                    async with self._subscription_lock:
                        self._socket = ws
                        self._subscribed = {}
                        self._mapping = {}
                        # Level-4 touch line retains LTP, OI, volume, original
                        # timestamps and best bid/ask, without unused full depth.
                        async with asyncio.timeout(10):
                            await self._apply_subscriptions(self._desired_contracts)
                    async for message in ws:
                        row = message.model_dump() if hasattr(message, "model_dump") else message
                        if isinstance(row, dict) and row.get("type") in {"scrip", "scrip_lite", "index"}:
                            key = (str(row.get("exchange_segment", "")), str(row.get("instrument_token", "")))
                            if key not in self._mapping:
                                # A previously subscribed token may already be
                                # queued when a view is removed. Never use it as
                                # another contract or reconnect the entire feed.
                                continue
                        quote = parse_sfeed(row, self._mapping, received_at=utc_now(), received_ns=time.time_ns())
                        if quote:
                            await self.on_quote(quote)
                finally:
                    async with self._subscription_lock:
                        self._socket = None
                        tokens = [WsToken(*key) for key in sorted(self._subscribed)]
                        self._subscribed = {}
                        self._mapping = {}
                        # Closing a socket also removes subscriptions at the
                        # broker; explicit unsubscribe is best effort only.
                        with contextlib.suppress(Exception):
                            async with asyncio.timeout(2):
                                if tokens:
                                    await ws.unsubscribe_scrips(tokens)
        except AuthenticationError as exc:
            # An unresponsive authentication endpoint is a transport outage,
            # not evidence that the owner's daily session was rejected. Let
            # FeedManager's bounded backoff reconnect rather than stopping the
            # collector indefinitely. Never expose the raw SDK response.
            if str(exc) == "Authentication timeout":
                raise ConnectionError("Kotak SFeed authentication timed out") from None
            raise FeedAuthenticationError("Kotak SFeed session rejected; renew the daily TOTP/session on the backend") from None
        except SubscriptionError:
            raise FeedProtocolError("Kotak rejected SFeed subscriptions; verify token master, permissions and 3000-token limit") from None
