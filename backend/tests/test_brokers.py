"""Offline protocol/contract tests. These do not certify broker account access."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.brokers import FeedManager
from app.brokers.base import FeedAuthenticationError, FeedProtocolError
from app.brokers.fivepaisa import parse_message, tick_timestamp
from app.brokers.kotak import parse_sfeed
from app.instruments import ContractCatalog, demo_contracts, import_master, market_session, normalize_row


def contract(broker="kotak", token="123", segment=None):
    return {"id": f"{broker}:123", "broker": broker, "token": token, "underlying": "NIFTY", "exchange": "NSE",
            "segment": segment or {"kotak": "nse_fo", "fivepaisa": "D"}[broker],
            "symbol": "EXAMPLE OPTION", "expiry": "2030-10-29", "strike": 25000.0, "option_type": "CE", "lot_size": 50, "demo": False}


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
    assert {r["broker"] for r in rows if r["id"] == identifier} == {"kotak"}
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


def test_quantity_multiplier_does_not_authorize_unverified_mcx_value():
    data = {"ScripCode": "123", "Exch": "M", "ExchType": "D", "Name": "GOLD CE", "SymbolRoot": "GOLD",
            "Expiry": "20301029", "StrikeRate": "75000", "ScripType": "CE", "LotSize": "1", "Multiplier": "100"}
    row = normalize_row("fivepaisa", data)
    assert row["lot_size"] == 1 and row["price_multiplier"] is None
    assert row["multiplier_verified"] is False
    explicit = normalize_row("fivepaisa", {**data, "price_multiplier": "100"})
    assert explicit["price_multiplier"] == 100 and explicit["monetary_conversion_source"]
    with pytest.raises(ValueError):
        normalize_row("fivepaisa", {**data, "price_multiplier": "0"})


def test_equity_multiplier_is_one_and_demo_mcx_explicitly_fictional():
    data = {"ScripCode": "123", "Exch": "N", "ExchType": "D", "Name": "NIFTY CE", "SymbolRoot": "NIFTY",
            "Expiry": "20301029", "StrikeRate": "25000", "ScripType": "CE", "LotSize": "50"}
    assert normalize_row("fivepaisa", data)["price_multiplier"] == 1
    assert all(r["price_multiplier"] == 1 and r["demo"] for r in demo_contracts() if r["exchange"] == "MCX")


def test_kotak_master_import_preserves_retired_rows_and_is_atomic(tmp_path):
    path = tmp_path / "master.json"
    retired=contract("fivepaisa")
    path.write_text(json.dumps({"instruments": [retired]}))
    text="pSymbol,pExchSeg,pSymbolName,pTrdSymbol,pOptionType,lLotSize,lExpiryDate,dStrikePrice,dTickSize,lPrecision\n123,nse_fo,NIFTY,NIFTY TEST CE,CE,50,2030-10-29,2500000,5,2\n"
    result=import_master("kotak",text,path)
    assert result["imported"]==1
    rows=json.loads(path.read_text())["instruments"]
    assert next(row for row in rows if row["broker"]=="fivepaisa")==retired
    assert {row["broker"] for row in rows}=={"kotak","fivepaisa"}
    assert {row["broker"] for row in ContractCatalog(path).all(False)}=={"kotak"}
    before=path.read_bytes()
    with pytest.raises(ValueError):import_master("kotak",text.replace(",50,",",0,"),path)
    assert path.read_bytes()==before
    with pytest.raises(ValueError,match="Only Kotak Neo"):import_master("fivepaisa",text,path)
    assert path.read_bytes()==before


def test_retired_all_segment_master_cannot_be_imported_and_currency_is_not_relabelled(tmp_path):
    currency={"ScripCode":"456","Exch":"N","ExchType":"U","Name":"USDINR CE","SymbolRoot":"USDINR",
              "Expiry":"2030-10-29","StrikeRate":"85","ScripType":"CE","LotSize":"1000"}
    with pytest.raises(ValueError,match="Unsupported derivative segment"):normalize_row("fivepaisa",currency)
    path=tmp_path/"master.json"
    with pytest.raises(ValueError,match="Only Kotak Neo"):import_master("fivepaisa","",path)
    assert not path.exists()


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


def test_only_kotak_runtime_adapter_even_with_legacy_environment(monkeypatch):
    monkeypatch.setenv('DHAN_CLIENT_ID','historical-only')
    monkeypatch.setenv('DHAN_ACCESS_TOKEN','must-not-create-an-adapter')
    monkeypatch.setenv('FIVEPAISA_CLIENT_CODE','historical-only')
    monkeypatch.setenv('FIVEPAISA_ACCESS_TOKEN','must-not-create-an-adapter')
    manager=FeedManager(None)
    assert set(manager.statuses())=={'kotak'}
    assert {adapter.broker for adapter in manager._adapters}=={'kotak'}


def test_legacy_master_does_not_block_current_broker_and_cannot_be_imported(tmp_path):
    path=tmp_path/'instruments.json'
    path.write_text(json.dumps({'instruments':[{'broker':'dhan'},contract('kotak')]}))
    catalog=ContractCatalog(path)
    assert catalog.error is None
    assert {c['broker'] for c in catalog.all(False)}=={'kotak'}
    with pytest.raises(ValueError,match='Only Kotak'):
        import_master('dhan','',path)


def test_fivepaisa_tokens_are_scoped_to_exchange():
    data={'ScripCode':'123','Exch':'N','ExchType':'D','Name':'EXAMPLE','SymbolRoot':'NIFTY','Expiry':'20301029','StrikeRate':'25000','ScripType':'CE','LotSize':'50'}
    assert normalize_row('fivepaisa',data)['id']!=normalize_row('fivepaisa',{**data,'Exch':'B'})['id']


@pytest.mark.parametrize('broker,raw',[
    ('fivepaisa',{'ScripCode':'123','Exch':'N','ExchType':'D','Name':'NIFTY FUT','SymbolRoot':'NIFTY','Expiry':'20301029','StrikeRate':'0','ScripType':'XX','LotSize':'50'}),
    ('kotak',{'pSymbol':'123','pExchSeg':'nse_fo','pTrdSymbol':'NIFTY FUT','pSymbolName':'NIFTY','expiry':'2030-10-29','dStrikePrice':'0','pOptionType':'XX','lLotSize':'50'}),
])
def test_native_future_master_not_mislabelled_as_call(broker,raw):
    result=normalize_row(broker,raw)
    assert result['option_type']=='FUT' and result['instrument_type']=='future' and result['strike']==0


def test_fivepaisa_oi_updates_do_not_fabricate_new_price_timestamp():
    mapping={('N','D','123'):contract('fivepaisa')};oi={}
    received='2026-10-24T01:00:00+00:00'
    assert parse_message({'Exch':'N','ExchType':'D','Token':123,'OpenInterest':1000},mapping,received_at=received,open_interest=oi)==[]
    quote=parse_message({'Exch':'N','ExchType':'D','Token':123,'LastRate':100},mapping,received_at='2026-10-24T01:00:01+00:00',open_interest=oi)[0]
    assert quote['open_interest']==1000 and quote['open_interest_received_at']==received
    assert quote['received_at']=='2026-10-24T01:00:01+00:00'


def test_fivepaisa_stream_subscribes_and_unsubscribes_read_only_channels(monkeypatch):
    from app.brokers.fivepaisa import FivepaisaFeed
    import websockets.asyncio.client
    monkeypatch.setenv('FIVEPAISA_CLIENT_CODE','test-client');monkeypatch.setenv('FIVEPAISA_ACCESS_TOKEN','not-real-test-token')
    class Socket:
        def __init__(self):self.sent=[];self.values=iter([json.dumps([{'Exch':'N','ExchType':'D','Token':123,'LastRate':101}])])
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def send(self,value):self.sent.append(json.loads(value))
        def __aiter__(self):return self
        async def __anext__(self):
            try:return next(self.values)
            except StopIteration:raise StopAsyncIteration
    socket=Socket()
    monkeypatch.setattr(websockets.asyncio.client,'connect',lambda *args,**kwargs:socket)
    received=[]
    async def quote(value):received.append(value)
    async def status(*args):pass
    asyncio.run(FivepaisaFeed(quote,status).stream([contract('fivepaisa')]))
    assert len(received)==1 and received[0]['broker']=='fivepaisa'
    assert [(m['Method'],m['Operation']) for m in socket.sent]==[('MarketFeedV3','Subscribe'),('GetScripInfoForFuture','Subscribe'),('MarketFeedV3','Unsubscribe'),('GetScripInfoForFuture','Unsubscribe')]


def test_feed_manager_retries_transport_but_stops_expired_session(monkeypatch):
    import app.brokers as module
    delays=[]
    async def pause(delay):delays.append(delay)
    monkeypatch.setattr(module.asyncio,'sleep',pause);monkeypatch.setattr(module.random,'uniform',lambda *a:0)
    class Adapter:
        broker='kotak'
        calls=0
        async def stream(self,contracts):
            self.calls+=1
            if self.calls==1:raise ConnectionError('private URL must never be shown')
            raise FeedAuthenticationError('Renew the daily session')
    async def run():
        manager=FeedManager(None);adapter=Adapter()
        await manager._run(adapter,[])
        state=manager.statuses()['kotak']
        assert state['status']=='authentication_required' and state['reconnect_count']==1 and adapter.calls==2
        assert 'private URL' not in json.dumps(state)
    asyncio.run(run())
    assert delays==[2]


def test_kotak_uses_sfeed_current_sdk_and_sanitizes_auth(monkeypatch):
    import sys
    from types import ModuleType
    from app.brokers.kotak import KotakFeed
    class AuthenticationError(Exception):pass
    class SubscriptionError(Exception):pass
    instances=[]
    class Socket:
        fail=False
        def __init__(self,**kwargs):self.kwargs=kwargs;self.calls=[];self.values=iter([{'type':'scrip','exchange_segment':'nse_fo','instrument_token':'123','last_traded_price':99}]);instances.append(self)
        async def __aenter__(self):
            if self.fail:raise AuthenticationError('PRIVATE SID must not escape')
            return self
        async def __aexit__(self,*args):pass
        async def subscribe_scrips(self,tokens):self.calls.append(('subscribe',tokens))
        async def unsubscribe_scrips(self,tokens):self.calls.append(('unsubscribe',tokens))
        def __aiter__(self):return self
        async def __anext__(self):
            try:return next(self.values)
            except StopIteration:raise StopAsyncIteration
    modules={name:ModuleType(name) for name in ['neo_api_client','neo_api_client.logger','neo_api_client.websocket','neo_api_client.websocket.feed','neo_api_client.websocket.feed.exceptions']}
    modules['neo_api_client'].NeoAPI=object
    modules['neo_api_client.logger'].setup_logging=lambda **kwargs:None
    modules['neo_api_client.websocket.feed'].SFeedWebSocket=Socket
    modules['neo_api_client.websocket.feed'].WsToken=lambda segment,token:(segment,token)
    modules['neo_api_client.websocket.feed.exceptions'].AuthenticationError=AuthenticationError
    modules['neo_api_client.websocket.feed.exceptions'].SubscriptionError=SubscriptionError
    for name,module in modules.items():monkeypatch.setitem(sys.modules,name,module)
    monkeypatch.setenv('KOTAK_UCC','mock-user');monkeypatch.setenv('KOTAK_SID','mock-sid')
    received=[]
    async def quote(value):received.append(value)
    async def status(*args):pass
    asyncio.run(KotakFeed(quote,status).stream([contract('kotak')]))
    assert received[0]['broker']=='kotak'
    assert instances[0].kwargs['ack_wait_timeout']==0
    assert instances[0].kwargs['max_reconnect_attempts']==0
    assert [v[0] for v in instances[0].calls]==['subscribe','unsubscribe']
    Socket.fail=True
    with pytest.raises(FeedAuthenticationError) as error:asyncio.run(KotakFeed(quote,status).stream([contract('kotak')]))
    assert 'PRIVATE SID' not in str(error.value)


def test_fivepaisa_sdk_routing_hint_uses_only_official_allowlisted_hosts():
    import base64
    from app.brokers.fivepaisa import feed_url
    def token(value):return 'header.'+base64.urlsafe_b64encode(json.dumps({'RedirectServer':value}).encode()).decode().rstrip('=')+'.signature'
    assert feed_url(token('A'),'123').startswith('wss://aopenfeed.5paisa.com/')
    assert feed_url(token('B'),'123').startswith('wss://bopenfeed.5paisa.com/')
    assert feed_url(token('https://evil.invalid'),'123').startswith('wss://openfeed.5paisa.com/')
    assert feed_url('malformed-token','123').startswith('wss://openfeed.5paisa.com/')


def test_feed_manager_updates_subscriptions_without_restarting_active_socket():
    async def run():
        class Adapter:
            broker="kotak"
            def __init__(self):self.started=asyncio.Event();self.calls=0;self.stops=0;self.changes=[]
            def configured(self):return True
            async def stream(self,contracts):
                self.calls+=1;self.contracts=contracts;self.started.set()
                try:await asyncio.Event().wait()
                finally:self.stops+=1
            async def reconfigure(self,contracts):self.changes.append(list(contracts))
        async def quote(value):pass
        manager=FeedManager(quote);adapter=Adapter();manager._adapters=[adapter]
        first=contract(token="123");second=contract(token="456")
        await manager.start([first]);await asyncio.wait_for(adapter.started.wait(),1)
        original=manager._tasks[0]
        await manager.reconfigure([first,second])
        assert manager._tasks[0] is original and adapter.calls==1 and adapter.stops==0
        assert adapter.contracts==[first,second] and adapter.changes==[[first,second]]
        await manager.reconfigure([])
        assert adapter.stops==1 and manager._tasks==[]
    asyncio.run(run())


def test_feed_manager_failed_subscription_change_joins_old_socket_before_recovery():
    async def run():
        class Adapter:
            broker="kotak"
            def __init__(self):self.started=asyncio.Event();self.restarted=asyncio.Event();self.calls=0;self.active=0;self.peak=0
            def configured(self):return True
            async def stream(self,contracts):
                self.calls+=1;self.active+=1;self.peak=max(self.peak,self.active);self.contracts=list(contracts)
                (self.started if self.calls==1 else self.restarted).set()
                try:await asyncio.Event().wait()
                finally:self.active-=1
            async def reconfigure(self,contracts):raise ConnectionError("private broker response")
        async def quote(value):pass
        manager=FeedManager(quote);adapter=Adapter();manager._adapters=[adapter]
        first=contract(token="123");second=contract(token="456")
        await manager.start([first]);await asyncio.wait_for(adapter.started.wait(),1)
        await manager.reconfigure([first,second]);await asyncio.wait_for(adapter.restarted.wait(),1)
        assert adapter.peak==1 and adapter.calls==2 and adapter.contracts==[first,second]
        state=manager.statuses()["kotak"]
        assert state["reconnect_count"]==1 and "private broker" not in json.dumps(state)
        await manager.stop()
    asyncio.run(run())
