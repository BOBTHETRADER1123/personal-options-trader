"""Explicit read-only Kotak single-order diagnostic; never basket margin.

The pinned official SDK 3.0.9 accepts one token/direction/quantity per request.
Existing *real* account positions can affect its answer. Do not add these
answers, infer paper hedge benefits, or promote them to portfolio margin.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .margin import KOTAK_MARGIN_DOCUMENTATION, KOTAK_MARGIN_CALCULATOR

IST = ZoneInfo("Asia/Kolkata")
SDK_VERSION = "kotakneoapi==3.0.9"
_WARNING = ("Single-order diagnostic for the real Kotak account; existing positions/funds "
            "may affect the response. This is not isolated paper-basket margin and must "
            "not be summed across legs or used as hedge benefit.")


class MarginCheckError(ValueError):
    """Fixed messages only; never include broker response strings or secrets."""


def _number(value, name, *, positive=False, integer=False):
    if isinstance(value, bool) or len(str(value)) > 128:
        raise MarginCheckError(f"{name} must be a finite number.")
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise MarginCheckError(f"{name} must be a finite number.") from None
    if not value.is_finite() or value < 0 or value > Decimal("1e15") or (positive and value == 0):
        raise MarginCheckError(f"{name} is outside the supported range.")
    if integer and value != value.to_integral_value():
        raise MarginCheckError(f"{name} must be an integer.")
    return value


def single_order_request(contract, *, lots, side, price, product="NRML", order_type="L", today=None):
    """Build SDK arguments from a server-resolved, genuine contract master row.

    Caller must resolve the contract ID from its own catalog; never trust a
    browser-supplied token or lot size. Quantity uses the broker lot size once,
    not the P&L monetary multiplier. No stop/cover order workflow is exposed.
    """
    if (not isinstance(contract, Mapping) or contract.get("broker") != "kotak"
            or contract.get("demo") is not False):
        raise MarginCheckError("Select a genuine Kotak contract from the current master.")
    if contract.get("segment") not in {"nse_fo", "bse_fo", "mcx_fo"}:
        raise MarginCheckError("Only supported derivatives segments can be checked.")
    if not isinstance(contract.get("token"), str) or not contract["token"].isdigit() or int(contract["token"]) <= 0:
        raise MarginCheckError("The contract master has no valid instrument token.")
    try:
        expiry = date.fromisoformat(contract["expiry"])
    except (KeyError, TypeError, ValueError):
        raise MarginCheckError("The contract master has no valid expiry date.") from None
    if expiry < (today or datetime.now(IST).date()):
        raise MarginCheckError("Expired contracts cannot receive a current margin check.")
    if side not in {"BUY", "SELL"} or product not in {"NRML", "MIS"} or order_type not in {"L", "MKT"}:
        raise MarginCheckError("Unsupported single-order margin inputs.")
    quantity = _number(lots, "Lots", positive=True, integer=True) * _number(contract.get("lot_size"), "Lot size", positive=True, integer=True)
    if quantity > 100000000:
        raise MarginCheckError("Quantity is outside the supported range.")
    amount = _number(price, "Price")
    if order_type == "L" and amount == 0:
        raise MarginCheckError("A limit margin check requires a positive price.")
    return {"exchange_segment": contract["segment"], "price": format(amount, "f"),
            "order_type": order_type, "product": product, "quantity": format(quantity, "f"),
            "instrument_token": contract["token"], "transaction_type": "B" if side == "BUY" else "S"}


class ReadOnlyKotakMargin:
    """Reuse the feed's authenticated client without a new login or feed restart.

    ``client_getter`` returns None when no REST session was established (e.g.
    websocket-only KOTAK_SID). Exactly one diagnostic can run at once, with a
    small cooldown. The SDK's existing REST timeout bounds the network call.
    """
    def __init__(self, client_getter, *, now=None, cooldown_seconds=5):
        self.client_getter = client_getter
        self.now = now or time.time
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._last_started = None

    def check(self, contract, *, lots, side, price, product="NRML", order_type="L"):
        instant = datetime.fromtimestamp(self.now(), timezone.utc)
        request = single_order_request(contract, lots=lots, side=side, price=price, product=product,
                                       order_type=order_type, today=instant.astimezone(IST).date())
        if not self._lock.acquire(blocking=False):
            raise MarginCheckError("A read-only margin check is already running. Try again shortly.")
        try:
            now = self.now()
            if self._last_started is not None and now - self._last_started < self.cooldown_seconds:
                raise MarginCheckError("Wait a few seconds before another margin check.")
            client = self.client_getter()
            if client is None:
                raise MarginCheckError("No authenticated Kotak REST session is available. Connect the market feed first.")
            self._last_started = now
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            result = {"broker": "kotak", "status": "unavailable", "verified": False,
                      "scope": "single_order_real_account_diagnostic", "paper_portfolio_verified": False,
                      "label": "Single-order check; broker basket margin unavailable", "warning": _WARNING,
                      "source": "Kotak Neo margin_required (official SDK)", "source_url": KOTAK_MARGIN_DOCUMENTATION,
                      "sdk_version": SDK_VERSION, "as_of": instant.isoformat(timespec="microseconds"),
                      "as_of_ist": instant.astimezone(IST).isoformat(timespec="seconds"),
                      "request_fingerprint": digest, "request": request,
                      "order_margin": None, "required_margin": None, "span_margin": None, "elm": None,
                      "additional_margin": None, "total_margin": None, "hedge_benefit": None,
                      "final_capital_required": None, "calculator_url": KOTAK_MARGIN_CALCULATOR,
                      "provider_request_performed": True, "response_values": {}}
            try:
                # The sole broker operation this module is permitted to invoke.
                response = client.margin_required(**request)
            except Exception:
                result["reason"] = "Kotak single-order margin request failed; no basket margin is available."
                return result
            row = response.get("data") if isinstance(response, Mapping) else None
            if (not isinstance(row, Mapping) or any(isinstance(key, str) and key.lower() in {"error", "error message"} for key in response)
                    or row.get("stat") not in {"Ok", "OK", "ok"} or str(row.get("stCode")) != "200"):
                result["reason"] = "Kotak did not return a successful recognized margin response; renew authorization or retry later."
                return result
            try:
                amounts = {key: _number(row[key], key) for key in ("ordMrgn", "reqdMrgn")}
            except (KeyError, MarginCheckError):
                result["reason"] = "Kotak returned incomplete or invalid margin amounts."
                return result
            result.update({"status": "single_order_response", "verified": True,
                           "order_margin": float(amounts["ordMrgn"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                           "required_margin": float(amounts["reqdMrgn"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                           "response_values": {key: format(value, "f") for key, value in amounts.items()},
                           "reason": _WARNING})
            # Account IDs, funds, headers, tokens, arbitrary strings and SDK
            # exceptions are never returned or logged, even on broker errors.
            return result
        finally:
            self._lock.release()


def safe_single_order_audit(result):
    """A small structured event for the protected endpoint's explicit action."""
    keys = ("broker", "status", "scope", "paper_portfolio_verified", "source", "source_url", "sdk_version",
            "as_of", "as_of_ist", "request_fingerprint", "provider_request_performed", "response_values")
    # Only call with this module's result; never serialize an SDK response.
    return {key: result.get(key) for key in keys}
