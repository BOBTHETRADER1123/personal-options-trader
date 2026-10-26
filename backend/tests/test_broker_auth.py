"""5paisa daily OAuth is owner bound, fenced and encrypted; all HTTP is mocked."""
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, configure_sqlite
from app.models import UserSession
from app.broker_auth import (BrokerAuthError, FivepaisaAuthService, FivepaisaConfig,
    FivepaisaCredential, FivepaisaOAuthState, FivepaisaOAuthSlot, IST, LOGIN_URL, TOKEN_URL)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    sql = create_engine(f'sqlite:///{tmp_path / "broker-auth.db"}')
    configure_sqlite(sql, journal_mode='DELETE')
    Base.metadata.create_all(sql)
    factory = sessionmaker(sql, expire_on_commit=False)
    now = [datetime(2026, 10, 26, 9, 30, tzinfo=IST).timestamp()]
    session_hash, other = 'owner-session-hash', 'another-owner-session'
    with factory() as db:
        db.add_all([UserSession(token_hash=value, csrf_token='csrf', expires_at=now[0] + 86400)
            for value in (session_hash, other)])
        db.commit()
    config = FivepaisaConfig('synthetic-app-key', 'synthetic-encryption-key', 'synthetic-user-id',
        'https://trading.example.test/api/brokers/fivepaisa/callback', Fernet.generate_key().decode())
    calls = []
    response = [httpx.Response(200, json={'body': {'Status': 0, 'AccessToken': 'synthetic-access-token',
        'ClientCode': 'synthetic-client-code'}})]
    hook = [None]
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {'timeout': 15.0, 'follow_redirects': False}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if hook[0]:
                hook[0]()
            if isinstance(response[0], Exception):
                raise response[0]
            return response[0]
    monkeypatch.delenv('FIVEPAISA_ACCESS_TOKEN', raising=False)
    monkeypatch.delenv('FIVEPAISA_CLIENT_CODE', raising=False)
    service = FivepaisaAuthService(factory, config, Client, lambda: now[0])
    yield service, factory, config, now, session_hash, other, calls, response, hook
    sql.dispose()


def test_url_state_private_storage_and_exact_official_exchange(setup):
    service, factory, config, now, owner, _, calls, _, _ = setup
    raw = service.begin(owner)
    with factory() as db:
        row = db.scalars(select(FivepaisaOAuthState)).one()
        assert row.state_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in repr(vars(row))
        assert row.expires_at == now[0] + 600 and not row.used
    url = urlparse(service.authorization_url(raw, owner))
    assert url.scheme + '://' + url.netloc + url.path == LOGIN_URL
    assert parse_qs(url.query) == {'VendorKey': [config.app_key], 'ResponseURL': [config.redirect_uri], 'State': [raw]}
    result = service.complete('synthetic-request-token', raw, owner)
    assert result['connected'] and not result['authorization_required']
    assert result['expires_at'] == '2026-10-26T18:29:00+00:00'
    assert calls == [(TOKEN_URL, {'json': {'head': {'Key': config.app_key}, 'body': {
        'RequestToken': 'synthetic-request-token', 'EncryKey': config.encryption_key, 'UserId': config.user_id}}})]
    with factory() as db:
        row = db.get(FivepaisaCredential, 1)
        assert 'synthetic' not in row.encrypted_credentials
        decrypted = json.loads(Fernet(config.credential_key.encode()).decrypt(row.encrypted_credentials.encode()))
        assert decrypted == {'access_token': 'synthetic-access-token', 'client_code': 'synthetic-client-code'}
        assert db.get(FivepaisaOAuthState, hashlib.sha256(raw.encode()).hexdigest()).used
    assert os.environ['FIVEPAISA_ACCESS_TOKEN'] == 'synthetic-access-token'
    assert 'synthetic' not in json.dumps(result)
    assert 'synthetic' not in repr(config)


def test_replay_wrong_owner_expiry_and_latest_begin_never_exchange(setup):
    service, _, _, now, owner, other, calls, _, _ = setup
    first = service.begin(owner)
    with pytest.raises(BrokerAuthError):
        service.complete('request', first, other)
    assert not calls
    second = service.begin(owner)
    with pytest.raises(BrokerAuthError):
        service.authorization_url(first, owner)
    with pytest.raises(BrokerAuthError):
        service.complete('request', first, owner)
    assert not calls
    service.complete('request', second, owner)
    with pytest.raises(BrokerAuthError):
        service.complete('request', second, owner)
    assert len(calls) == 1
    third = service.begin(owner)
    now[0] += 600
    with pytest.raises(BrokerAuthError):
        service.complete('request', third, owner)
    assert len(calls) == 1


def test_http_holds_no_database_transaction_and_new_start_fences_old_callback(setup):
    service, factory, _, _, owner, _, calls, _, hook = setup
    first = service.begin(owner)
    latest = []
    def during_http():
        with factory() as db:
            # A real DELETE-journal write succeeds before the HTTP response.
            db.execute(text("UPDATE user_sessions SET csrf_token='rotated'"))
            db.commit()
        latest.append(service.begin(owner))
    hook[0] = during_http
    with pytest.raises(BrokerAuthError, match='newer'):
        service.complete('request', first, owner)
    with factory() as db:
        assert db.get(FivepaisaCredential, 1) is None
    assert 'FIVEPAISA_ACCESS_TOKEN' not in os.environ
    hook[0] = None
    assert service.complete('request-latest', latest[0], owner)['connected']
    assert len(calls) == 2


