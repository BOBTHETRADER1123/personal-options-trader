"""Synthetic fixtures validate the safety boundary, not live exchange margin."""
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import urllib.error
import zipfile

import pytest
from app import exchange_margin as m

NOW = datetime(2026, 10, 30, 4, tzinfo=timezone.utc)
URL = "https://www.mcxccl.com/docs/default-source/market-operations/daily-span-risk-parameter-file/2026/october/mcxrpf-20261029-1500-05-i.zip"


def zipped(name="mcx.20261029.spn", payload=b"<spanFile><pointInTime>20261029</pointInTime></spanFile>"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, payload)
    return out.getvalue()


def inspect(data):
    return m.inspect_risk_file(data, exchange="MCX", source_url=URL,
                               expected_date=date(2026, 10, 29), downloaded_at=NOW)


def row(token="naturalgas-300pe", quantity=-1):
    return {"exchange": "MCX", "combined_commodity": "NATURALGAS", "file_version": "fixture-only",
            "effective_at": NOW.isoformat(), "units": "INR_per_contract", "contract_id": token,
            "signed_contracts": quantity, "risk_array": [str(Decimal(i)/10) for i in range(16)]}


@pytest.mark.parametrize("url", [
    "http://www.mcxccl.com/file.zip", "https://evil.example/file.zip",
    "https://www.mcxccl.com.evil.example/file.zip", "https://u:p@www.mcxccl.com/file.zip",
    "https://www.mcxccl.com:444/file.zip", "https://www.mcxccl.com/file.zip?token=secret",
    "https://www.mcxccl.com/file.zip#fragment", "https://127.0.0.1/file.zip",
    "file:///tmp/file.zip", "https://www.mcxccl.com/file.exe",
])
def test_reject_untrusted_download_url(url):
    with pytest.raises(ValueError):
        m.validate_source_url("MCX", url)


def test_exchange_hosts_are_not_interchangeable():
    with pytest.raises(ValueError):
        m.validate_source_url("NSE", URL)


def test_valid_archive_remains_quarantined_not_verified_span():
    result = inspect(zipped())
    assert result["transport_validated"] is True
    assert result["model_validated"] is False
    assert result["status"] == "quarantined"
    assert result["risk_parameter_version_verified"] is False
    assert result["effective_at"] is None
    assert result["freshness"] == "unverified"
    assert len(result["sha256"]) == 64
    assert result["members"][0]["root_element"] == "spanFile"
    assert result["downloaded_at_ist"].endswith("+05:30")


@pytest.mark.parametrize("name", ["../escape.spn", "/absolute.spn", "C:/escape.spn", "folder\\..\\escape.spn", "runnable.exe"])
def test_archive_rejects_unsafe_entries(name):
    with pytest.raises(ValueError):
        inspect(zipped(name))


def test_archive_rejects_duplicate_entries():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("same.spn", b"x")
        with pytest.warns(UserWarning):
            z.writestr("same.spn", b"y")
    with pytest.raises(ValueError):
        inspect(out.getvalue())


@pytest.mark.parametrize("payload", [b"<!DOCTYPE html><html>Access denied</html>", b"<!DOCTYPE spanFile [<!ENTITY x SYSTEM 'file:///secret'>]><spanFile>&x;</spanFile>", b"<spanFile>broken"])
def test_reject_error_or_unsafe_xml(payload):
    with pytest.raises(ValueError):
        inspect(zipped(payload=payload))


def test_future_and_wrong_filename_dates_rejected():
    with pytest.raises(ValueError):
        m.inspect_risk_file(zipped(), exchange="MCX", source_url=URL,
                            expected_date=date(2026, 10, 31), downloaded_at=NOW)
    with pytest.raises(ValueError):
        m.inspect_risk_file(zipped(), exchange="MCX", source_url=URL,
                            expected_date=date(2026, 10, 28), downloaded_at=NOW)


def test_download_and_expansion_bounds(monkeypatch):
    monkeypatch.setattr(m, "MAX_DOWNLOAD_BYTES", 10)
    with pytest.raises(ValueError):
        inspect(b"1" * 11)
    monkeypatch.setattr(m, "MAX_DOWNLOAD_BYTES", 10000)
    monkeypatch.setattr(m, "MAX_EXPANDED_BYTES", 30)
    with pytest.raises(ValueError):
        inspect(zipped(payload=b"x" * 500))


def test_http_failure_does_not_return_private_error_body(monkeypatch):
    class Denied:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError(URL, 403, "private-diagnostics-secret", {}, None)
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *args: Denied())
    result = m.download_risk_file("MCX", URL, date(2026, 10, 29), now=NOW)
    assert result["status"] == "unavailable"
    assert result["reason"] == "Official exchange download returned HTTP 403."
    assert "secret" not in str(result)


