"""Owner-bound, daily 5paisa authorization; never places broker orders.

Request tokens exist only during the TLS exchange. Access tokens and client
codes are encrypted before persistence. Public methods expose no credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timezone
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
import httpx
from sqlalchemy import Boolean, Float, Integer, String, Text, delete, update
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, SessionLocal, run_transaction
from .models import UserSession

IST = ZoneInfo('Asia/Kolkata')
TOKEN_URL = 'https://Openapi.5paisa.com/VendorsAPI/Service1.svc/GetAccessToken'
LOGIN_URL = 'https://dev-openapi.5paisa.com/WebVendorLogin/VLogin/Index'
_LOCK = threading.RLock()


class BrokerAuthError(Exception):
    """Fixed safe messages only: callers must not include HTTP response bodies."""


class FivepaisaCredential(Base):
    __tablename__ = 'fivepaisa_credentials'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    encrypted_credentials: Mapped[str] = mapped_column(Text)
    config_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)
    connected_at: Mapped[float] = mapped_column(Float)


class FivepaisaOAuthState(Base):
    __tablename__ = 'fivepaisa_oauth_states'
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class FivepaisaOAuthSlot(Base):
    """Latest-start fence remains present while the broker HTTP request runs."""
    __tablename__ = 'fivepaisa_oauth_slot'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    current_state_hash: Mapped[str] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


@dataclass(frozen=True)
class FivepaisaConfig:
    app_key: str = field(default='', repr=False)
    encryption_key: str = field(default='', repr=False)
    user_id: str = field(default='', repr=False)
    redirect_uri: str = ''
    credential_key: str = field(default='', repr=False)

    @classmethod
    def from_env(cls):
        return cls(*(os.getenv(name, '').strip() for name in (
            'FIVEPAISA_APP_KEY', 'FIVEPAISA_ENCRYPTION_KEY', 'FIVEPAISA_USER_ID',
            'FIVEPAISA_REDIRECT_URI', 'BROKER_CREDENTIAL_KEY')))

    def cipher(self):
        if not all((self.app_key, self.encryption_key, self.user_id, self.redirect_uri, self.credential_key)):
            raise BrokerAuthError('5paisa sign-in is not configured on the server yet.')
        uri = urlparse(self.redirect_uri)
        local = uri.hostname in ('localhost', '127.0.0.1')
        if (not uri.hostname or uri.username or uri.password or uri.query or uri.fragment or
                (uri.scheme != 'https' and not (uri.scheme == 'http' and local))):
            raise BrokerAuthError('The 5paisa callback address must be a secure application address.')
        try:
            return Fernet(self.credential_key.encode('ascii'))
        except (ValueError, UnicodeError, TypeError):
            raise BrokerAuthError('The private broker encryption key needs server configuration.') from None

    def fingerprint(self):
        return hashlib.sha256(json.dumps((self.app_key, self.encryption_key, self.user_id,
            self.redirect_uri, self.credential_key), separators=(',', ':')).encode()).hexdigest()


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _valid_session(db, session_hash, now):
    session = db.get(UserSession, session_hash)
    if not session or session.expires_at <= now:
        raise BrokerAuthError('Your app sign-in expired. Sign in again before connecting 5paisa.')


def _expiry(now):
    day = datetime.fromtimestamp(now, IST).date()
    return datetime.combine(day, datetime_time(23, 59), IST).timestamp()


class FivepaisaAuthService:
    def __init__(self, session_factory=SessionLocal, config=None, client_factory=None, now=None):
        self.factory = session_factory
        self._config = config
        self.client_factory = client_factory or httpx.Client
        self.now = now or time.time

    @property
    def config(self):
        return self._config if self._config is not None else FivepaisaConfig.from_env()

    def _transaction(self, operation):
        # Single application worker plus transactional state consumption/fencing.
        # No network request runs inside this lock or any database transaction.
        with _LOCK:
            return run_transaction(operation, session_factory=self.factory)

    def status(self, db=None):
        if db is None:
            with self.factory() as owned:
                return self.status(owned)
        configured, config_error = True, None
        config = self.config
        try:
            config.cipher()
        except BrokerAuthError as error:
            configured, config_error = False, str(error)
        credential = db.get(FivepaisaCredential, 1)
        slot = db.get(FivepaisaOAuthSlot, 1)
        compatible = bool(credential and configured and credential.config_hash == config.fingerprint())
        connected = bool(compatible and credential.expires_at > self.now())
        credential_error = None
        if connected:
            try:
                value = json.loads(config.cipher().decrypt(credential.encrypted_credentials.encode()))
                if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key]
                        for key in ('access_token', 'client_code')):
                    raise ValueError('Invalid saved credential format')
            except (BrokerAuthError, InvalidToken, ValueError, TypeError):
                connected = False
                credential_error = 'Saved 5paisa authorization could not be read. Sign in again.'
        return {'configured': configured, 'connected': connected,
                'authorization_required': not connected,
                'expires_at': datetime.fromtimestamp(credential.expires_at, timezone.utc).isoformat() if compatible else None,
                'expiry_basis': '5paisa daily authorization ends at 23:59 IST; sign in again when it expires.',
                'last_error': config_error or credential_error or (slot.last_error if slot else None)}

    def begin(self, session_hash):
        config = self.config
        config.cipher()
        raw = secrets.token_urlsafe(32)
        state_hash = _digest(raw)
        now = self.now()
        def reserve(db):
            _valid_session(db, session_hash, now)
            # Only one pending authorization is supported; earlier callbacks are
            # invalidated without touching an existing valid broker connection.
            db.execute(delete(FivepaisaOAuthState))
            db.add(FivepaisaOAuthState(state_hash=state_hash, session_hash=session_hash,
                config_hash=config.fingerprint(), expires_at=now + 600, used=False))
            slot = db.get(FivepaisaOAuthSlot, 1)
            if slot:
                slot.current_state_hash, slot.last_error = state_hash, None
            else:
                db.add(FivepaisaOAuthSlot(id=1, current_state_hash=state_hash, last_error=None))
        self._transaction(reserve)
        return raw

    def _check_state(self, db, raw_state, session_hash, now, config):
        if not isinstance(raw_state, str) or not 30 <= len(raw_state) <= 200:
            raise BrokerAuthError('This 5paisa sign-in request is invalid or expired. Start again.')
        _valid_session(db, session_hash, now)
        state_hash = _digest(raw_state)
        state = db.get(FivepaisaOAuthState, state_hash)
        slot = db.get(FivepaisaOAuthSlot, 1)
        if (not state or state.used or state.expires_at <= now or state.session_hash != session_hash or
                state.config_hash != config.fingerprint() or not slot or slot.current_state_hash != state_hash):
            raise BrokerAuthError('This 5paisa sign-in request is invalid or expired. Start again.')
        return state_hash

    def authorization_url(self, state, session_hash):
        config = self.config
        config.cipher()
        with self.factory() as db:
            self._check_state(db, state, session_hash, self.now(), config)
        return LOGIN_URL + '?' + urlencode({'VendorKey': config.app_key,
            'ResponseURL': config.redirect_uri, 'State': state})

    def complete(self, request_token, state, session_hash):
        config = self.config
        cipher = config.cipher()
        if (not isinstance(request_token, str) or not 1 <= len(request_token) <= 4096 or
                any(ord(char) < 32 for char in request_token)):
            raise BrokerAuthError('5paisa did not return a valid authorization. Start sign-in again.')
        def consume(db):
            state_hash = self._check_state(db, state, session_hash, self.now(), config)
            changed = db.execute(update(FivepaisaOAuthState).where(
                FivepaisaOAuthState.state_hash == state_hash,
                FivepaisaOAuthState.used.is_(False)).values(used=True)).rowcount
            if changed != 1:
                raise BrokerAuthError('This 5paisa sign-in request has already been used. Start again.')
            return state_hash
        state_hash = self._transaction(consume)
        try:
            with self.client_factory(timeout=15.0, follow_redirects=False) as client:
                response = client.post(TOKEN_URL, json={'head': {'Key': config.app_key}, 'body': {
                    'RequestToken': request_token, 'EncryKey': config.encryption_key, 'UserId': config.user_id}})
                if response.status_code != 200:
                    raise BrokerAuthError('5paisa could not complete sign-in. Start again on its official page.')
                payload = response.json()
            body = payload.get('body') if isinstance(payload, dict) else None
            token = body.get('AccessToken') if isinstance(body, dict) else None
            code = body.get('ClientCode') if isinstance(body, dict) else None
            status = body.get('Status') if isinstance(body, dict) else None
            if (status not in (0, '0') or isinstance(status, bool) or
                    not isinstance(token, str) or not 1 <= len(token) <= 8192 or
                    not isinstance(code, str) or not 1 <= len(code) <= 100 or
                    any(ord(char) < 33 for char in token + code)):
                raise BrokerAuthError('5paisa did not approve this sign-in. Start again on its official page.')
            now, expires_at = self.now(), _expiry(self.now())
            if expires_at <= now:
                raise BrokerAuthError('5paisa daily authorization has ended. Sign in after midnight IST.')
            encrypted = cipher.encrypt(json.dumps({'access_token': token, 'client_code': code},
                separators=(',', ':')).encode()).decode()
            def persist(db):
                _valid_session(db, session_hash, self.now())
                slot = db.get(FivepaisaOAuthSlot, 1)
                if not slot or slot.current_state_hash != state_hash or self.config.fingerprint() != config.fingerprint():
                    raise BrokerAuthError('A newer 5paisa sign-in replaced this request. Use the latest sign-in.')
                credential = db.get(FivepaisaCredential, 1)
                if not credential:
                    credential = FivepaisaCredential(id=1)
                    db.add(credential)
                credential.encrypted_credentials = encrypted
                credential.config_hash = config.fingerprint()
                credential.expires_at, credential.connected_at = expires_at, now
                slot.last_error = None
            self._transaction(persist)
            # The private environment is updated only after a durable commit.
            # The fence prevents an older in-flight callback replacing new data.
            self.restore()
            return self.status()
        except Exception as error:
            safe = str(error) if isinstance(error, BrokerAuthError) else '5paisa sign-in could not be completed. Start again; your existing records are preserved.'
            def record_failure(db):
                slot = db.get(FivepaisaOAuthSlot, 1)
                if slot and slot.current_state_hash == state_hash:
                    slot.last_error = safe
            self._transaction(record_failure)
            raise BrokerAuthError(safe) from None

    def restore(self):
        config = self.config
        def read(db):
            try:
                cipher = config.cipher()
                row = db.get(FivepaisaCredential, 1)
                if not row or row.expires_at <= self.now() or row.config_hash != config.fingerprint():
                    return None
                value = json.loads(cipher.decrypt(row.encrypted_credentials.encode()))
                if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key]
                        for key in ('access_token', 'client_code')):
                    return None
                return value
            except (BrokerAuthError, InvalidToken, ValueError, TypeError):
                return None
        with _LOCK:
            with self.factory() as db:
                managed = db.get(FivepaisaCredential, 1) is not None
                value = read(db)
            if value:
                os.environ['FIVEPAISA_ACCESS_TOKEN'] = value['access_token']
                os.environ['FIVEPAISA_CLIENT_CODE'] = value['client_code']
            elif managed:
                os.environ.pop('FIVEPAISA_ACCESS_TOKEN', None)
                os.environ.pop('FIVEPAISA_CLIENT_CODE', None)
        return self.status()