def test_restart_restores_token_then_daily_expiry_clears_runtime(setup):
    service, factory, config, now, owner, _, calls, _, _ = setup
    service.complete('request', service.begin(owner), owner)
    os.environ.pop('FIVEPAISA_ACCESS_TOKEN')
    restored = FivepaisaAuthService(factory, config, now=lambda: now[0])
    assert restored.restore()['connected']
    assert os.environ['FIVEPAISA_CLIENT_CODE'] == 'synthetic-client-code'
    now[0] = datetime(2026, 10, 26, 23, 59, tzinfo=IST).timestamp()
    assert restored.restore()['authorization_required']
    assert 'FIVEPAISA_ACCESS_TOKEN' not in os.environ and 'FIVEPAISA_CLIENT_CODE' not in os.environ
    assert len(calls) == 1  # No invented token refresh endpoint.


@pytest.mark.parametrize('failure', [
    httpx.Response(500, text='secret-response-body'),
    httpx.Response(302, headers={'location': 'https://malicious.example.test'}),
    httpx.Response(200, text='not-json secret-response-body'),
    httpx.Response(200, json={'body': {'Status': 1, 'Message': 'secret-response-body'}}),
    httpx.Response(200, json={'body': {'Status': 0, 'AccessToken': 'token-only'}}),
    httpx.Response(200, json={'body': {'Status': False, 'AccessToken': 'token', 'ClientCode': 'client'}}),
    httpx.ReadTimeout('private-request-token-in-exception'),
])
def test_failures_redacted_consumed_and_never_replace_prior_connection(setup, failure):
    service, factory, _, _, owner, _, calls, response, _ = setup
    service.complete('request', service.begin(owner), owner)
    with factory() as db:
        saved = db.get(FivepaisaCredential, 1).encrypted_credentials
    response[0] = failure
    raw = service.begin(owner)
    with pytest.raises(BrokerAuthError) as caught:
        service.complete('request-secret', raw, owner)
    message = str(caught.value)
    assert 'secret' not in message and 'private' not in message
    with factory() as db:
        assert db.get(FivepaisaCredential, 1).encrypted_credentials == saved
        assert db.get(FivepaisaOAuthSlot, 1).last_error == message
    with pytest.raises(BrokerAuthError):
        service.complete('request-secret', raw, owner)
    assert len(calls) == 2
    assert service.status()['connected']


def test_logged_out_during_http_cannot_store_broker_session(setup):
    service, factory, _, _, owner, _, _, _, hook = setup
    def logout():
        with factory() as db:
            db.delete(db.get(UserSession, owner))
            db.commit()
    hook[0] = logout
    with pytest.raises(BrokerAuthError, match='expired'):
        service.complete('request', service.begin(owner), owner)
    with factory() as db:
        assert db.get(FivepaisaCredential, 1) is None


def test_configuration_change_invalidates_state_and_restored_token(setup):
    service, factory, config, now, owner, _, calls, _, hook = setup
    raw = service.begin(owner)
    changed = FivepaisaAuthService(factory, replace(config, app_key='changed-app-key'), now=lambda: now[0])
    with pytest.raises(BrokerAuthError):
        changed.complete('request', raw, owner)
    assert not calls
    service.complete('request', raw, owner)
    assert changed.restore()['authorization_required']
    assert 'FIVEPAISA_ACCESS_TOKEN' not in os.environ
    raw = service.begin(owner)
    hook[0] = lambda: setattr(service, '_config', replace(config, app_key='changed-in-flight'))
    with pytest.raises(BrokerAuthError, match='newer'):
        service.complete('request', raw, owner)


@pytest.mark.parametrize('config', [FivepaisaConfig(), FivepaisaConfig('a','b','c','http://unsafe.example.test/callback','bad'),
    FivepaisaConfig('a','b','c','https://example.test/callback','bad')])
def test_unconfigured_restore_safe_and_begin_rejected(setup, config):
    _, factory, _, now, owner, _, _, _, _ = setup
    service = FivepaisaAuthService(factory, config, now=lambda: now[0])
    assert service.restore()['configured'] is False
    with pytest.raises(BrokerAuthError):
        service.begin(owner)


def test_begin_requires_live_owner_and_bounds_pending_state_table(setup):
    service, factory, _, _, owner, _, _, _, _ = setup
    with pytest.raises(BrokerAuthError):
        service.begin('not-an-active-session')
    for _ in range(5):
        service.begin(owner)
    with factory() as db:
        assert len(list(db.scalars(select(FivepaisaOAuthState)))) == 1


def test_corrupt_encrypted_token_never_claims_connected(setup):
    service, factory, _, _, owner, _, _, _, _ = setup
    service.complete('request', service.begin(owner), owner)
    with factory() as db:
        row = db.get(FivepaisaCredential, 1)
        row.encrypted_credentials = 'corrupt-ciphertext'
        db.commit()
    assert not service.status()['connected']
    assert service.status()['authorization_required']
    assert 'Sign in again' in service.status()['last_error']
    assert not service.restore()['connected']
    assert 'FIVEPAISA_ACCESS_TOKEN' not in os.environ


def test_no_managed_token_preserves_existing_private_environment(setup, monkeypatch):
    service, _, _, _, _, _, _, _, _ = setup
    monkeypatch.setenv('FIVEPAISA_ACCESS_TOKEN', 'existing-private-token')
    monkeypatch.setenv('FIVEPAISA_CLIENT_CODE', 'existing-private-client')
    assert not service.restore()['connected']  # Unverified external tokens are not claimed authenticated.
    assert os.environ['FIVEPAISA_ACCESS_TOKEN'] == 'existing-private-token'
    assert os.environ['FIVEPAISA_CLIENT_CODE'] == 'existing-private-client'