def test_timeout_returns_safe_unavailable_status(monkeypatch):
    class Slow:
        def open(self, *args, **kwargs):
            raise TimeoutError("secret-host-details")
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *args: Slow())
    result = m.download_risk_file("MCX", URL, date(2026, 10, 29), now=NOW)
    assert result["status"] == "unavailable"
    assert "secret" not in str(result)


def test_complete_portfolio_scanning_offsets_decimal_arrays():
    short = row(quantity=-2)
    long = row("naturalgas-290pe", 2)
    long["risk_array"] = ["0"] * 15 + ["0.10"]
    result = m.portfolio_scan_diagnostic([short, long])
    # Total at scenario 15 = -2*1.5 + 2*0.1; all nonpositive here.
    assert Decimal(result["scenario_losses"][-1]) == Decimal("-2.80")
    assert Decimal(result["scanning_risk"]) == 0
    assert result["span_margin"] is None
    assert result["total_margin"] is None
    assert result["hedge_benefit"] is None
    assert result["verified"] is False
    assert result["status"] == "diagnostic_only"


def test_scanning_loss_does_not_round_financial_inputs():
    position = row(quantity=3)
    position["risk_array"][-1] = "12.123456789"
    result = m.portfolio_scan_diagnostic([position])
    assert Decimal(result["scanning_risk"]) == Decimal("36.370370367")


@pytest.mark.parametrize("field,value", [
    ("file_version", "other"), ("combined_commodity", "CRUDEOIL"),
    ("exchange", "NSE"), ("effective_at", "2026-10-29T04:00:00+00:00"),
])
def test_never_offset_unaligned_risk_scenarios(field, value):
    other = row("second", 1)
    other[field] = value
    with pytest.raises(ValueError):
        m.portfolio_scan_diagnostic([row(), other])


@pytest.mark.parametrize("field,value", [
    ("units", "INR_per_unit"), ("signed_contracts", "1.5"),
    ("signed_contracts", True), ("risk_array", ["1"] * 15),
    ("risk_array", ["NaN"] * 16), ("effective_at", "2026-10-30T04:00:00"),
])
def test_reject_unverified_scaling_quantity_and_timestamp(field, value):
    position = row()
    position[field] = value
    with pytest.raises(ValueError):
        m.portfolio_scan_diagnostic([position])


def test_public_exchange_margin_stays_unavailable_even_with_untrusted_verified_flag():
    result = m.exchange_margin_status(
        [{"exchange": "MCX", "open_lots": 1}, {"exchange": "NSE", "open_lots": 1}],
        risk_files={"MCX": {"file_version": "fixture-only", "model_validated": True}}, now=NOW)
    assert result["total_exchange_margin"] is None
    assert result["span_margin"] is None
    assert result["hedge_benefit"] is None
    assert result["verified"] is False
    assert len(result["sources"]) == 2
    assert all(item["model_validated"] is False for item in result["sources"])
    assert "devolvement" in str(result["missing_components"])
    assert result["as_of_ist"].endswith("+05:30")


def test_closed_disabled_and_partially_exited_legs_have_correct_source_coverage():
    result = m.exchange_margin_status([
        {"exchange": "MCX", "open_lots": 0, "lots": 3},
        {"exchange": "BSE", "enabled": False},
        {"exchange": "NSE", "open_lots": 1, "lots": 2}], now=NOW)
    assert [r["exchange"] for r in result["sources"]] == ["NSE"]


def test_internal_point_in_time_must_match_requested_filename_date():
    with pytest.raises(ValueError, match="point-in-time"):
        inspect(zipped(payload=b"<spanFile><pointInTime><date>20261028</date></pointInTime></spanFile>"))
    good = inspect(zipped(payload=b"<spanFile><pointInTime><date>2026-10-29</date></pointInTime></spanFile>"))
    assert good["internal_date_matches_expected"] is True
    assert good["model_validated"] is False


def test_contract_expiries_are_not_mistaken_for_risk_file_dates():
    good = inspect(zipped(payload=b"<spanFile><pointInTime>20261029</pointInTime><contract><date>20261120</date></contract></spanFile>"))
    assert good["internal_date_matches_expected"] is True


def test_missing_exchange_identity_returns_unavailable_not_guessed_exchange():
    result = m.exchange_margin_status([{"underlying": "NIFTY", "lots": 1}], now=NOW)
    assert result["status"] == "unavailable"
    assert "identity is missing" in result["reason"]
    assert result["sources"] == []
    assert result["total_margin"] is None
