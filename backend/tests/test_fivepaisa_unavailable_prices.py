"""An untraded strike must not disconnect usable, independent 5paisa quotes."""
import asyncio
import json

import pytest

from app.brokers.base import FeedAuthenticationError, FeedProtocolError
from app.brokers.fivepaisa import FivepaisaFeed, parse_message


def contract(token):
    return {"id": f"fivepaisa:NSE:D:{token}", "broker": "fivepaisa", "token": str(token),
            "underlying": "NIFTY", "exchange": "NSE", "segment": "D",
            "symbol": f"TEST {token}", "expiry": "2030-10-29", "strike": 25000,
            "option_type": "CE", "lot_size": 50, "demo": False}


def mapping(*tokens):
    return {("N", "D", str(token)): contract(token) for token in tokens}


def row(token, price, **extra):
    return {"Exch": "N", "ExchType": "D", "Token": token, "LastRate": price, **extra}


@pytest.mark.parametrize('price', [0, 0.0, '0', '0.00', None, '', '   '])
def test_unavailable_ltp_is_not_a_quote_or_feed_failure(price):
    assert parse_message(row(123, price), mapping(123), received_at='2030-01-01T00:00:00+00:00') == []


def test_zero_strike_does_not_drop_other_contracts_in_same_batch():
    values = parse_message([row(123, 101), row(456, 0), row(789, 52)], mapping(123, 456, 789))
    assert [(value['instrument_id'], value['ltp']) for value in values] == [
        ('fivepaisa:NSE:D:123', 101), ('fivepaisa:NSE:D:789', 52)]
    assert all(value['broker'] == 'fivepaisa' and not value['demo'] for value in values)


def test_unavailable_price_preserves_original_oi_receipt_until_real_quote():
    cache = {}
    earlier, later = '2030-01-01T00:00:00+00:00', '2030-01-01T00:00:05+00:00'
    assert parse_message(row(123, None, OpenInterest=400), mapping(123), received_at=earlier, open_interest=cache) == []
    assert cache[("N", "D", "123")] == {'value': 400, 'received_at': earlier}
    result = parse_message(row(123, 70), mapping(123), received_at=later, open_interest=cache)[0]
    assert result['ltp'] == 70 and result['received_at'] == later
    assert result['open_interest'] == 400 and result['open_interest_received_at'] == earlier


@pytest.mark.parametrize('price', [-1, 'bad-private-value', float('nan'), float('inf'), {}, True])
def test_malformed_price_still_reports_protocol_failure_without_raw_value(price):
    with pytest.raises(FeedProtocolError) as error:
        parse_message(row(123, price), mapping(123))
    assert 'bad-private-value' not in str(error.value)


@pytest.mark.parametrize('message', [{'Message': 'Token expired'}, row(123, None, Message='Invalid access token')])
def test_authentication_rejection_is_not_mislabelled_untraded(message):
    with pytest.raises(FeedAuthenticationError):
        parse_message(message, mapping(123))


def test_unknown_contract_is_not_silently_accepted():
    with pytest.raises(FeedProtocolError, match='unmapped'):
        parse_message(row(999, 0), mapping(123))


def test_stream_continues_from_unavailable_messages_to_independent_real_prices(monkeypatch):
    import websockets.asyncio.client
    monkeypatch.setenv('FIVEPAISA_CLIENT_CODE', 'synthetic-client')
    monkeypatch.setenv('FIVEPAISA_ACCESS_TOKEN', 'synthetic-token')
    class Socket:
        def __init__(self):
            self.messages = iter([json.dumps([row(123, 0)]), json.dumps([row(456, 45)]),
                                  json.dumps([row(123, None, OpenInterest=90)]), json.dumps([row(123, 12)])])
            self.sent = []
        async def __aenter__(self):return self
        async def __aexit__(self, *args):pass
        async def send(self, value):self.sent.append(json.loads(value))
        def __aiter__(self):return self
        async def __anext__(self):
            try:return next(self.messages)
            except StopIteration:raise StopAsyncIteration
    socket = Socket()
    monkeypatch.setattr(websockets.asyncio.client, 'connect', lambda *args, **kwargs: socket)
    quotes = []
    async def on_quote(value):quotes.append(value)
    async def on_status(*args):pass
    asyncio.run(FivepaisaFeed(on_quote, on_status).stream([contract(123), contract(456)]))
    assert [(value['instrument_id'], value['ltp']) for value in quotes] == [
        ('fivepaisa:NSE:D:456', 45), ('fivepaisa:NSE:D:123', 12)]
    assert quotes[1]['open_interest'] == 90
    assert [entry['Operation'] for entry in socket.sent] == ['Subscribe', 'Subscribe', 'Unsubscribe', 'Unsubscribe']
