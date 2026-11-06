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


def _exception_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        yield exc
        exc = getattr(exc, "original_exception", None) or getattr(exc, "__cause__", None)


def _http_status(chain):
    for exc in chain:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(exc, "status", None)
        if isinstance(status, str) and status.isdigit():
            status = int(status)
        if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
            return status
    return None


def is_transient_network_error(exc: BaseException) -> bool:
    """Inspect typed failures without exposing SDK response contents."""
    chain = tuple(_exception_chain(exc))
    status = _http_status(chain)
    if status is not None:
        # An explicit credential rejection takes precedence over a wrapper
        # with a generic transport-error type.
        return status in {408, 424, 429} or status >= 500
    transports = (TimeoutError, ConnectionError, OSError)
    try:
        from httpx import TransportError
        transports += (TransportError,)
    except ImportError:
        pass
    try:
        from websockets.exceptions import ConnectionClosed
        transports += (ConnectionClosed,)
    except ImportError:
        pass
    return any(isinstance(cause, transports) for cause in chain)


def _subscription_failure(exc: BaseException) -> Exception:
    if is_transient_network_error(exc):
        return ConnectionError("Kotak SFeed transport interrupted during subscription")
    return FeedProtocolError("Kotak rejected SFeed subscriptions; verify token master, permissions and 3000-token limit")


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
    bid_raw = buy[0].get("price") if buy else None
    ask_raw = sell[0].get("price") if sell else None
    if isinstance(bid_raw, bool) or isinstance(ask_raw, bool):
        raise FeedProtocolError("Invalid market-feed depth price")
    bid = optional_price(bid_raw)
    ask = optional_price(ask_raw)
    updated_at = epoch_timestamp(row.get("last_update_time"))
    traded_at = epoch_timestamp(row.get("last_trade_time")) if updated_at is None else None
    timestamp_basis = "last_update_time" if updated_at is not None else "last_trade_time" if traded_at is not None else "unavailable"
    return normalized_quote("kotak", contract, price, received_at=received_at,
                            received_ns=received_ns, bid=bid, ask=ask,
                            volume=row.get("volume_traded_today"), open_interest=row.get("open_interest"),
                            exchange_timestamp=updated_at or traded_at, exchange_timestamp_basis=timestamp_basis,
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
        self._quoted_keys = set()
        self._awaiting_since = None
        self._last_totp_step = None
        self._rejected_ticks = 0
        self._last_rejected_warning_at = None
        self._margin_client = None
        from ..kotak_margin import ReadOnlyKotakMargin
        self._margin_service = ReadOnlyKotakMargin(lambda: self._margin_client)

    async def margin_required(self, contract, **inputs):
        """Explicit read-only single-order diagnostic, never a paper basket sum."""
        return await asyncio.to_thread(self._margin_service.check, contract, **inputs)

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
        self._quoted_keys.intersection_update(desired)
        if self._quoted_keys:
            self._awaiting_since = None
            await self.status("connected", "Receiving Kotak market quotes", subscriptions=len(desired),
                              awaiting_since=None)
        else:
            self._awaiting_since = self._awaiting_since or utc_now()
            await self.status("awaiting_quotes", "Kotak subscriptions sent; waiting for the first usable quote",
                              subscriptions=len(desired), awaiting_since=self._awaiting_since)

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
            from neo_api_client.websocket.feed.exceptions import SubscriptionError
            try:
                async with asyncio.timeout(10):
                    await self._apply_subscriptions(desired)
            except SubscriptionError as exc:
                raise _subscription_failure(exc) from None
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
        self._margin_client = None
        if os.environ.get("KOTAK_SID"):
            ws = SFeedWebSocket(user=os.environ["KOTAK_UCC"], auth=os.environ["KOTAK_SID"],
                               ack_wait_timeout=0, max_reconnect_attempts=0, max_connect_retries=0)
        else:
            totp = os.environ.get("KOTAK_TOTP")
            if os.environ.get("KOTAK_TOTP_SECRET"):
                import pyotp
                # A fast reconnect must not resend the just-consumed one-time
                # code. Sleep asynchronously until the next 30-second window.
                instant = time.time()
                step = int(instant // 30)
                if self._last_totp_step == step:
                    await asyncio.sleep(30 - (instant % 30) + 1)
                    instant = time.time()
                    step = int(instant // 30)
                self._last_totp_step = step
                # Tie the generated code to the sampled step even if the wall
                # clock crosses a boundary while generation is running.
                totp = pyotp.TOTP(os.environ["KOTAK_TOTP_SECRET"]).at(instant)
            client = NeoAPI(consumer_key=os.environ["KOTAK_CONSUMER_KEY"], environment="prod", timeout=20)
            try:
                # The official SDK normally returns HTTP error responses. Its
                # existing REST policy exposes status-bearing ApiException so
                # an outage cannot be confused with a rejected TOTP/MPIN.
                rest = getattr(getattr(client, "api_client", None), "rest_client", None)
                if rest is not None and hasattr(rest, "raise_on_error"):
                    rest.raise_on_error = True
                login = await asyncio.to_thread(client.totp_login, mobile_number=os.environ["KOTAK_MOBILE_NUMBER"],
                                                ucc=os.environ["KOTAK_UCC"], totp=totp)
                if isinstance(login, dict) and any(str(key).lower() == "error" for key in login):
                    raise ValueError("Authentication was not accepted")
                await asyncio.to_thread(client.totp_validate, mpin=os.environ["KOTAK_MPIN"])
                if not getattr(client.configuration, "edit_sid", None):
                    raise ValueError("Authentication did not establish a valid session")
            except Exception as exc:
                if is_transient_network_error(exc):
                    raise ConnectionError("Kotak login endpoint unreachable; retrying with bounded backoff") from None
                # SDK exceptions can contain login responses. Surface a fixed
                # explanation instead of returning secrets to the dashboard.
                raise FeedAuthenticationError("Kotak TOTP login failed; refresh the daily TOTP/session on the backend") from None
            ws = client.create_websocket(ack_wait_timeout=0, max_reconnect_attempts=0, max_connect_retries=0)
            self._margin_client = client
        entered = False
        try:
            async with ws:
                entered = True
                try:
                    async with self._subscription_lock:
                        self._socket = ws
                        self._subscribed = {}
                        self._mapping = {}
                        self._quoted_keys.clear()
                        self._awaiting_since = None
                        # Level-4 touch line retains LTP, OI, volume, original
                        # timestamps and best bid/ask, without unused full depth.
                        async with asyncio.timeout(10):
                            await self._apply_subscriptions(self._desired_contracts)
                    async for message in ws:
                        try:
                            row = message.model_dump() if hasattr(message, "model_dump") else message
                            if isinstance(row, dict) and row.get("type") in {"scrip", "scrip_lite", "index"}:
                                key = (str(row.get("exchange_segment", "")), str(row.get("instrument_token", "")))
                                if key not in self._mapping:
                                    # Removed-token ticks may already be queued.
                                    continue
                            quote = parse_sfeed(row, self._mapping, received_at=utc_now(), received_ns=time.time_ns())
                        except (FeedProtocolError, TypeError, AttributeError, KeyError, ValueError, IndexError, OverflowError):
                            # One corrupt tick cannot tear down all other tokens.
                            # Keep its previous quote/time unchanged; no price is
                            # invented. Fixed diagnostics are limited to one per
                            # 30 seconds even if malformed messages arrive fast.
                            self._rejected_ticks += 1
                            now = time.monotonic()
                            if self._last_rejected_warning_at is None or now - self._last_rejected_warning_at >= 30:
                                self._last_rejected_warning_at = now
                                state = "connected" if self._quoted_keys else "awaiting_quotes"
                                await self.status(state, f"Ignored {self._rejected_ticks} malformed market messages; other contracts continue",
                                                  subscriptions=len(self._subscribed), malformed_messages=self._rejected_ticks)
                            continue
                        if quote:
                            await self.on_quote(quote)
                            key = (quote["segment"], str(quote["token"]))
                            if key in self._mapping:
                                self._quoted_keys.add(key)
                                self._awaiting_since = None
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
            if str(exc) == "Authentication timeout" or is_transient_network_error(exc):
                raise ConnectionError("Kotak SFeed transport interrupted during authentication") from None
            raise FeedAuthenticationError("Kotak SFeed session rejected; renew the daily TOTP/session on the backend") from None
        except SubscriptionError as exc:
            raise _subscription_failure(exc) from None
        except Exception as exc:
            # The SDK's initial dial wraps the WebSocket handshake error. A
            # broker HTTP rejection must not trigger repeated login attempts.
            status = _http_status(_exception_chain(exc))
            if status in {401, 403}:
                raise FeedAuthenticationError("Kotak SFeed session rejected; renew the daily TOTP/session on the backend") from None
            if status is not None and not is_transient_network_error(exc):
                raise FeedProtocolError("Kotak rejected the SFeed connection request") from None
            raise
        finally:
            self._margin_client = None
            # SDK 3.0.9 can open the socket and fail authenticating inside
            # __aenter__. Python does not call __aexit__ for a failed entry.
            if not entered:
                close = getattr(ws, "close", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(15):
                            await close()
