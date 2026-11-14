"""Unverified scalar order checks must never become paper basket margin."""
import json
import socket
from datetime import datetime, timezone

import pytest

from app.margin import MARGIN_FIELDS, safe_margin_audit, unavailable_for_basket


def legs():
    return [
        {"broker": "kotak", "underlying": "NIFTY", "expiry": "2026-11-03",
         "option_type": kind, "side": side, "strike": strike, "price": price,
         "lots": 1, "lot_size": 65, "price_multiplier": 1}
        for side, strike, kind, price in [
            ("SELL", 22700, "CE", 185.10), ("SELL", 22700, "PE", 146.50),
            ("BUY", 22850, "CE", 111.35), ("BUY", 22950, "CE", 286.20),
            ("BUY", 22550, "PE", 91.95),
        ]
    ]


def test_five_leg_premium_does_not_invent_margin_or_hedge_benefit(monkeypatch, caplog):
    def no_network(*args, **kwargs):
        raise AssertionError("Paper basket margin must not call single-order or order APIs")
    monkeypatch.setattr(socket, "socket", no_network)
    result = unavailable_for_basket(legs(), as_of="2026-10-29T10:00:00+05:30")
    assert result["net_premium"] == -10263.50
    assert result["status"] == "unavailable" and result["verified"] is False
    assert result["label"] == "Broker margin unavailable"
    assert all(result[key] is None for key in MARGIN_FIELDS)
    assert result["estimate"] is None and result["active_leg_count"] == 5
    assert result["as_of"] == "2026-10-29T04:30:00.000000+00:00"
    assert "margin_required" in result["source"]
    assert "single-order" in result["reason"].lower()
    assert not caplog.records


def test_audit_is_allowlisted_and_never_promotes_single_order_response():
    result = unavailable_for_basket(legs())
    response = {
        "Authorization": "Bearer PRIVATE_TOKEN", "sid": "PRIVATE_SID",
        "data": {"ordMrgn": "20140.19", "reqdMrgn": 0, "stat": "SECRET_IN_MESSAGE",
                 "Message": "PRIVATE_MPIN", "clientCode": "PRIVATE_ACCOUNT",
                 "body": {"span_margin": 10, "access_token": "PRIVATE_REFRESH"}},
        "headers": {"elm": 9999}, "credentials": {"total_margin": 9999},
    }
    audit = safe_margin_audit(result, response)
    encoded = json.dumps(audit)
    for secret in ["PRIVATE_TOKEN", "PRIVATE_SID", "SECRET_IN_MESSAGE", "PRIVATE_MPIN",
                   "PRIVATE_ACCOUNT", "PRIVATE_REFRESH", "Authorization", "credentials", "headers"]:
        assert secret not in encoded
    assert audit["response_values"] == {
        "data.ordMrgn": 20140.19, "data.reqdMrgn": 0.0, "data.body.span_margin": 10.0,
    }
    assert audit["response_scope"] == "diagnostic_only_not_verified_paper_portfolio"
    assert result["total_margin"] is None and result["span_margin"] is None


def test_timestamp_is_assessment_time_and_not_an_invented_broker_quote():
    before = datetime.now(timezone.utc)
    result = unavailable_for_basket(legs())
    after = datetime.now(timezone.utc)
    assert before <= datetime.fromisoformat(result["as_of"]) <= after
    assert "quote_timestamp" not in result
    audit = safe_margin_audit(result)
    assert audit["provider_request_performed"] is False and audit["response_values"] == {}


def test_permuting_legs_does_not_change_basket_but_exposure_changes_do():
    original = legs()
    result = unavailable_for_basket(original)
    assert unavailable_for_basket(list(reversed(original)))["request_fingerprint"] == result["request_fingerprint"]
    for change in [{"lots": 2}, {"open_lots": 0}, {"price": 250}, {"expiry": "2026-11-10"},
                   {"strike": 23000}, {"side": "SELL"}]:
        changed = [dict(leg) for leg in original]
        changed[2].update(change)
        assert unavailable_for_basket(changed)["request_fingerprint"] != result["request_fingerprint"]


def test_partial_exit_recalculates_only_remaining_premium_and_ignores_disabled_leg():
    original = legs()
    original[2].update(lots=2, open_lots=1)
    assert unavailable_for_basket(original)["net_premium"] == -10263.50
    original[2]["open_lots"] = 0
    assert unavailable_for_basket(original)["net_premium"] == -3025.75
    original[2]["open_lots"] = 1
    original[2]["enabled"] = False
    assert unavailable_for_basket(original)["net_premium"] == -3025.75


def test_futures_do_not_count_contract_notional_as_option_premium():
    future = [{"side": "BUY", "option_type": "FUT", "price": 24000, "lot_size": 65, "lots": 1}]
    result = unavailable_for_basket(future)
    assert result["net_premium"] == 0 and result["total_margin"] is None


def test_unverified_monetary_conversion_does_not_guess_premium():
    original = legs()
    original[0]["price_multiplier"] = None
    result = unavailable_for_basket(original)
    assert result["net_premium"] is None and result["total_margin"] is None


