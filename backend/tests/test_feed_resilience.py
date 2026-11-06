"""Offline tests for feed drops: backoff reset, login classification, bad ticks, closed-market label.

These use fake broker objects. They do not prove an authenticated Kotak connection.
"""
import asyncio
import sys
import types

import pytest

import app.brokers as manager_module
from app.brokers import FeedManager
from app.brokers.base import FeedAuthenticationError, FeedProtocolError
from app.brokers.kotak import KotakFeed, is_transient_network_error


def contract(token="123"):
    return {"id": f"kotak:mcx_fo:{token}", "broker": "kotak", "token": str(token), "underlying": "CRUDEOIL",
            "exchange": "MCX", "segment": "mcx_fo", "symbol": f"OFFLINE {token}", "expiry": "2030-11-15",
            "strike": 6000, "option_type": "CE", "lot_size": 100, "demo": False}


def row(token="123", **fields):
    return {"type": "scrip", "exchange_segment": "mcx_fo", "instrument_token": token, **fields}


def quiet_clock(monkeypatch):
    delays = []
    async def pause(delay):
        delays.append(round(delay, 3))
    monkeypatch.setattr(manager_module.asyncio, "sleep", pause)
    monkeypatch.setattr(manager_module.random, "uniform", lambda *a: 0)
    return delays


