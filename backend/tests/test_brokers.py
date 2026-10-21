"""Offline protocol/contract tests. These do not certify broker account access."""
import asyncio
import json
import struct
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.brokers import FeedManager
from app.brokers.base import FeedAuthenticationError, FeedProtocolError
from app.brokers.dhan import parse_frame, parse_packet
from app.brokers.fivepaisa import parse_message, tick_timestamp
from app.brokers.kotak import parse_sfeed
from app.instruments import ContractCatalog, demo_contracts, import_master, market_session, normalize_row


def contract(broker="dhan", token="123", segment=None):
    return {"id": f"{broker}:123", "broker": broker, "token": token, "underlying": "NIFTY", "exchange": "NSE",
            "segment": segment or {"dhan": "NSE_FNO", "kotak": "nse_fo", "fivepaisa": "D"}[broker],
            "symbol": "EXAMPLE OPTION", "expiry": "2030-10-29", "strike": 25000.0, "option_type": "CE", "lot_size": 50, "demo": False}


def test_dhan_ticker_uses_protocol_timestamp_and_received_precision():
    row = contract()
    packet = struct.pack("<BHBIfI", 2, 16, 2, 123, 105.5, 1792464000)
    result = parse_packet(packet, {("NSE_FNO", "123"): row}, received_at="2026-10-20T06:00:00.123456+00:00", received_ns=1000000001)
    assert result["ltp"] == 105.5
    assert result["broker"] == "dhan" and result["demo"] is False
    assert result["exchange_timestamp"] == datetime.fromtimestamp(1792464000, timezone.utc).isoformat(timespec="microseconds")
    assert result["received_ns"] == 1000000001
    assert result["raw_packet_hex"] == packet.hex()


def test_dhan_full_depth_volume_and_oi():
    row = contract()
    body = struct.pack("<fHIfIIIIIIffff", 100.5, 50, 1792464000, 99, 1500, 10, 20, 300, 400, 200, 95, 94, 110, 90)
    depth = struct.pack("<IIHHff", 5, 7, 1, 2, 100.0, 101.0) * 5
    packet = struct.pack("<BHBI", 8, 162, 2, 123) + body + depth
    result = parse_packet(packet, {("NSE_FNO", "123"): row})
    assert (result["bid"], result["ask"], result["volume"], result["open_interest"]) == (100, 101, 1500, 300)


def test_dhan_concatenated_events_are_not_coalesced():
    packet = struct.pack("<BHBIfI", 2, 16, 2, 123, 105.5, 1792464000)
    rows = parse_frame(packet + packet, {("NSE_FNO", "123"): contract()})
    assert len(rows) == 2


@pytest.mark.parametrize("packet", [b"", b"123", struct.pack("<BHBI", 2, 100, 2, 123)])
def test_dhan_invalid_packets_fail_visibly(packet):
    with pytest.raises(FeedProtocolError):
        parse_frame(packet, {("NSE_FNO", "123"): contract()}) if packet else parse_packet(packet, {})


def test_dhan_expired_token_is_authentication_error():
    with pytest.raises(FeedAuthenticationError, match="expired"):
        parse_packet(struct.pack("<BHBIH", 50, 10, 2, 123, 807), {})


def test_fivepaisa_preserves_actual_fields_and_null_missing_timestamp():
    mapping = {("N", "D", "123"): contract("fivepaisa")}
    data = [{"Exch": "N", "ExchType": "D", "Token": 123, "LastRate": 60.5, "BidRate": 60, "OffRate": 61, "TotalQty": 200,
             "Time": 35950, "TickDt": "/Date(-62135596800000)/"}]
    result = parse_message(json.dumps(data), mapping)[0]
    assert result["ltp"] == 60.5 and result["bid"] == 60 and result["ask"] == 61
    assert result["exchange_timestamp"] is None and result["exchange_time_raw"] == 35950
    assert result["raw_message"] == data[0]


def test_fivepaisa_dotnet_epoch_milliseconds():
    assert tick_timestamp("/Date(1792464000123)/") == "2026-10-20T02:40:00.123000+00:00"


def test_fivepaisa_duplicate_prices_retained_as_distinct_events():
    row = {"Exch": "N", "ExchType": "D", "Token": 123, "LastRate": 60.5}
    assert len(parse_message([row, row], {("N", "D", "123"): contract("fivepaisa")})) == 2


def test_fivepaisa_authentication_failure():
    with pytest.raises(FeedAuthenticationError):
        parse_message({"Message": "Token expired"}, {})


