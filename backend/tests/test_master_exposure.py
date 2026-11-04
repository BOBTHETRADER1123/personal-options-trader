"""Offline contract-provenance and native-schema regressions, never live orders."""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import engine
from app.database import Base, configure_sqlite, iso_now
from app.instruments import ContractCatalog, contract_master_status, normalize_row
from app.models import Event, Trade, Timeline

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)


def live_contract(**overrides):
    moment = datetime.now(timezone.utc)
    return {"id": "kotak:nse_fo:123", "instrument_id": "kotak:nse_fo:123",
            "broker": "kotak", "token": "123", "symbol": "OFFLINE TEST CE", "underlying": "NIFTY",
            "exchange": "NSE", "segment": "nse_fo", "expiry": (moment + timedelta(days=30)).date().isoformat(),
            "strike": 23000.0, "option_type": "CE", "lot_size": 65, "price_multiplier": 1.0,
            "tick_size": .05, "multiplier_verified": True, "demo": False, "source": "broker_master",
            "master_active": True, "master_refresh_at": moment.isoformat(),
            "master_source_date": moment.astimezone(IST).date().isoformat(), **overrides}


def native_row(segment="nse_fo", **overrides):
    return {"pSymbol": "123", "pExchSeg": segment, "pTrdSymbol": "OFFLINE TEST CE",
            "pSymbolName": "NIFTY", "pOptionType": "CE", "lLotSize": "65",
            "dStrikePrice": "2300000", "lPrecision": "2", "dTickSize": "5", **overrides}


@pytest.mark.parametrize("column", ["pExpiryDate", "lExpiryDate"])
@pytest.mark.parametrize("segment", ["nse_fo", "bse_fo", "mcx_fo"])
@pytest.mark.parametrize("decimal_suffix", ["", ".0"])
def test_current_and_legacy_expiry_fields_keep_distinct_official_epochs(column, segment, decimal_suffix):
    # BSE/MCX end-of-day UTC remains the published business date, not tomorrow in IST.
    moment = datetime(2026, 11, 3, 23, 59, tzinfo=timezone.utc)
    legacy_shift = column == "lExpiryDate" and segment == "nse_fo"
    seconds = int(moment.timestamp()) - (315511200 if legacy_shift else 0)
    result = normalize_row("kotak", native_row(segment, **{column: str(seconds) + decimal_suffix}))
    expected = moment.astimezone(IST).date().isoformat() if legacy_shift else "2026-11-03"
    assert result["expiry"] == expected
    assert result["strike"] == 23000


@pytest.mark.parametrize("value", ["-1", "0", "1.25", "nan", "inf", "999999999999999999999999999"])
def test_native_expiry_rejects_invalid_or_out_of_bounds_seconds(value):
    with pytest.raises((ValueError, OverflowError)):
        normalize_row("kotak", native_row("bse_fo", pExpiryDate=value))



def test_current_p_expiry_column_takes_precedence_over_legacy_alias():
    seconds = str(int(datetime(2026, 11, 3, 10, tzinfo=timezone.utc).timestamp()))
    row = normalize_row("kotak", native_row(pExpiryDate=seconds, lExpiryDate="invalid"))
    assert row["expiry"] == "2026-11-03"


def test_explicit_normalized_expiry_preserves_date_over_native_epoch():
    row = normalize_row("kotak", native_row(pExpiryDate="bad", expiry="2026-11-03"))
    assert row["expiry"] == "2026-11-03"


@pytest.mark.parametrize("metadata", [
    {}, {"master_active": False},
    {"master_refresh_at": "2026-11-04T09:00:00"},
    {"master_refresh_at": "2026-11-03T09:59:59+00:00"},
    {"master_refresh_at": "2026-11-04T10:00:06+00:00"},
    {"master_source_date": "2026-11-03"}, {"master_source_date": "2026-11-05"},
    {"master_source_date": "invalid"}, {"master_refresh_at": None},
])
def test_new_exposure_fails_closed_without_current_validated_provenance(metadata):
    row = {"master_active": True, "master_refresh_at": NOW.isoformat(), "master_source_date": "2026-11-04"}
    if metadata:
        row.update(metadata)
    else:
        row = {}
    assert contract_master_status(row, NOW)["ready"] is False


