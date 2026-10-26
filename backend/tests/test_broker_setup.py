"""Kotak setup never stores plaintext or sends a broker request."""
import json
from copy import deepcopy

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, configure_sqlite
from app.broker_setup import BrokerSetupError, KotakSetupService, PrivateBrokerCredential


@pytest.fixture
def factory(tmp_path):
    sql = create_engine(f'sqlite:///{tmp_path / "kotak-private.db"}')
    configure_sqlite(sql, journal_mode='DELETE')
    Base.metadata.create_all(sql)
    yield sessionmaker(sql, expire_on_commit=False)
    sql.dispose()


@pytest.fixture
def env():
    return {'BROKER_CREDENTIAL_KEY': Fernet.generate_key().decode(), 'UNRELATED_SECRET': 'preserve-this'}


VALUES = {'consumer_key': 'private-application-key-123456', 'mobile_number': '9876543210',
          'ucc': 'OWNER123', 'mpin': '123456', 'totp_secret': 'JBSWY3DPEHPK3PXP'}


def test_saved_values_are_encrypted_and_status_has_no_secrets(factory, env):
    service = KotakSetupService(factory, env=env)
    status = service.save(VALUES)
    assert status['stored'] and status['configured'] and status['credentials_readable']
    assert all(status['fields_present'].values())
    with factory() as db:
        row = db.get(PrivateBrokerCredential, 'kotak')
        assert not any(value in row.encrypted_payload for value in VALUES.values())
        clean = json.loads(Fernet(env['BROKER_CREDENTIAL_KEY']).decrypt(row.encrypted_payload.encode()))
        assert clean['mobile_number'] == '+919876543210'
    encoded_status = json.dumps(status)
    assert not any(value in encoded_status for value in VALUES.values())
    assert env['UNRELATED_SECRET'] == 'preserve-this'


def test_restart_restores_only_allowlisted_kotak_environment(factory, env):
    service = KotakSetupService(factory, env=env)
    service.save(VALUES)
    fresh = {'BROKER_CREDENTIAL_KEY': env['BROKER_CREDENTIAL_KEY'], 'UNRELATED_SECRET': 'retained',
             'KOTAK_SID': 'expired-sid', 'KOTAK_TOTP': '000000'}
    status = KotakSetupService(factory, env=fresh).restore()
    assert status['configured'] and fresh['KOTAK_MPIN'] == VALUES['mpin']
    assert fresh['KOTAK_TOTP_SECRET'] == VALUES['totp_secret']
    assert fresh['UNRELATED_SECRET'] == 'retained'
    assert 'KOTAK_SID' not in fresh and 'KOTAK_TOTP' not in fresh
    assert set(fresh) == {'BROKER_CREDENTIAL_KEY', 'UNRELATED_SECRET', 'KOTAK_CONSUMER_KEY',
                          'KOTAK_UCC', 'KOTAK_MOBILE_NUMBER', 'KOTAK_MPIN', 'KOTAK_TOTP_SECRET'}


def test_replacement_updates_same_row(factory, env):
    service = KotakSetupService(factory, env=env)
    service.save(VALUES)
    service.save({**VALUES, 'consumer_key': 'new-private-key-654321'})
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PrivateBrokerCredential)) == 1
    assert env['KOTAK_CONSUMER_KEY'] == 'new-private-key-654321'


@pytest.mark.parametrize('change', [
    {'extra': 'private-value'}, {'mpin': 'wrong-secret-pin'}, {'mpin': '1234'},
    {'totp_secret': '123456'}, {'totp_secret': 'otpauth://totp/secret'},
    {'consumer_key': 'bad key'}, {'mobile_number': '+447700900001'},
    {'mobile_number': '12345'}, {'ucc': 'private/client'}, {'ucc': 1234},
])
def test_invalid_fields_never_write_or_publish(factory, env, change):
    before = deepcopy(env)
    service = KotakSetupService(factory, env=env)
    with pytest.raises(BrokerSetupError) as caught:
        service.save({**VALUES, **change})
    assert 'private-value' not in str(caught.value) and 'wrong-secret-pin' not in str(caught.value)
    assert env == before
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PrivateBrokerCredential)) == 0


def test_missing_fields_are_rejected(factory, env):
    with pytest.raises(BrokerSetupError):
        KotakSetupService(factory, env=env).save({'consumer_key': 'partial-key'})


def test_secret_normalization_keeps_totp_seed_not_one_time_code(factory, env):
    service = KotakSetupService(factory, env=env)
    service.save({**VALUES, 'totp_secret': 'jbsw y3dp ehpk 3pxp', 'mobile_number': '+919876543210'})
    assert env['KOTAK_TOTP_SECRET'] == VALUES['totp_secret']


def test_missing_encryption_key_does_not_store_plaintext(factory):
    service = KotakSetupService(factory, env={})
    with pytest.raises(BrokerSetupError, match='storage is not configured'):
        service.save(VALUES)
    with factory() as db:
        assert not service.status(db)['encryption_ready']
        assert db.get(PrivateBrokerCredential, 'kotak') is None


def test_wrong_key_and_corruption_leave_other_functions_available(factory, env):
    KotakSetupService(factory, env=env).save(VALUES)
    wrong_env = {'BROKER_CREDENTIAL_KEY': Fernet.generate_key().decode()}
    status = KotakSetupService(factory, env=wrong_env).restore()
    assert status['stored'] and not status['credentials_readable'] and not status['configured']
    assert 'could not be opened' in status['last_error']
    assert len(wrong_env) == 1
    with factory() as db:
        row = db.get(PrivateBrokerCredential, 'kotak')
        row.encrypted_payload = 'damaged-private-token'
        db.commit()
    status = KotakSetupService(factory, env=env).restore()
    assert not status['credentials_readable']
    assert 'damaged-private-token' not in json.dumps(status)


def test_database_failure_keeps_previous_runtime_credentials(factory, env, monkeypatch):
    service = KotakSetupService(factory, env=env)
    service.save(VALUES)
    before = deepcopy(env)
    def fail(*args, **kwargs):
        raise RuntimeError('simulated durable commit failure')
    monkeypatch.setattr('app.broker_setup.run_transaction', fail)
    with pytest.raises(RuntimeError):
        service.save({**VALUES, 'consumer_key': 'new-private-key-654321'})
    assert env == before
    with factory() as db:
        stored = json.loads(Fernet(env['BROKER_CREDENTIAL_KEY']).decrypt(db.get(PrivateBrokerCredential, 'kotak').encrypted_payload.encode()))
        assert stored['consumer_key'] == VALUES['consumer_key']


def test_encrypted_payload_cannot_inject_environment_fields(factory, env):
    payload = {**VALUES, 'OTHER_SERVICE_TOKEN': 'injected'}
    encrypted = Fernet(env['BROKER_CREDENTIAL_KEY']).encrypt(json.dumps(payload).encode()).decode()
    with factory() as db:
        db.add(PrivateBrokerCredential(broker='kotak', encrypted_payload=encrypted, updated_at='2026-10-26T00:00:00+00:00'))
        db.commit()
    status = KotakSetupService(factory, env=env).restore()
    assert not status['configured'] and not status['credentials_readable']
    assert 'OTHER_SERVICE_TOKEN' not in env


def test_empty_store_does_not_change_environment(factory, env):
    before = deepcopy(env)
    status = KotakSetupService(factory, env=env).restore()
    assert not status['stored'] and not status['configured']
    assert env == before
