"""A WAL writer collision is retryable, not a disconnected workspace.

These tests use two real SQLite connections and a private temporary database.
They do not open or modify the installed user's journal.
"""
import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError
from sqlalchemy.orm import sessionmaker

from app import database, engine, main, reporting
from app.database import Base, configure_sqlite, get_db, iso_now
from app.instruments import ContractCatalog
from app.models import QuoteReceiptCounter, Setting, Trade


@pytest.fixture
def journal(tmp_path):
    sql = create_engine(f'sqlite:///{(tmp_path / "contention.db").as_posix()}',
                        connect_args={'check_same_thread': False, 'timeout': .1})
    configure_sqlite(sql)
    Base.metadata.create_all(sql)
    factory = sessionmaker(sql, expire_on_commit=False)
    with factory() as db:
        db.add(Setting(key='probe', value={'writes': 0}))
        db.commit()
    yield factory
    engine.clear_cache()
    sql.dispose()


def change_from_another_connection(factory):
    with factory() as other:
        before = other.get(Setting, 'probe').value['writes']
        other.execute(update(Setting).where(Setting.key == 'probe')
                      .values(value={'writes': before + 1}))
        other.commit()


def busy_snapshot(factory):
    with factory() as first:
        first.get(Setting, 'probe')  # A real WAL read snapshot now exists.
        change_from_another_connection(factory)
        with pytest.raises(OperationalError) as error:
            first.execute(update(Setting).where(Setting.key == 'probe')
                          .values(value={'writes': -1}))
        first.rollback()
    return error.value


def test_real_wal_snapshot_error_is_classified_as_contention(journal):
    error = busy_snapshot(journal)
    assert database.sqlite_error_code(error) == sqlite3.SQLITE_BUSY_SNAPSHOT
    assert database.is_sqlite_contention(error)
    assert not database.is_database_unavailable(error)
    with journal() as db:
        assert db.get(Setting, 'probe').value == {'writes': 1}


def test_transaction_retry_uses_fresh_session_and_rechecks_committed_data(journal):
    attempts = []
    def operation(db):
        attempts.append(db)
        value = db.get(Setting, 'probe').value['writes']
        if len(attempts) == 1:
            change_from_another_connection(journal)
        db.execute(update(Setting).where(Setting.key == 'probe')
                   .values(value={'writes': value + 10}))
        return value
    assert database.run_transaction(operation, journal) == 1
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    with journal() as db:
        # Retrying only UPDATE on the old snapshot would fail again or lose
        # the other transaction's +1; the complete operation must be replayed.
        assert db.get(Setting, 'probe').value == {'writes': 11}


def test_contention_retry_is_bounded_and_rolls_back_every_failed_attempt(journal, monkeypatch):
    calls = []
    monkeypatch.setattr(database.time, 'sleep', lambda _: None)
    def operation(db):
        calls.append(db)
        db.get(Setting, 'probe')
        change_from_another_connection(journal)
        db.execute(update(Setting).where(Setting.key == 'probe')
                   .values(value={'writes': -1}))
    with pytest.raises(OperationalError) as error:
        database.run_transaction(operation, journal)
    assert database.is_sqlite_contention(error.value) and len(calls) == 4
    with journal() as db:
        assert db.get(Setting, 'probe').value == {'writes': 4}


def test_capture_replays_real_price_transaction_after_wal_conflict_and_keeps_api_ready(journal, tmp_path, monkeypatch):
    original_catalog = engine.catalog
    catalog = ContractCatalog(tmp_path / 'no-live-master.json')
    engine.set_catalog(catalog)
    monkeypatch.setattr(main, 'SessionLocal', journal)
    monkeypatch.setattr(main, 'database_ready', True)
    try:
        contract = catalog.list_contracts(underlying='NIFTY', broker='kotak', demo=True)[0]
        with journal() as db:
            engine.update_settings(db, {'logging_mode': 'summary', 'demo_mode': True,
                                        'fill_basis': 'ltp', 'brokerage_per_order': 0, 'charges_percent': 0})
            engine.record_quote(db, {**main.demo_quote(contract, 'kotak'), 'received_at': iso_now()})
            result = engine.create_trades(db, {
                'name': 'WAL recovery paper trade', 'underlying': 'NIFTY', 'brokers': ['kotak'],
                'legs': [{'instrument_id': contract['id'], 'side': 'BUY', 'lots': 1,
                          'order_type': 'MARKET', 'enabled': True}],
                'idempotency_key': 'wal-capture-trade', 'confirmed': True,
            })
            trade_id = result['trades'][0]['id']
            engine.record_quote(db, main.demo_quote(contract, 'kotak'))
            db.commit()
            before = db.get(QuoteReceiptCounter, 1).value

        recover = engine.recover_pending_quotes
        captures = []
        def interleaved_recovery(db):
            captures.append(db)
            count = recover(db)  # SELECT establishes this attempt's snapshot.
            if len(captures) == 1:
                change_from_another_connection(journal)
            return count
        monkeypatch.setattr(engine, 'recover_pending_quotes', interleaved_recovery)
        capture = main.capture_once
        def run_one_capture():
            capture()
            main.stop_event.set()
        monkeypatch.setattr(main, 'capture_once', run_one_capture)
        async def run():
            monkeypatch.setattr(main, 'stop_event', asyncio.Event())
            monkeypatch.setattr(main, 'write_lock', asyncio.Lock())
            await asyncio.wait_for(main.background_loop(), timeout=3)
        asyncio.run(run())
        assert len(captures) == 2 and captures[0] is not captures[1]
        assert main.database_ready is True
        with journal() as db:
            assert db.get(QuoteReceiptCounter, 1).value == before + 1
            assert db.get(Trade, trade_id).data['_last_quote_event_id'] == before + 1
        def dependency():
            with journal() as db:
                yield db
        main.app.dependency_overrides[get_db] = dependency
        client = TestClient(main.app)
        assert client.get('/api/health').json()['database_ready'] is True
        assert client.get('/api/session').status_code == 200
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        engine.set_catalog(original_catalog)


