"""Owner-provided Kotak feed credentials, encrypted before durable storage.

This module never authenticates with the broker or places orders. HTTP routes
must require the owner session and CSRF protection before calling ``save``.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, SessionLocal, iso_now, run_transaction


class BrokerSetupError(ValueError):
    """A fixed, credential-free error safe for the owner interface."""


class PrivateBrokerCredential(Base):
    __tablename__ = 'private_broker_credentials'
    broker: Mapped[str] = mapped_column(String(24), primary_key=True)
    encrypted_payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(40), default=iso_now)


_FIELDS = {
    'consumer_key': 'KOTAK_CONSUMER_KEY',
    'mobile_number': 'KOTAK_MOBILE_NUMBER',
    'ucc': 'KOTAK_UCC',
    'mpin': 'KOTAK_MPIN',
    'totp_secret': 'KOTAK_TOTP_SECRET',
}
_SETUP_LOCK = threading.RLock()


def _validated(values):
    if not isinstance(values, Mapping) or set(values) != set(_FIELDS):
        raise BrokerSetupError('Enter all five Kotak connection fields; extra fields are not accepted.')
    if any(not isinstance(value, str) for value in values.values()):
        raise BrokerSetupError('Kotak connection fields must be text.')
    result = {key: value.strip() for key, value in values.items()}
    if not re.fullmatch(r'[!-~]{8,512}', result['consumer_key']):
        raise BrokerSetupError('Enter the Kotak application API key without spaces.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', result['ucc']):
        raise BrokerSetupError('Enter the client code shown in your Kotak account.')
    mobile = result['mobile_number']
    if re.fullmatch(r'[6-9][0-9]{9}', mobile):
        mobile = '+91' + mobile
    if not re.fullmatch(r'\+91[6-9][0-9]{9}', mobile):
        raise BrokerSetupError('Enter the registered Indian mobile number, with an optional +91 prefix.')
    result['mobile_number'] = mobile
    if not re.fullmatch(r'[0-9]{6}', result['mpin']):
        raise BrokerSetupError('Enter your six-digit Kotak MPIN.')
    secret = re.sub(r'\s', '', result['totp_secret']).upper().rstrip('=')
    if not re.fullmatch(r'[A-Z2-7]{16,128}', secret):
        raise BrokerSetupError('Enter the authenticator setup key, not a temporary six-digit code.')
    try:
        decoded = base64.b32decode(secret + '=' * (-len(secret) % 8))
    except (ValueError, binascii.Error):
        raise BrokerSetupError('The authenticator setup key is not valid Base32 text.') from None
    if not 10 <= len(decoded) <= 80:
        raise BrokerSetupError('The authenticator setup key has an unsupported length.')
    result['totp_secret'] = secret
    return result


class KotakSetupService:
    def __init__(self, factory=None, *, env=None):
        self.factory = factory or SessionLocal
        self.env = os.environ if env is None else env

    def _cipher(self):
        try:
            key = self.env.get('BROKER_CREDENTIAL_KEY', '')
            return Fernet(key.encode('ascii'))
        except (ValueError, TypeError, UnicodeError):
            raise BrokerSetupError('Private broker credential storage is not configured on the server.') from None

    def _decode(self, row):
        try:
            raw = self._cipher().decrypt(row.encrypted_payload.encode('ascii'))
            return _validated(json.loads(raw))
        except (InvalidToken, ValueError, TypeError, UnicodeError, KeyError):
            raise BrokerSetupError('Saved Kotak credentials could not be opened; check the server encryption key.') from None

    def status(self, db):
        row = db.get(PrivateBrokerCredential, 'kotak')
        try:
            self._cipher()
            encryption_ready = True
        except BrokerSetupError:
            encryption_ready = False
        fields = {field: bool(self.env.get(name)) for field, name in _FIELDS.items()}
        readable, error = None, None
        if row:
            try:
                saved = self._decode(row)
                readable = True
                fields = {field: bool(saved.get(field)) for field in _FIELDS}
            except BrokerSetupError as exc:
                readable, error = False, str(exc)
        configured = all(self.env.get(name) for name in _FIELDS.values())
        # An externally configured daily SID is also supported by the adapter.
        configured = configured or bool(self.env.get('KOTAK_SID') and self.env.get('KOTAK_UCC'))
        return {'broker': 'kotak', 'configured': bool(configured), 'stored': row is not None,
                'encryption_ready': encryption_ready, 'credentials_readable': readable,
                'fields_present': fields, 'updated_at': row.updated_at if row else None,
                'last_error': error}

    def _publish(self, values):
        # The adapter prioritizes SID and an explicit TOTP. Retire only those
        # stale Kotak values so the newly saved setup is actually used.
        for key in ('KOTAK_SID', 'KOTAK_TOTP'):
            self.env.pop(key, None)
        self.env.update({_FIELDS[key]: value for key, value in values.items()})

    def save(self, values):
        clean = _validated(values)
        with _SETUP_LOCK:
            encrypted = self._cipher().encrypt(json.dumps(clean, sort_keys=True, separators=(',', ':')).encode()).decode('ascii')
            def persist(db):
                row = db.get(PrivateBrokerCredential, 'kotak')
                if row:
                    row.encrypted_payload, row.updated_at = encrypted, iso_now()
                else:
                    db.add(PrivateBrokerCredential(broker='kotak', encrypted_payload=encrypted, updated_at=iso_now()))
            run_transaction(persist, session_factory=self.factory)
            # Publish only after the durable commit; failure keeps old runtime
            # credentials intact and lets the owner safely retry.
            self._publish(clean)
            with self.factory() as db:
                return self.status(db)

    def restore(self):
        with _SETUP_LOCK, self.factory() as db:
            row = db.get(PrivateBrokerCredential, 'kotak')
            if row:
                try:
                    self._publish(self._decode(row))
                except BrokerSetupError:
                    # Other app functions must remain available when broker
                    # authorization needs repair. status exposes a fixed error.
                    pass
            return self.status(db)
