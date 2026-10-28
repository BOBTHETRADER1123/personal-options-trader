"""Untraded Kotak strikes do not interrupt other genuine SFeed quotes.

These are offline protocol tests, not authenticated broker acceptance results.
"""
import asyncio
import sys
import types

import pytest

from app.brokers.base import FeedAuthenticationError, FeedProtocolError
from app.brokers.kotak import KotakFeed, parse_sfeed


def contract(token="123"):
    return {"id": f"kotak:mcx_fo:{token}", "broker": "kotak", "token": str(token),
            "underlying": "CRUDEOIL", "exchange": "MCX", "segment": "mcx_fo",
            "symbol": f"OFFLINE {token}", "expiry": "2030-11-15", "strike": 6000,
            "option_type": "CE", "lot_size": 100, "demo": False}


def mapping(*tokens):
    return {("mcx_fo", str(token)): contract(token) for token in tokens}


def row(token="123", **fields):
    return {"type": "scrip", "exchange_segment": "mcx_fo", "instrument_token": token, **fields}


@pytest.mark.parametrize("price", [0, 0.0, "0", "0.00", None, "", "   "])
def test_unavailable_ltp_produces_no_quote(price):
    assert parse_sfeed(row(last_traded_price=price), mapping("123")) is None


def test_absent_ltp_does_not_make_a_new_timestamp_or_zero_quote():
    assert parse_sfeed(row(open_interest=200, last_update_time=1792464001), mapping("123")) is None


@pytest.mark.parametrize("price", [-1, "private-invalid-value", float("nan"), float("inf"), {}, [], True, False])
def test_malformed_ltp_still_fails_without_echoing_raw_data(price):
    with pytest.raises(FeedProtocolError) as error:
        parse_sfeed(row(last_traded_price=price), mapping("123"))
    assert "private-invalid-value" not in str(error.value)


def test_unknown_contract_is_not_hidden_by_unavailable_ltp():
    with pytest.raises(FeedProtocolError, match="unmapped"):
        parse_sfeed(row("999", last_traded_price=0), mapping("123"))


def test_scaled_quote_retains_its_own_original_timestamps_and_depth():
    data=row(last_traded_price=123.45, last_update_time=1792464001,
             buy=[{"price":123.4}], sell=[{"price":123.5}], open_interest=300)
    result=parse_sfeed(data, mapping("123"), received_at="2030-01-01T00:00:05+00:00", received_ns=1)
    assert result["ltp"]==123.45 and result["bid"]==123.4 and result["ask"]==123.5
    assert result["exchange_timestamp"]=="2026-10-20T02:40:01.000000+00:00"
    assert result["received_at"]=="2030-01-01T00:00:05+00:00" and result["received_ns"]==1
    assert result["raw_message"]==data and result["broker"]=="kotak" and not result["demo"]


def fake_sdk(monkeypatch, socket):
    class AuthenticationError(Exception):pass
    class SubscriptionError(Exception):pass
    modules={name:types.ModuleType(name) for name in (
        "neo_api_client", "neo_api_client.logger", "neo_api_client.websocket",
        "neo_api_client.websocket.feed", "neo_api_client.websocket.feed.exceptions")}
    modules["neo_api_client"].NeoAPI=object
    modules["neo_api_client.logger"].setup_logging=lambda **kwargs:None
    modules["neo_api_client.websocket.feed"].SFeedWebSocket=lambda **kwargs:socket
    modules["neo_api_client.websocket.feed"].WsToken=lambda segment,token:(segment,token)
    modules["neo_api_client.websocket.feed.exceptions"].AuthenticationError=AuthenticationError
    modules["neo_api_client.websocket.feed.exceptions"].SubscriptionError=SubscriptionError
    for name,module in modules.items():monkeypatch.setitem(sys.modules,name,module)
    monkeypatch.setenv("KOTAK_SID","offline-session")
    monkeypatch.setenv("KOTAK_UCC","offline-owner")
    return AuthenticationError,SubscriptionError


def test_stream_continues_after_untraded_strikes(monkeypatch):
    class Socket:
        def __init__(self):
            self.messages=iter([row(last_traded_price=0), row("456",last_traded_price=45),
                                row(), row(last_traded_price=12)])
            self.subscribed=[];self.unsubscribed=[]
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def subscribe_depth(self,tokens):self.subscribed.extend(tokens)
        async def unsubscribe_depth(self,tokens):self.unsubscribed.extend(tokens)
        def __aiter__(self):return self
        async def __anext__(self):
            try:return next(self.messages)
            except StopIteration:raise StopAsyncIteration
    socket=Socket();fake_sdk(monkeypatch,socket)
    quotes=[]
    async def on_quote(value):quotes.append(value)
    async def on_status(*args):pass
    asyncio.run(KotakFeed(on_quote,on_status).stream([contract("123"),contract("456")]))
    assert [(q["instrument_id"],q["ltp"]) for q in quotes]==[("kotak:mcx_fo:456",45),("kotak:mcx_fo:123",12)]
    assert socket.subscribed==socket.unsubscribed==[("mcx_fo","123"),("mcx_fo","456")]


@pytest.mark.parametrize("failure",["authentication","subscription"])
def test_sdk_failures_remain_visible_and_sanitized(monkeypatch,failure):
    class Socket:
        async def __aenter__(self):raise thrown("private-sdk-response")
        async def __aexit__(self,*args):pass
    auth,subscription=fake_sdk(monkeypatch,Socket())
    thrown=auth if failure=="authentication" else subscription
    async def on_quote(value):pytest.fail("No quote may be published")
    async def on_status(*args):pass
    expected=FeedAuthenticationError if failure=="authentication" else FeedProtocolError
    with pytest.raises(expected) as error:
        asyncio.run(KotakFeed(on_quote,on_status).stream([contract()]))
    assert "private-sdk-response" not in str(error.value)