def test_current_master_source_date_follows_ist_not_utc():
    moment = datetime(2026, 11, 3, 19, tzinfo=timezone.utc)
    row = {"master_active": True, "master_refresh_at": moment.isoformat(), "master_source_date": "2026-11-04"}
    assert contract_master_status(row, moment) == {"ready": True, "reason": None}
    assert not contract_master_status({**row, "master_source_date": "2026-11-03"}, moment)["ready"]


@pytest.fixture
def journal(tmp_path, monkeypatch):
    sql = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    configure_sqlite(sql)
    Base.metadata.create_all(sql)
    current = ContractCatalog(tmp_path / "missing.json")
    current.merge_live_contracts([live_contract()])
    monkeypatch.setattr(engine, "catalog", current)
    with sessionmaker(sql, expire_on_commit=False)() as db:
        engine.update_settings(db, {"demo_mode": False, "brokerage_per_order": 0, "charges_percent": 0,
                                    "fill_basis": "ltp", "logging_mode": "every_update"})
        db.commit()
        yield db, current
    engine.clear_cache()
    sql.dispose()


def quote(db, price=100):
    return engine.record_quote(db, {"broker": "kotak", "instrument_id": "kotak:nse_fo:123",
                                   "ltp": price, "received_at": iso_now(), "demo": False})


def enter(db, order_type="MARKET"):
    quote(db)
    result = engine.create_trades(db, {"name": "Offline provenance test", "underlying": "NIFTY",
        "brokers": ["kotak"], "confirmed": True, "idempotency_key": uuid.uuid4().hex,
        "legs": [{"instrument_id": "kotak:nse_fo:123", "side": "BUY", "lots": 2,
                  "order_type": order_type, "price": 90 if order_type == "LIMIT" else None, "enabled": True}]})
    db.commit()
    return result


def act(db, trade, action, **kwargs):
    result = engine.apply_action(db, trade["id"], {"action": action, "confirmed": True,
                                "idempotency_key": uuid.uuid4().hex, **kwargs})
    db.commit()
    return result


def test_fresh_master_allows_paper_entry_but_stale_master_blocks_new_entry(journal):
    db, current = journal
    assert enter(db)["results"][0]["success"] is True
    current.merge_live_contracts([live_contract(master_active=False)])
    blocked = enter(db)
    assert blocked["trades"] == []
    assert "master unavailable or stale" in blocked["results"][0]["error"]
    assert db.scalar(select(func.count()).select_from(Trade)) == 1


def test_stale_master_blocks_added_lots_but_preserves_marking_partial_exit_and_history(journal):
    db, current = journal
    trade = enter(db)["trades"][0]
    leg = trade["legs"][0]
    count = db.scalar(select(func.count()).select_from(Timeline))
    current.merge_live_contracts([live_contract(master_active=False)])
    quote(db, 105)
    db.commit()
    stored = db.get(Trade, trade["id"]).data
    assert stored["legs"][0]["current_price"] == 105
    with pytest.raises(HTTPException, match="master unavailable or stale"):
        act(db, trade, "add_lots", leg_id=leg["id"], lots=1, price=105)
    # A stale master does not trap the already recorded position.
    exited = act(db, trade, "partial_exit", leg_id=leg["id"], lots=1)["trade"]
    assert exited["legs"][0]["open_lots"] == 1
    assert exited["realized_pnl"] == 325
    assert db.scalar(select(func.count()).select_from(Timeline)) > count


def test_pending_limit_waits_without_loss_or_duplicate_events_then_resumes(journal):
    db, current = journal
    trade = enter(db, "LIMIT")["trades"][0]
    current.merge_live_contracts([live_contract(master_active=False)])
    quote(db, 89)
    quote(db, 88)
    db.commit()
    stored = db.get(Trade, trade["id"]).data
    assert stored["status"] == "pending"
    assert stored["legs"][0]["filled_lots"] == 0
    assert "master unavailable or stale" in stored["legs"][0]["master_blocked"]
    assert db.scalar(select(func.count()).select_from(Event).where(Event.kind == "limit_waiting_for_current_master")) == 1
    current.merge_live_contracts([live_contract()])
    quote(db, 87)
    db.commit()
    stored = db.get(Trade, trade["id"]).data
    assert stored["status"] == "open" and stored["legs"][0]["entry_price"] == 87
    assert "master_blocked" not in stored["legs"][0]
    assert db.scalar(select(func.count()).select_from(Event).where(Event.kind == "limit_filled")) == 1