def test_kotak_sfeed_scaled_prices_not_divided_again():
    row = {"type": "scrip", "exchange_segment": "nse_fo", "instrument_token": "123", "last_traded_price": 123.45,
           "last_trade_time": 1792464000, "last_update_time": 1792464001, "volume_traded_today": 250,
           "open_interest": 600, "buy": [{"price": 123.4}], "sell": [{"price": 123.5}]}
    result = parse_sfeed(row, {("nse_fo", "123"): contract("kotak")})
    assert result["ltp"] == 123.45 and result["ask"] == 123.5
    assert result["raw_message"] == row


def test_kotak_market_status_not_invented_as_quote():
    assert parse_sfeed({"type": "market_status", "status_code": 1}, {}) is None


def test_demo_contracts_are_never_live_and_have_dynamic_expiry():
    rows = demo_contracts(date(2030, 1, 1))
    assert all(row["demo"] and row["token"] == "DEMO" and row["source"] == "synthetic_demo" for row in rows)
    assert {row["expiry"] for row in rows} == {"2030-01-08", "2030-01-15"}
    identifier = rows[0]["id"]
    assert {r["broker"] for r in rows if r["id"] == identifier} == {"dhan", "kotak", "fivepaisa"}
    assert {r["exchange"] for r in rows} == {"NSE", "BSE", "MCX"}


def test_live_catalog_empty_without_current_master(tmp_path):
    catalog = ContractCatalog(tmp_path / "missing.json")
    assert catalog.all(demo=False) == []
    assert catalog.all(demo=True)


def test_demo_contract_restored_after_startup_date_rollover(tmp_path):
    old_start = date.today()
    old = next(row for row in demo_contracts(old_start) if row["broker"] == "kotak")
    catalog = ContractCatalog(tmp_path / "missing.json")
    catalog._demo = demo_contracts(old_start + timedelta(days=1))
    assert catalog.find(old["id"], "kotak") is None
    trade = {"id": "persisted-demo-trade", "demo": True, "broker": "kotak", "spot": old["spot"],
             "legs": [{**old, "instrument_id": old["id"], "current_price": old["base_price"] + 1}]}
    assert catalog.restore_demo_contracts([trade]) == 1
    restored = catalog.find(old["id"], "kotak")
    assert restored["expiry"] == old["expiry"] and restored["base_price"] == old["base_price"]
    assert restored["ltp"] == old["base_price"] + 1 and restored["demo"] is True
    assert catalog.restore_demo_contracts([trade]) == 0
    assert catalog.all(demo=False) == []
    # A non-demo trade or a broker-token row cannot enter the recovery catalog.
    assert catalog.restore_demo_contracts([{**trade, "demo": False}, {**trade, "broker": "dhan", "legs": [{**trade["legs"][0], "token": "123"}]}]) == 0


def test_dhan_native_master_normalization():
    row = normalize_row("dhan", {"SECURITY_ID": "123", "EXCH_ID": "MCX", "DISPLAY_NAME": "GOLD 2030 CE", "UNDERLYING_SYMBOL": "GOLD",
                                  "SM_EXPIRY_DATE": "2030-10-29 00:00:00", "STRIKE_PRICE": "75000", "OPTION_TYPE": "CE", "LOT_SIZE": "100", "TICK_SIZE": "0.5"})
    assert row["exchange"] == "MCX" and row["segment"] == "MCX_COMM" and row["lot_size"] == 100
    assert row["expiry"] == "2030-10-29"


def test_kotak_documented_nse_epoch_offset_and_header_cleanup():
    actual_epoch = int(datetime(2030, 10, 29, 10, tzinfo=timezone.utc).timestamp())
    row = normalize_row("kotak", {"pSymbol": "123", "pExchSeg": "nse_fo", "pTrdSymbol": "NIFTY30OCT25000CE", "pSymbolName": "NIFTY",
                                  "lExpiryDate ": str(actual_epoch - 315511200), "dStrikePrice;": "25000", "pOptionType": "CE", "lLotSize": "50"})
    assert row["expiry"] == "2030-10-29" and row["strike"] == 25000


def test_kotak_documented_mcx_epoch_has_no_offset():
    epoch = int(datetime(2030, 10, 29, 10, tzinfo=timezone.utc).timestamp())
    row = normalize_row("kotak", {"pSymbol": "123", "pExchSeg": "mcx_fo", "pTrdSymbol": "GOLD CE", "pSymbolName": "GOLD",
                                  "lExpiryDate": str(epoch), "dStrikePrice": "75000", "pOptionType": "CE", "lLotSize": "100"})
    assert row["expiry"] == "2030-10-29"


