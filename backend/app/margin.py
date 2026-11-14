"""Honest margin availability for a hypothetical paper portfolio.

This module deliberately has no broker client or network calls. Kotak's public
``margin_required`` operation describes one order in a real account, not an
isolated hypothetical multi-leg paper portfolio. Its result cannot establish
portfolio SPAN, ELM or hedge benefit. Expiry payoff is not broker margin.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


KOTAK_MARGIN_DOCUMENTATION = (
    "https://github.com/Kotak-Neo/kotak-neo-python/blob/main/"
    "docs/functions/portfolio/margin_required.md"
)
KOTAK_MARGIN_CALCULATOR = "https://www.kotakneo.com/calculator/margin-calculator/"
FIVEPAISA_MULTI_ORDER_DOCUMENTATION = (
    "https://xstream.5paisa.com/dev-docs/funds-management-system/Multi%20order%20margin"
)
MARGIN_FIELDS = (
    "span_margin", "elm", "additional_margin", "total_margin",
    "hedge_benefit", "final_capital_required",
)
_SOURCES = {
    "kotak": {
        "source": "Kotak Neo public SDK: single-order margin_required only; basket margin unverified",
        "source_url": KOTAK_MARGIN_DOCUMENTATION,
        "reason": (
            "The public Kotak Neo margin_required method accepts one instrument/order. "
            "No officially documented complete hypothetical basket margin adapter has "
            "been verified for this app. Single-order results may include the real "
            "account's positions and must not be added or used as paper basket margin."
        ),
    },
    "fivepaisa": {
        "source": "5paisa official Multi Order Margin documentation; integration disabled",
        "source_url": FIVEPAISA_MULTI_ORDER_DOCUMENTATION,
        "reason": (
            "5paisa documents combined multi-order margin with hedge benefit, but this "
            "application is Kotak Neo only. No 5paisa request or connection is enabled."
        ),
    },
}
_IDENTITY_FIELDS = (
    "instrument_id", "id", "token", "exchange", "segment", "underlying",
    "expiry", "option_type", "side", "strike", "entry_price", "price",
    "lots", "open_lots", "lot_size", "price_multiplier", "enabled",
)
_NUMBER_FIELDS = {
    "strike", "entry_price", "price", "lots", "open_lots", "lot_size", "price_multiplier",
}


def _decimal(value, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number.") from None
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"{name} must be a finite{' positive' if positive else ''} number.")
    return number


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Margin assessment timestamp must be ISO 8601 with a timezone.") from None
    else:
        raise ValueError("Margin assessment timestamp must include a timezone.")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Margin assessment timestamp must include a timezone.")
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _portfolio(legs: Iterable[Mapping], broker: str) -> tuple[list[dict], Decimal | None, Decimal | None, Decimal | None]:
    """Canonical remaining exposure; never retain arbitrary credential fields."""
    canonical = []
    premium = Decimal(0)
    paid = received = Decimal(0)
    paid_complete = received_complete = True
    complete_premium = True
    for leg in legs:
        if not isinstance(leg, Mapping):
            raise ValueError("Portfolio legs must be objects.")
        if leg.get("broker") is not None and leg["broker"] != broker:
            raise ValueError("A margin assessment cannot mix brokers.")
        if leg.get("enabled", True) is False:
            continue
        lots = _decimal(leg.get("open_lots", leg.get("lots", 1)), "Open lots")
        if lots < 0 or lots != lots.to_integral_value():
            raise ValueError("Open lots must be nonnegative integers.")
        if lots == 0:
            continue
        row = {}
        for name in _IDENTITY_FIELDS:
            if name not in leg:
                continue
            value = leg[name]
            if name in _NUMBER_FIELDS and value is not None:
                value = str(_decimal(value, name).normalize())
            elif value is not None and not isinstance(value, (str, int, bool)):
                value = str(value)
            row[name] = value
        row["remaining_lots"] = str(lots.normalize())
        canonical.append(row)
        side = leg.get("side")
        kind = leg.get("option_type")
        if side not in ("BUY", "SELL") or kind not in ("CE", "PE", "FUT"):
            raise ValueError("Invalid portfolio direction or instrument type.")
        lot_size = _decimal(leg.get("lot_size"), "Lot size", positive=True)
        if lot_size != lot_size.to_integral_value():
            raise ValueError("Lot size must be a positive integer.")
        if kind == "FUT":
            continue
        entry = leg.get("entry_price", leg.get("price"))
        multiplier = leg.get("price_multiplier", None if leg.get("exchange") == "MCX" else 1)
        if entry is None or multiplier is None:
            complete_premium = False
            if side == "BUY":
                paid_complete = False
            else:
                received_complete = False
            continue
        price = _decimal(entry, "Entry premium")
        if price < 0:
            raise ValueError("Entry premium must be nonnegative.")
        factor = _decimal(multiplier, "Price multiplier", positive=True)
        cash_flow = price * lots * lot_size * factor
        if side == "BUY":
            paid += cash_flow
        else:
            received += cash_flow
        premium += (1 if side == "SELL" else -1) * cash_flow
    # Leg insertion order has no effect on the hypothetical basket identity.
    canonical.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
    return (canonical, premium if complete_premium else None,
            paid if paid_complete else None, received if received_complete else None)


def unavailable_for_basket(
    legs: Iterable[Mapping], broker: str = "kotak", *, net_premium=None,
    as_of: datetime | str | None = None,
) -> dict:
    """Return provenance and null margin values; no HTTP, logging or order calls.

    ``net_premium`` is signed option cash flow (credit positive, debit negative),
    independently calculated from remaining paper legs. It is never margin.
    ``as_of`` identifies this assessment, not a verified broker quotation.
    """
    if broker not in _SOURCES:
        raise ValueError("Unsupported broker for paper margin assessment.")
    canonical, derived_premium, paid, received = _portfolio(legs, broker)
    premium = derived_premium if net_premium is None else _decimal(net_premium, "Net premium")
    encoded = json.dumps({"broker": broker, "legs": canonical}, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return {
        "status": "unavailable", "label": "Broker margin unavailable", "verified": False,
        "broker": broker, **dict.fromkeys(MARGIN_FIELDS),
        "net_premium": None if premium is None else float(premium.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "premium_paid": None if paid is None else float(paid.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "premium_received": None if received is None else float(received.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "premium_scope": "remaining_open_option_legs_at_entry_prices_excluding_charges",
        "provider_request_performed": False,
        "estimate": None, "verification_scope": "complete_hypothetical_paper_portfolio",
        **_SOURCES[broker], "as_of": _timestamp(as_of),
        "request_fingerprint": fingerprint, "active_leg_count": len(canonical),
        "calculator_url": KOTAK_MARGIN_CALCULATOR if broker == "kotak" else None,
    }


_RESPONSE_NUMBERS = {
    "avlCash", "totMrgnUsd", "mrgnUsd", "ordMrgn", "reqdMrgn", "avlMrgn", "insufFund",
    "span_margin", "elm", "additional_margin", "total_margin", "hedge_benefit",
    "final_capital_required",
}


def safe_margin_audit(assessment: Mapping, response=None) -> dict:
    """Allowlisted debug data for an explicit owner action, never raw responses.

    No headers, account IDs, tokens, SDK messages or arbitrary response strings
    are retained. Calling this helper does not write logs or query a provider.
    Order-check amounts are clearly diagnostic and never promoted to margin.
    """
    broker = assessment.get("broker")
    if broker not in _SOURCES:
        raise ValueError("Unsupported broker for paper margin audit.")
    fingerprint = str(assessment.get("request_fingerprint", ""))
    if re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
        raise ValueError("Margin audit requires a valid request fingerprint.")
    audit = {
        "broker": broker, "status": "unavailable", "verified": False,
        "source": _SOURCES[broker]["source"], "source_url": _SOURCES[broker]["source_url"],
        "as_of": _timestamp(assessment.get("as_of")),
        "request_fingerprint": fingerprint,
        "verification_scope": "complete_hypothetical_paper_portfolio",
        "provider_request_performed": response is not None,
        "response_scope": "diagnostic_only_not_verified_paper_portfolio",
        "response_values": {},
    }

    def extract(value, prefix="", depth=0):
        if not isinstance(value, Mapping) or depth > 3:
            return
        for key, item in value.items():
            if key in _RESPONSE_NUMBERS and not isinstance(item, bool):
                try:
                    amount = float(item)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(amount):
                    audit["response_values"][f"{prefix}{key}"] = amount
            elif key in {"data", "body", "result"}:
                extract(item, f"{prefix}{key}.", depth + 1)
    extract(response)
    return audit
