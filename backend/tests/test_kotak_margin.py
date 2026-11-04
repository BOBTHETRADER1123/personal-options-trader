"""Mocked SDK margin diagnostics, never live broker orders or sessions."""
import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kotak_margin import ReadOnlyKotakMargin, MarginCheckError, single_order_request, safe_single_order_audit
from app.brokers.kotak import KotakFeed


def contract(**updates):
    return {"id": "kotak:nse_fo:53782", "broker": "kotak", "demo": False, "token": "53782", "symbol": "NIFTY30NOV22700CE",
            "segment": "nse_fo", "expiry": "2030-11-03", "lot_size": 65, "price_multiplier": 1, **updates}


class FakeSDK:
    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {"data": {"stat": "Ok", "stCode": 200,
            "ordMrgn": "12345.678", "reqdMrgn": "23456.781", "avlCash": "99999", "token": "PRIVATE",
            "account": "PRIVATE-ACCOUNT", "message": "PRIVATE-MESSAGE"}}

    def margin_required(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def place_order(self, **kwargs):
        raise AssertionError("No real order endpoint may be called")

    cancel_order = modify_order = positions = limits = place_order


def service(sdk=None, **kwargs):
    return ReadOnlyKotakMargin(lambda: sdk, now=lambda: datetime(2030, 10, 29, tzinfo=timezone.utc).timestamp(), **kwargs)


def test_exact_documented_single_order_arguments_and_scope():
    sdk = FakeSDK()
    result = service(sdk).check(contract(), lots=2, side="SELL", price="185.10")
    assert sdk.calls == [{"exchange_segment": "nse_fo", "price": "185.10", "order_type": "L", "product": "NRML",
                          "quantity": "130", "instrument_token": "53782", "transaction_type": "S"}]
    assert result["order_margin"] == 12345.68 and result["required_margin"] == 23456.78
    assert result["paper_portfolio_verified"] is False
    assert result["scope"] == "single_order_real_account_diagnostic"
    assert all(result[name] is None for name in ("span_margin", "elm", "additional_margin", "total_margin", "hedge_benefit", "final_capital_required"))
    assert result["as_of_ist"].endswith("+05:30") and result["sdk_version"] == "kotakneoapi==3.0.9"
    assert "not be summed" in result["warning"]
    serialized = json.dumps(result)
    assert "PRIVATE" not in serialized and "99999" not in serialized
    assert "PRIVATE" not in json.dumps(safe_single_order_audit(result))


def test_mcx_quantities_not_multiplied_by_monetary_conversion_twice():
    sdk = FakeSDK()
    service(sdk).check(contract(segment="mcx_fo", lot_size=1250, price_multiplier=1250), lots=2, side="BUY", price=18.15)
    assert sdk.calls[0]["quantity"] == "2500"


@pytest.mark.parametrize("change", [{"broker": "fivepaisa"}, {"demo": True}, {"demo": None}, {"segment": "nse_cm"},
    {"token": "secret"}, {"token": "0"}, {"expiry": "bad"}, {"expiry": "2020-11-03"}, {"lot_size": 65.5}])
def test_invalid_contract_never_reaches_sdk(change):
    sdk = FakeSDK()
    with pytest.raises(MarginCheckError):
        service(sdk).check(contract(**change), lots=1, side="BUY", price=10)
    assert not sdk.calls


@pytest.mark.parametrize("inputs", [{"lots": True}, {"lots": 1.2}, {"lots": 0}, {"side": "B"}, {"price": "NaN"},
    {"price": -1}, {"price": "1e999999"}, {"lots": "1e999999"}, {"price": 0}, {"product": "CO"}, {"order_type": "SL"}])
def test_invalid_order_never_reaches_sdk(inputs):
    sdk = FakeSDK()
    with pytest.raises(MarginCheckError):
        service(sdk).check(contract(), **{"lots": 1, "side": "BUY", "price": 10, **inputs})
    assert not sdk.calls


@pytest.mark.parametrize("response", [RuntimeError("PRIVATE-TOKEN"), {"Error": "PRIVATE-TOKEN"},
    {"data": {"stat": "Not_Ok", "stCode": 403, "message": "PRIVATE-TOKEN"}},
    {"data": {"stat": "Ok", "stCode": 200, "ordMrgn": "NaN", "reqdMrgn": "10"}},
    {"data": {"stat": "Ok", "stCode": 200, "ordMrgn": "1e999999", "reqdMrgn": "10"}},
    {"data": {"stat": "Ok", "stCode": 200, "ordMrgn": "10"}}, [], None])
def test_failures_are_fixed_safe_messages_and_never_margin(response):
    sdk = FakeSDK(response)
    if response is None:
        sdk.response = None
    result = service(sdk).check(contract(), lots=1, side="SELL", price=10)
    assert result["status"] == "unavailable" and result["verified"] is False
    assert result["order_margin"] is None and result["total_margin"] is None
    assert "PRIVATE" not in json.dumps(result)


def test_cooldown_and_unavailable_session_do_not_reauthenticate_or_reconnect():
    with pytest.raises(MarginCheckError, match="No authenticated"):
        service().check(contract(), lots=1, side="BUY", price=10)
    sdk = FakeSDK()
    check = service(sdk)
    check.check(contract(), lots=1, side="BUY", price=10)
    with pytest.raises(MarginCheckError, match="Wait a few"):
        check.check(contract(), lots=1, side="BUY", price=10)
    assert len(sdk.calls) == 1
    check._lock.acquire()
    try:
        with pytest.raises(MarginCheckError, match="already running"):
            check.check(contract(), lots=1, side="BUY", price=10)
    finally:
        check._lock.release()


def test_adapter_uses_existing_client_without_feed_start_or_new_login():
    async def callback(*args):
        raise AssertionError("Margin checks may not start the feed")
    adapter = KotakFeed(callback, callback)
    sdk = FakeSDK()
    adapter._margin_client = sdk
    adapter._margin_service.now = lambda: datetime(2030, 10, 29, tzinfo=timezone.utc).timestamp()
    result = asyncio.run(adapter.margin_required(contract(), lots=1, side="BUY", price=10))
    assert result["status"] == "single_order_response" and len(sdk.calls) == 1
    adapter._margin_client = None
    adapter._margin_service._last_started = None
    with pytest.raises(MarginCheckError, match="No authenticated"):
        asyncio.run(adapter.margin_required(contract(), lots=1, side="BUY", price=10))


def test_service_source_contains_only_read_only_broker_operation():
    source = Path("backend/app/kotak_margin.py").read_text()
    assert "client.margin_required(**request)" in source
    for forbidden in (".place_order(", ".modify_order(", ".cancel_order(", ".positions(", ".limits("):
        assert forbidden not in source