def test_retired_broker_is_independent_and_does_not_enable_connection():
    five = [{**leg, "broker": "fivepaisa"} for leg in legs()]
    result = unavailable_for_basket(five, broker="fivepaisa")
    assert result["status"] == "unavailable" and result["verified"] is False
    assert "disabled" in result["source"] and "Kotak Neo only" in result["reason"]
    assert result["request_fingerprint"] != unavailable_for_basket(legs())["request_fingerprint"]
    with pytest.raises(ValueError, match="mix brokers"):
        unavailable_for_basket(five)


@pytest.mark.parametrize("change", [
    {"lots": -1}, {"lots": 1.5}, {"open_lots": float("nan")}, {"lot_size": 0},
    {"lot_size": 1.5}, {"price": -1}, {"price": float("inf")},
    {"price_multiplier": -1}, {"side": "HOLD"}, {"option_type": "CALL"}, {"lots": True},
])
def test_invalid_exposure_is_rejected(change):
    original = legs()
    original[0].update(change)
    with pytest.raises(ValueError):
        unavailable_for_basket(original)


@pytest.mark.parametrize("as_of", ["not-a-time", "2026-10-29T10:00:00", datetime(2026, 10, 29), 123])
def test_naive_or_invalid_assessment_timestamp_is_rejected(as_of):
    with pytest.raises(ValueError, match="timestamp"):
        unavailable_for_basket(legs(), as_of=as_of)


def test_audit_ignores_nonfinite_and_unexpected_response_data():
    audit = safe_margin_audit(unavailable_for_basket(legs()), {
        "data": {"ordMrgn": float("nan"), "reqdMrgn": "infinity", "elm": True,
                 "additional_margin": "SECRET", "unknown": "SECRET"},
    })
    assert audit["response_values"] == {}
    assert "SECRET" not in json.dumps(audit)


def test_arbitrary_inputs_cannot_inject_debug_source_or_fingerprint():
    result = unavailable_for_basket(legs())
    result["source"] = "PRIVATE_TOKEN"
    result["reason"] = "PRIVATE_TOKEN"
    assert "PRIVATE_TOKEN" not in json.dumps(safe_margin_audit(result))
    result["request_fingerprint"] = "PRIVATE_TOKEN"
    with pytest.raises(ValueError, match="fingerprint"):
        safe_margin_audit(result)


def test_credentials_are_not_part_of_basket_identity():
    original = legs()
    clean = unavailable_for_basket(original)
    original[0].update(access_token="PRIVATE_TOKEN", mpin="PRIVATE_MPIN", clientCode="PRIVATE_ACCOUNT")
    assert unavailable_for_basket(original)["request_fingerprint"] == clean["request_fingerprint"]


def test_iron_butterfly_remaining_option_premiums_are_separate_from_margin():
    rows = [
        {"broker": "kotak", "exchange": "MCX", "underlying": "NATURALGAS", "expiry": "2030-11-20",
         "option_type": kind, "side": side, "strike": strike, "entry_price": price,
         "lots": 1, "open_lots": 1, "lot_size": 1250, "price_multiplier": 1}
        for side, strike, kind, price in [
            ("BUY", 300, "PE", 2.00), ("SELL", 315, "PE", 7.00),
            ("SELL", 315, "CE", 5.00), ("BUY", 330, "CE", 2.00),
        ]
    ]
    result = unavailable_for_basket(rows)
    assert result["premium_paid"] == 5000
    assert result["premium_received"] == 15000
    assert result["net_premium"] == 10000
    assert result["provider_request_performed"] is False
    assert all(result[key] is None for key in MARGIN_FIELDS)
    # Partial exit and another expiry affect cash-flow totals, not margin availability.
    rows[0].update(lots=2, open_lots=1)
    rows[1].update(open_lots=0)
    rows[2].update(expiry="2030-12-18")
    rows[3].update(enabled=False)
    result = unavailable_for_basket(rows)
    assert result["premium_paid"] == 2500
    assert result["premium_received"] == 6250
    assert result["net_premium"] == 3750


def test_five_leg_gross_option_cash_flows_reconcile_to_net_debit():
    result = unavailable_for_basket(legs())
    assert result["premium_paid"] == 31817.50
    assert result["premium_received"] == 21554
    assert result["net_premium"] == -10263.50
    assert result["premium_received"] - result["premium_paid"] == result["net_premium"]


@pytest.mark.parametrize("unknown", [{"price_multiplier": None}, {"exchange": "MCX"}])
def test_unknown_option_monetary_conversion_preserves_unknown_cash_flows(unknown):
    original = legs()
    original[0].pop("price_multiplier", None)
    original[0].update(unknown)
    result = unavailable_for_basket(original)
    assert result["net_premium"] is None
    assert result["premium_received"] is None
    assert result["premium_paid"] == 31817.50
    assert result["total_margin"] is None


def test_futures_and_fully_closed_options_have_no_remaining_option_cash_flow():
    rows = legs()
    for row in rows:
        row["open_lots"] = 0
    rows.append({"side": "BUY", "option_type": "FUT", "price": 24000, "lot_size": 65, "lots": 1})
    result = unavailable_for_basket(rows)
    assert result["premium_paid"] == result["premium_received"] == result["net_premium"] == 0
    assert result["total_margin"] is None


def test_explicit_net_premium_override_does_not_override_gross_cash_flows():
    result = unavailable_for_basket(legs(), net_premium="-10263.505")
    assert result["net_premium"] == -10263.51
    assert result["premium_paid"] == 31817.50
    assert result["premium_received"] == 21554