def test_fivepaisa_native_master_normalization():
    row = normalize_row("fivepaisa", {"ScripCode": "123", "Exch": "M", "ExchType": "D", "Name": "GOLD CE", "SymbolRoot": "GOLD",
                                      "Expiry": "20301029", "StrikeRate": "75000", "ScripType": "CE", "LotSize": "100"})
    assert row["exchange"] == "MCX" and row["segment"] == "D" and row["expiry"] == "2030-10-29"
    assert row["price_multiplier"] is None and row["multiplier_verified"] is False


def test_verified_mcx_multiplier_preserved_without_assumed_mapping():
    data = {"ScripCode": "123", "Exch": "M", "ExchType": "D", "Name": "GOLD CE", "SymbolRoot": "GOLD",
            "Expiry": "20301029", "StrikeRate": "75000", "ScripType": "CE", "LotSize": "1", "Multiplier": "100"}
    row = normalize_row("fivepaisa", data)
    assert row["lot_size"] == 1 and row["price_multiplier"] == 100
    assert row["multiplier_verified"] is True
    with pytest.raises(ValueError):
        normalize_row("fivepaisa", {**data, "Multiplier": "0"})


def test_equity_multiplier_is_one_and_demo_mcx_explicitly_fictional():
    data = {"ScripCode": "123", "Exch": "N", "ExchType": "D", "Name": "NIFTY CE", "SymbolRoot": "NIFTY",
            "Expiry": "20301029", "StrikeRate": "25000", "ScripType": "CE", "LotSize": "50"}
    assert normalize_row("fivepaisa", data)["price_multiplier"] == 1
    assert all(r["price_multiplier"] == 1 and r["demo"] for r in demo_contracts() if r["exchange"] == "MCX")


def test_master_import_preserves_other_brokers_and_is_atomic(tmp_path):
    path = tmp_path / "master.json"
    path.write_text(json.dumps({"instruments": [contract("kotak")]}))
    text = "SECURITY_ID,EXCH_ID,DISPLAY_NAME,UNDERLYING_SYMBOL,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,LOT_SIZE\n123,NSE,NIFTY CE,NIFTY,2030-10-29,25000,CE,50\n"
    result = import_master("dhan", text, path)
    assert result["imported"] == 1
    assert {r["broker"] for r in json.loads(path.read_text())["instruments"]} == {"dhan", "kotak"}
    before = path.read_bytes()
    with pytest.raises(ValueError):
        import_master("dhan", text.replace(",50\n", ",0\n"), path)
    assert before == path.read_bytes()


def test_all_segment_master_ignores_currency_without_relabeling_as_index(tmp_path):
    text = "SECURITY_ID,EXCH_ID,SEGMENT,DISPLAY_NAME,UNDERLYING_SYMBOL,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,LOT_SIZE\n123,NSE,D,NIFTY CE,NIFTY,2030-10-29,25000,CE,50\n456,NSE,C,USDINR CE,USDINR,2030-10-29,85,CE,1000\n"
    path = tmp_path / "master.json"
    result = import_master("dhan", text, path)
    assert result["imported"] == 1 and result["ignored_non_options"] == 1
    assert json.loads(path.read_text())["instruments"][0]["underlying"] == "NIFTY"


def test_market_session_without_calendar_unknown(monkeypatch):
    monkeypatch.delenv("MARKET_CALENDAR_PATH", raising=False)
    assert market_session("MCX")["status"] == "unknown"


def test_mcx_split_holiday_and_evening_session(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"MCX:non_agri": {"2026-10-20": [["17:00", "23:30"]]}}))
    monkeypatch.setenv("MARKET_CALENDAR_PATH", str(path))
    assert market_session("MCX", datetime(2026, 10, 20, 5, tzinfo=timezone.utc))["status"] == "closed"
    assert market_session("MCX", datetime(2026, 10, 20, 15, tzinfo=timezone.utc))["status"] == "open"


def test_feed_manager_not_configured_does_not_connect(monkeypatch):
    for name in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "KOTAK_SID", "KOTAK_UCC", "KOTAK_CONSUMER_KEY", "FIVEPAISA_ACCESS_TOKEN", "FIVEPAISA_CLIENT_CODE"):
        monkeypatch.delenv(name, raising=False)
    received = []
    async def callback(quote):
        received.append(quote)
    async def run():
        manager = FeedManager(callback)
        await manager.start(demo_contracts())
        assert {s["status"] for s in manager.statuses().values()} == {"not_configured"}
        await manager.stop()
    asyncio.run(run())
    assert received == []


def test_adapter_sources_contain_no_live_order_methods():
    folder = Path(__file__).resolve().parents[1] / "app" / "brokers"
    forbidden = (".place_order(", ".modify_order(", ".cancel_order(", "/orders", "PlaceOrderRequest")
    for path in folder.glob("*.py"):
        assert all(value not in path.read_text(encoding="utf-8") for value in forbidden), path.name