@pytest.mark.parametrize('kind', ['integrity', 'statement', 'contention', 'outage'])
def test_api_error_policy_keeps_statement_failures_local_and_never_leaks_values(journal, monkeypatch, caplog, kind):
    secret = 'session-and-password-values-must-not-leak'
    if kind == 'integrity':
        error = IntegrityError(f'SQL {secret}', {'password': secret}, sqlite3.IntegrityError(secret))
    elif kind == 'statement':
        error = StatementError(secret, f'SQL {secret}', {'token': secret}, ValueError(secret))
    elif kind == 'contention':
        error = busy_snapshot(journal)
    else:
        error = OperationalError(f'SQL {secret}', {'token': secret}, OSError(secret))
    monkeypatch.setattr(main, 'database_ready', True)
    def unavailable_db():
        raise error
        yield
    main.app.dependency_overrides[get_db] = unavailable_db
    try:
        client = TestClient(main.app)
        response = client.get('/api/session')
        expected = 503 if kind in {'contention', 'outage'} else 500
        assert response.status_code == expected
        assert secret not in response.text and secret not in caplog.text
        assert main.database_ready is (kind != 'outage')
        assert client.get('/api/health').status_code == 200
        if kind == 'contention':
            assert response.headers['retry-after'] == '1'
        if kind == 'outage':
            assert response.headers['retry-after'] == str(main.DATABASE_RETRY_SECONDS)
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_exhausted_capture_contention_does_not_pause_all_workers(journal, monkeypatch):
    error = busy_snapshot(journal)
    calls = []
    def collision():
        calls.append(True)
        main.stop_event.set()
        raise error
    async def run():
        monkeypatch.setattr(main, 'stop_event', asyncio.Event())
        monkeypatch.setattr(main, 'write_lock', asyncio.Lock())
        await asyncio.wait_for(main.background_loop(), timeout=1)
    monkeypatch.setattr(main, 'database_ready', True)
    monkeypatch.setattr(main, 'capture_once', collision)
    asyncio.run(run())
    assert calls == [True] and main.database_ready is True


def test_received_quote_is_not_inserted_twice_when_valuation_snapshot_retries(journal, monkeypatch):
    receipt_ids, valuation_sessions = [], []
    def receive(db, quote, process):
        assert process is False
        db.execute(update(Setting).where(Setting.key == 'probe').values(value={'writes': 10}))
        receipt_ids.append(42)
        return 42
    def revalue(db, receipt):
        valuation_sessions.append(db)
        value = db.get(Setting, 'probe').value['writes']
        if len(valuation_sessions) == 1:
            change_from_another_connection(journal)
        db.execute(update(Setting).where(Setting.key == 'probe').values(value={'writes': value + 100}))
    monkeypatch.setattr(main, 'SessionLocal', journal)
    monkeypatch.setattr(engine, 'record_quote', receive)
    monkeypatch.setattr(engine, 'revalue_quote', revalue)
    main.process_quote_receipt({'broker': 'kotak', 'instrument_id': 'private-test'})
    assert receipt_ids == [42] and len(valuation_sessions) == 2
    with journal() as db:
        assert db.get(Setting, 'probe').value == {'writes': 111}


def test_daily_reconciliation_retries_fresh_wal_snapshot_and_keeps_current_counts(journal, monkeypatch):
    with journal() as db:
        db.add(reporting.ExportJob(id='wal-report', trade_id='closed-wal-trade', broker='kotak',
                                   filename='verified-paper-trade.xlsx', relative_path='private-test.xlsx',
                                   payload={'format': 'summary', 'trade': {'demo': True}}, status='pending'))
        db.commit()
    initial = reporting.save_daily_reconciliation(journal)
    assert initial['pending'] == 1 and initial['saved'] == 0
    reconcile = reporting.reconciliation
    attempts = []
    def interrupted_reconciliation(db):
        attempts.append(db)
        report = reconcile(db)
        if len(attempts) == 1:
            # Reproduce the concurrent companion/report writer, including an
            # update to the very daily row this stale reader will try to save.
            with journal() as other:
                other.execute(update(reporting.ExportJob).where(reporting.ExportJob.id == 'wal-report')
                              .values(status='saved', verification_status='verified'))
                other.execute(update(reporting.DailyExportReconciliation)
                              .where(reporting.DailyExportReconciliation.date == report['date'])
                              .values(data={'concurrent_writer': True}))
                other.commit()
        return report
    monkeypatch.setattr(reporting, 'reconciliation', interrupted_reconciliation)
    result = reporting.save_daily_reconciliation(journal)
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    assert result['total'] == 1 and result['saved'] == 1 and result['pending'] == 0
    assert result['mismatches'] == []
    with journal() as db:
        daily = db.get(reporting.DailyExportReconciliation, result['date'])
        assert daily.data == result and daily.generated_at == result['generated_at']