def test_current_catalog_provenance_overrides_historical_saved_metadata(journal):
    db, current = journal
    trade = enter(db)["trades"][0]
    old = copy.deepcopy(trade["legs"][0])
    old.update(master_active=False, master_refresh_at="2000-01-01T00:00:00+00:00")
    engine.validate_new_exposure(old, engine.get_settings(db))
    current.merge_live_contracts([live_contract(strike=24000)])
    with pytest.raises(HTTPException, match="master unavailable or stale"):
        engine.validate_new_exposure(old, engine.get_settings(db))



@pytest.mark.parametrize("changed_units", [{"lot_size": 130}, {"price_multiplier": 2.0}])
def test_fresh_master_cannot_increase_a_saved_leg_with_changed_units(journal, changed_units):
    db, current = journal
    trade = enter(db)["trades"][0]
    current.merge_live_contracts([live_contract(**changed_units)])
    with pytest.raises(HTTPException, match="contract units changed"):
        act(db, trade, "add_lots", leg_id=trade["legs"][0]["id"], lots=1, price=100)
    assert db.get(Trade, trade["id"]).data["legs"][0]["open_lots"] == 2


def test_pending_expired_leg_keeps_order_and_reports_master_block_without_valuation_failure(journal):
    db, current = journal
    trade = enter(db, "LIMIT")["trades"][0]
    stored = copy.deepcopy(db.get(Trade, trade["id"]).data)
    stored["legs"][0]["expiry"] = "2020-01-01"
    engine.store_trade(db, stored)
    db.commit()
    quote(db, 89)
    db.commit()
    stored = db.get(Trade, trade["id"]).data
    assert stored["status"] == "pending"
    assert "expired" in stored["legs"][0]["master_blocked"]
    assert "valuation_error" not in stored


def test_practice_mode_has_no_dependency_on_a_live_master():
    engine.validate_new_exposure({}, {"demo_mode": True})


def test_expired_master_cannot_be_used_even_with_fresh_provenance(journal):
    db, current = journal
    row = live_contract(expiry="2020-01-01")
    current.merge_live_contracts([row])
    with pytest.raises(HTTPException, match="expired"):
        engine.validate_new_exposure(row, engine.get_settings(db))


def test_restored_positions_cannot_overwrite_fresh_catalog_or_certify_new_exposure(tmp_path):
    current = ContractCatalog(tmp_path / "missing.json")
    fresh = live_contract()
    current.merge_live_contracts([fresh])
    saved = {**fresh, "instrument_id": fresh["id"], "status": "open", "master_active": True,
             "master_refresh_at": "2000-01-01T00:00:00+00:00"}
    trade = {"broker": "kotak", "demo": False, "status": "open", "legs": [saved]}
    assert current.restore_live_contracts([trade]) == 0
    assert current.find(fresh["id"])["master_refresh_at"] == fresh["master_refresh_at"]
    restored = ContractCatalog(tmp_path / "another-missing.json")
    saved.pop("broker")  # Original historical leg shape did not save broker on each leg.
    assert restored.restore_live_contracts([trade]) == 1
    row = restored.find(fresh["id"])
    assert row["broker"] == "kotak" and row["master_active"] is False
    assert row["restored_from_trade"] is True
    assert not contract_master_status(row)["ready"]


def test_reload_publishes_master_metadata_only_after_all_rows_validate(tmp_path):
    path = tmp_path / "master.json"
    initial = {"last_successful_refresh_at": "test"}
    path.write_text(json.dumps({"instruments": [live_contract()], "master_refresh": initial}))
    current = ContractCatalog(path)
    assert current.master_metadata == initial
    before = current.find("kotak:nse_fo:123")
    path.write_text(json.dumps({"instruments": [live_contract(lot_size=0)], "master_refresh": {"changed": True}}))
    current.reload()
    assert current.error is not None
    assert current.master_metadata == initial
    assert current.find("kotak:nse_fo:123") == before