def test_backoff_grows_on_short_lived_quotes_and_resets_after_a_stable_connection(monkeypatch):
    delays = quiet_clock(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(manager_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    async def run():
        manager = FeedManager(_noop)

        class Adapter:
            broker = "kotak"
            calls = 0

            async def stream(self, contracts):
                self.calls += 1
                if self.calls == 5:
                    manager._stopping = True
                    return
                quote = {"broker": "kotak", "received_at": "2030-01-01T00:00:00+00:00"}
                await manager._quote(quote)
                clock[0] += 30 if self.calls == 3 else 1
                await manager._quote(quote)
                raise ConnectionError("dropped after receiving quotes")

        await manager._run(Adapter(), [])

    asyncio.run(run())
    assert delays == [2, 4, 2, 4]


def test_backoff_still_grows_when_nothing_was_ever_received(monkeypatch):
    delays = quiet_clock(monkeypatch)
    async def run():
        manager = FeedManager(None)
        class Adapter:
            broker = "kotak"
            calls = 0
            async def stream(self, contracts):
                self.calls += 1
                if self.calls == 4:
                    manager._stopping = True
                    return
                raise ConnectionError("cannot connect")
        await manager._run(Adapter(), [])
    asyncio.run(run())
    assert delays == [2, 4, 8]


def test_market_closed_is_reported_but_never_hides_a_real_fault():
    async def run():
        manager = FeedManager(None)
        manager._states["kotak"]["configured"] = True
        manager._states["kotak"]["status"] = "disconnected"
        await manager.mark_market_closed()
        assert manager.statuses()["kotak"]["status"] == "market_closed"
        manager._states["kotak"]["status"] = "authentication_required"
        await manager.mark_market_closed()
        assert manager.statuses()["kotak"]["status"] == "authentication_required"
    asyncio.run(run())


class Rejected(Exception):
    def __init__(self, status):
        super().__init__("private response body")
        self.response = types.SimpleNamespace(status_code=status)


@pytest.mark.parametrize("error,expected", [
    (TimeoutError(), True), (ConnectionResetError(), True), (OSError("network is unreachable"), True),
    (Rejected(503), True), (Rejected(429), True), (Rejected(408), True), (Rejected(424), True),
    (Rejected(401), False), (Rejected(400), False), (ValueError("Authentication did not establish a valid session"), False),
])
def test_only_network_trouble_counts_as_transient(error, expected):
    assert is_transient_network_error(error) is expected


def fake_login_sdk(monkeypatch, login_error):
    class AuthenticationError(Exception): pass
    class SubscriptionError(Exception): pass
    class Client:
        configuration = types.SimpleNamespace(edit_sid="sid")
        def __init__(self, **kwargs): pass
        def totp_login(self, **kwargs):
            if login_error: raise login_error
        def totp_validate(self, **kwargs): pass
        def create_websocket(self, **kwargs): raise AssertionError("must not reach the socket when login failed")
    modules = {name: types.ModuleType(name) for name in (
        "neo_api_client", "neo_api_client.logger", "neo_api_client.websocket",
        "neo_api_client.websocket.feed", "neo_api_client.websocket.feed.exceptions")}
    modules["neo_api_client"].NeoAPI = Client
    modules["neo_api_client.logger"].setup_logging = lambda **kwargs: None
    modules["neo_api_client.websocket.feed"].SFeedWebSocket = object
    modules["neo_api_client.websocket.feed"].WsToken = lambda segment, token: (segment, token)
    modules["neo_api_client.websocket.feed.exceptions"].AuthenticationError = AuthenticationError
    modules["neo_api_client.websocket.feed.exceptions"].SubscriptionError = SubscriptionError
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("KOTAK_SID", raising=False)
    for name, value in (("KOTAK_CONSUMER_KEY", "k" * 10), ("KOTAK_MOBILE_NUMBER", "+919999999999"),
                        ("KOTAK_UCC", "ABC12"), ("KOTAK_MPIN", "123456"), ("KOTAK_TOTP", "123456")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("KOTAK_TOTP_SECRET", raising=False)


async def _noop(*args):
    pass


@pytest.mark.parametrize("login_error,expected", [
    (TimeoutError("login timed out"), ConnectionError),
    (Rejected(502), ConnectionError),
    (Rejected(424), ConnectionError),
    (Rejected(401), FeedAuthenticationError),
    (RuntimeError("anything else"), FeedAuthenticationError),
])
def test_login_outage_retries_but_rejected_login_stops(monkeypatch, login_error, expected):
    fake_login_sdk(monkeypatch, login_error)
    with pytest.raises(expected) as error:
        asyncio.run(KotakFeed(_noop, _noop).stream([contract()]))
    assert "private response body" not in str(error.value)
    if expected is ConnectionError:
        assert not isinstance(error.value, FeedAuthenticationError)


def test_one_malformed_message_no_longer_stops_every_contract(monkeypatch):
    class Socket:
        def __init__(self):
            self.messages = iter([row("123", last_traded_price=-5), row("123", last_traded_price="junk"),
                                  row("456", last_traded_price=45, buy=["not a dict"]), row("456", last_traded_price=46)])
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def subscribe_scrips(self, tokens): pass
        async def unsubscribe_scrips(self, tokens): pass
        def __aiter__(self): return self
        async def __anext__(self):
            try: return next(self.messages)
            except StopIteration: raise StopAsyncIteration
    class AuthenticationError(Exception): pass
    class SubscriptionError(Exception): pass
    modules = {name: types.ModuleType(name) for name in (
        "neo_api_client", "neo_api_client.logger", "neo_api_client.websocket",
        "neo_api_client.websocket.feed", "neo_api_client.websocket.feed.exceptions")}
    modules["neo_api_client"].NeoAPI = object
    modules["neo_api_client.logger"].setup_logging = lambda **kwargs: None
    modules["neo_api_client.websocket.feed"].SFeedWebSocket = lambda **kwargs: Socket()
    modules["neo_api_client.websocket.feed"].WsToken = lambda segment, token: (segment, token)
    modules["neo_api_client.websocket.feed.exceptions"].AuthenticationError = AuthenticationError
    modules["neo_api_client.websocket.feed.exceptions"].SubscriptionError = SubscriptionError
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("KOTAK_SID", "offline-session")
    monkeypatch.setenv("KOTAK_UCC", "offline-owner")
    quotes, statuses = [], []
    async def on_quote(value): quotes.append(value)
    async def on_status(broker, state): statuses.append(state)

    async def run():
        manager = FeedManager(on_quote, on_status)
        await manager._adapters[0].stream([contract("123"), contract("456")])

    asyncio.run(run())
    assert [(q["instrument_id"], q["ltp"]) for q in quotes] == [("kotak:mcx_fo:456", 46)]
    assert [state["status"] for state in statuses] == ["awaiting_quotes", "awaiting_quotes", "connected"]
    assert statuses[0]["awaiting_since"]
    assert "malformed" in statuses[1]["detail"]
    assert statuses[2]["awaiting_since"] is None


def test_totp_code_is_not_sent_twice_in_the_same_30_second_step(monkeypatch):
    fake_login_sdk(monkeypatch, None)
    monkeypatch.delenv("KOTAK_TOTP", raising=False)
    monkeypatch.setenv("KOTAK_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    clock = [1_000_000_010.0]
    waits, codes = [], []
    class TOTP:
        def __init__(self, secret): pass
        def at(self, timestamp):
            code = str(int(timestamp // 30)); codes.append(code); return code
    monkeypatch.setitem(sys.modules, "pyotp", types.SimpleNamespace(TOTP=TOTP))
    import app.brokers.kotak as kotak_module
    async def fake_sleep(delay):
        waits.append(round(delay, 1)); clock[0] += delay
    monkeypatch.setattr(kotak_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(kotak_module.time, "time", lambda: clock[0])
    feed = KotakFeed(_noop, _noop)
    for _ in range(2):
        try:
            asyncio.run(feed.stream([contract()]))
        except AssertionError:
            pass  # the fake client refuses to open a socket; only the login step matters here
    assert len(codes) == 2 and codes[0] != codes[1]
    assert len(waits) == 1 and 0 < waits[0] <= 31


class SDKRejected(Exception):
    """The pinned SDK uses .status, not .response.status_code."""
    def __init__(self, status):
        super().__init__("private-sdk-response")
        self.status = status


@pytest.mark.parametrize("status,expected", [(429,True),(500,True),(503,True),(400,False),(401,False),(403,False),("502",True)])
def test_sdk_status_is_classified_without_echoing_response(status,expected):
    assert is_transient_network_error(SDKRejected(status)) is expected


def test_wrapped_sdk_transport_cause_retries_but_unexplained_status_zero_does_not():
    error=SDKRejected(0)
    error.__cause__=TimeoutError("private timeout body")
    assert is_transient_network_error(error)
    assert not is_transient_network_error(SDKRejected(0))
    error.status=401
    assert not is_transient_network_error(error)


def test_httpx_transport_failure_is_recognized_even_when_sdk_wraps_it():
    import httpx
    error=SDKRejected(0)
    error.__cause__=httpx.ConnectError("private connection data")
    assert is_transient_network_error(error)


def test_login_enables_existing_sdk_http_error_policy(monkeypatch):
    fake_login_sdk(monkeypatch,None)
    Client=sys.modules["neo_api_client"].NeoAPI
    def initialize(self, **kwargs):
        self.api_client=types.SimpleNamespace(rest_client=types.SimpleNamespace(raise_on_error=False))
    def login(self, **kwargs):
        assert self.api_client.rest_client.raise_on_error is True
        raise SDKRejected(503)
    monkeypatch.setattr(Client,"__init__",initialize)
    monkeypatch.setattr(Client,"totp_login",login)
    with pytest.raises(ConnectionError) as error:
        asyncio.run(KotakFeed(_noop,_noop).stream([contract()]))
    assert "private-sdk-response" not in str(error.value)


def test_rejected_login_body_does_not_send_mpin_or_start_socket(monkeypatch):
    fake_login_sdk(monkeypatch,None)
    Client=sys.modules["neo_api_client"].NeoAPI
    monkeypatch.setattr(Client,"totp_login",lambda self, **kwargs:{"error":[{"code":"401","message":"private-response"}]})
    def forbidden(self, **kwargs):
        pytest.fail("MPIN validation must not follow a rejected first login")
    monkeypatch.setattr(Client,"totp_validate",forbidden)
    with pytest.raises(FeedAuthenticationError) as error:
        asyncio.run(KotakFeed(_noop,_noop).stream([contract()]))
    assert "private-response" not in str(error.value)


@pytest.mark.parametrize("times,basis,expected", [
    ({"last_update_time":1792464001,"last_trade_time":1792464000},"last_update_time","2026-10-20T02:40:01.000000+00:00"),
    ({"last_update_time":0,"last_trade_time":1792464000},"last_trade_time","2026-10-20T02:40:00.000000+00:00"),
    ({"last_update_time":"invalid","last_trade_time":1792464000},"last_trade_time","2026-10-20T02:40:00.000000+00:00"),
    ({},"unavailable",None),
])
def test_quote_retains_exchange_timestamp_semantics(times,basis,expected):
    from app.brokers.kotak import parse_sfeed
    quote=parse_sfeed(row(last_traded_price=10,**times),{("mcx_fo","123"):contract()})
    assert quote["exchange_timestamp"]==expected
    assert quote["exchange_timestamp_basis"]==basis


def test_boolean_depth_prices_cannot_become_genuine_bid_ask():
    from app.brokers.kotak import parse_sfeed
    from app.brokers.base import FeedProtocolError
    with pytest.raises(FeedProtocolError):
        parse_sfeed(row(last_traded_price=10,buy=[{"price":True}]),{("mcx_fo","123"):contract()})


@pytest.mark.parametrize("failure,expected", [
    ("transport", ConnectionError),
    ("timeout", ConnectionError),
    ("rejected", FeedAuthenticationError),
    ("handshake_rejected", FeedAuthenticationError),
    ("cancelled", asyncio.CancelledError),
])
def test_failed_sdk_context_entry_closes_socket_before_retry(monkeypatch, failure, expected):
    from websockets.exceptions import ConnectionClosed

    fake_login_sdk(monkeypatch, None)
    monkeypatch.setenv("KOTAK_SID", "offline-session")
    auth_error = sys.modules["neo_api_client.websocket.feed.exceptions"].AuthenticationError
    transitions = []

    class Socket:
        def __init__(self, **settings):
            assert settings["max_reconnect_attempts"] == 0
            assert settings["max_connect_retries"] == 0

        async def __aenter__(self):
            transitions.append("opened")
            if failure == "transport":
                raise auth_error("private transport response") from ConnectionClosed(None, None)
            if failure == "timeout":
                raise auth_error("Authentication timeout")
            if failure == "handshake_rejected":
                raise RuntimeError("private handshake response") from Rejected(403)
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise auth_error("private credential rejection")

        async def __aexit__(self, *args):
            pytest.fail("Python must not exit an unentered context")

        async def close(self):
            transitions.append("closed")

    monkeypatch.setattr(sys.modules["neo_api_client.websocket.feed"], "SFeedWebSocket", Socket)
    with pytest.raises(expected) as error:
        asyncio.run(KotakFeed(_noop, _noop).stream([contract()]))
    assert transitions == ["opened", "closed"]
    assert "private" not in str(error.value)


def test_nested_explicit_rejection_is_terminal_even_with_generic_connection_error():
    failure = ConnectionError("private wrapper")
    failure.__cause__ = Rejected(403)
    assert not is_transient_network_error(failure)


@pytest.mark.parametrize("transport", [True, False])
def test_reconfigure_only_retries_transient_sdk_subscription_failure(monkeypatch, transport):
    fake_login_sdk(monkeypatch, None)
    subscription_error = sys.modules["neo_api_client.websocket.feed.exceptions"].SubscriptionError

    class Socket:
        async def subscribe_scrips(self, tokens):
            if transport:
                raise subscription_error("private response") from ConnectionResetError()
            raise subscription_error("private response")

    async def run():
        feed = KotakFeed(_noop, _noop)
        feed._socket = Socket()
        with pytest.raises(ConnectionError if transport else FeedProtocolError) as error:
            await feed.reconfigure([contract()])
        assert "private response" not in str(error.value)

    asyncio.run(run())


def test_manager_does_not_retry_a_terminal_subscription_update():
    async def run():
        manager = FeedManager(_noop)
        started = asyncio.Event()

        class Adapter:
            broker = "kotak"

            def configured(self):
                return True

            async def stream(self, contracts):
                assert not started.is_set(), "Terminal subscription errors must not create another socket"
                started.set()
                await asyncio.Event().wait()

            async def reconfigure(self, contracts):
                raise FeedProtocolError("Invalid subscription configuration")

        manager._adapters = [Adapter()]
        await manager.start([contract()])
        await started.wait()
        await manager.reconfigure([contract("456")])
        assert manager.statuses()["kotak"]["status"] == "error"
        assert manager.statuses()["kotak"]["reconnect_count"] == 0
        assert not manager._tasks

    asyncio.run(run())
