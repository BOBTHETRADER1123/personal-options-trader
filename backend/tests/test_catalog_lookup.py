"""Catalog lookup work stays bounded by selected contracts, not master size."""
import json
from datetime import date, timedelta

from app.instruments import ContractCatalog


def row(identifier="kotak:123", broker="kotak", expiry=None, **extra):
    return {"id":identifier,"broker":broker,"token":"123","symbol":"OFFLINE CONTRACT",
            "underlying":"NIFTY","exchange":"NSE","segment":"nse_fo" if broker=="kotak" else "D",
            "expiry":expiry or (date.today()+timedelta(days=30)).isoformat(),"strike":25000,
            "option_type":"CE","lot_size":65,"demo":False,**extra}


class CountedRows(list):
    def __init__(self, rows):super().__init__(rows);self.iterations=0
    def __iter__(self):self.iterations+=1;return super().__iter__()


def catalog(tmp_path, live=(), demo=()):
    result=ContractCatalog(tmp_path/"missing.json")
    result._live=CountedRows(live);result._demo=CountedRows(demo)
    return result


def test_find_preserves_first_matching_broker_and_unscoped_precedence(tmp_path):
    source=catalog(tmp_path,[row("shared",broker="fivepaisa",symbol="live-first"),
                             row("shared",broker="fivepaisa",symbol="live-second"),row("live")],
                            [row("shared",symbol="demo-first",demo=True),row("shared",symbol="demo-second",demo=True)])
    assert source.find("shared")["symbol"]=="demo-first"
    assert source.find("shared","kotak")["symbol"]=="demo-first"
    assert source.find("shared","fivepaisa")["symbol"]=="live-first"
    assert source.find("shared","unknown") is None and source.find("missing") is None


def test_find_returns_shallow_copies_and_keeps_expired_history(tmp_path):
    original=row(expiry="2020-01-01")
    source=catalog(tmp_path,[original])
    found=source.find(original["id"])
    assert found==original and found is not original
    found["symbol"]="modified-client-copy"
    assert source.find(original["id"])["symbol"]=="OFFLINE CONTRACT"


def test_repeated_tick_lookups_do_not_rescan_catalog(tmp_path):
    source=catalog(tmp_path,[row(f"kotak:{i}") for i in range(10_000)])
    assert source.find("kotak:9999")["id"]=="kotak:9999"
    before=(source._live.iterations,source._demo.iterations)
    for _ in range(1000):
        assert source.find("kotak:9999","kotak")["id"]=="kotak:9999"
        assert source.find("missing") is None
    assert (source._live.iterations,source._demo.iterations)==before


def test_revision_list_replacement_and_append_invalidate_lookup(tmp_path):
    source=catalog(tmp_path,[row("first")])
    assert source.find("first")
    source._live.append(row("append"))
    assert source.find("append")
    source._live=[row("replacement"),row("same-length")]
    assert source.find("replacement") and source.find("first") is None
    source._live[0]=row("revision-replacement");source.revision+=1
    assert source.find("revision-replacement") and source.find("replacement") is None
    source._demo.append(row("new-demo",demo=True))
    assert source.find("new-demo") and not source.select_live(["new-demo"])


def test_merge_and_disk_reload_replace_indexed_identity_without_mutating_copy(tmp_path):
    path=tmp_path/"master.json"
    path.write_text(json.dumps({"instruments":[row(symbol="old")]}),encoding="utf-8")
    source=ContractCatalog(path);old=source.find("kotak:123")
    source.merge_live_contracts([row(symbol="merged")])
    assert source.find("kotak:123")["symbol"]=="merged" and old["symbol"]=="old"
    path.write_text(json.dumps({"instruments":[row(symbol="reloaded")]}),encoding="utf-8")
    source.reload()
    assert source.find("kotak:123")["symbol"]=="reloaded"


def test_select_live_filters_expiry_and_broker_and_never_includes_demo(tmp_path):
    current=date.today().isoformat()
    source=catalog(tmp_path,[row("today",expiry=current),row("expired",expiry="2020-01-01"),
                             row("shared",broker="kotak"),row("shared",broker="fivepaisa")],
                            [row("shared",demo=True),row("demo-only",demo=True)])
    values=source.select_live(["today","expired","shared","demo-only","missing","shared"])
    assert [(r["id"],r["broker"]) for r in values]==[("shared","kotak"),("today","kotak")]
    assert source.select_live(["shared"],{"fivepaisa"})==[]
    assert source.select_live(["shared"],set())==[]
    values[0]["symbol"]="changed"
    assert source.select_live(["shared"])[0]["symbol"]=="OFFLINE CONTRACT"
    assert source.find("expired") is not None


def test_select_live_after_warmup_only_touches_requested_rows(tmp_path):
    source=catalog(tmp_path,[row(f"kotak:{i}") for i in range(10_000)])
    source.find("kotak:1")
    before=(source._live.iterations,source._demo.iterations)
    for _ in range(100):
        result=source.select_live(["kotak:1","kotak:9999","missing"])
        assert [r["id"] for r in result]==["kotak:1","kotak:9999"]
    assert (source._live.iterations,source._demo.iterations)==before
