"""One transactional journal shared by paper execution, feed capture and exports."""
import os
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.exc import (DBAPIError, DisconnectionError, InterfaceError,
                            OperationalError, SQLAlchemyError, TimeoutError)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / '.env')
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'OptionsTraderData')))
DATA.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{(DATA / "trader.db").as_posix()}')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
_pool_options = ({'pool_size': max(1, int(os.getenv('DB_POOL_SIZE', '2'))),
                  'max_overflow': max(0, int(os.getenv('DB_MAX_OVERFLOW', '1'))),
                  'pool_timeout': 10, 'pool_recycle': 300, 'pool_use_lifo': True}
                 if not DATABASE_URL.startswith('sqlite') else {})
engine = create_engine(DATABASE_URL, pool_pre_ping=True, **_pool_options,
    connect_args=({'check_same_thread': False, 'timeout': 30} if DATABASE_URL.startswith('sqlite') else
                  {'connect_timeout': 10, 'options': '-c statement_timeout=15000 -c lock_timeout=10000'}))
def configure_sqlite(target, journal_mode=None):
    """Disable sqlite3 legacy transactions so SAVEPOINT release cannot commit early."""
    mode=(journal_mode or os.getenv('SQLITE_JOURNAL_MODE','WAL')).upper()
    if mode not in {'WAL','DELETE'}:raise ValueError('Unsupported SQLite journal mode.')
    @event.listens_for(target, 'connect')
    def sqlite_pragmas(connection, _):
        connection.isolation_level=None
        # Opening a new connection must not wait thirty seconds just to read
        # the existing journal setting during an exclusive writer commit.
        connection.execute('PRAGMA busy_timeout=150')
        connection.execute('PRAGMA journal_mode='+mode)
        connection.execute('PRAGMA busy_timeout=30000')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA foreign_keys=ON')
    @event.listens_for(target, 'begin')
    def sqlite_begin(connection):
        connection.exec_driver_sql('BEGIN')

if DATABASE_URL.startswith('sqlite'):
    configure_sqlite(engine)

class Base(DeclarativeBase):
    pass

SessionLocal = sessionmaker(engine, expire_on_commit=False)

def sqlite_error_code(error):
    """Return the driver's extended code without exposing SQL or parameters."""
    original = getattr(error, 'orig', error)
    code = getattr(original, 'sqlite_errorcode', None)
    return code if isinstance(code, int) else None

def is_sqlite_contention(error):
    # WAL read-then-write conflicts report SQLITE_BUSY_SNAPSHOT (517), whose
    # base code is BUSY (5). Waiting inside that transaction cannot fix it.
    code = sqlite_error_code(error)
    return code is not None and code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

def is_database_unavailable(error):
    """Only connection/storage failures should take every API route offline."""
    if is_sqlite_contention(error):
        return False
    code = sqlite_error_code(error)
    if code is not None:
        return code & 0xff in {
            sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_FULL,
            sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PROTOCOL, sqlite3.SQLITE_NOTADB,
        }
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    return isinstance(error, (OperationalError, InterfaceError, DisconnectionError, TimeoutError))

def run_transaction(operation, session_factory=None, attempts=4):
    """Retry an entire SQLite write on a fresh snapshot, never a failed Session.

    Callers must keep irreversible work outside the operation. A successful
    receipt commit can be followed by a separately retried valuation without
    inserting the received price twice.
    """
    factory = session_factory or SessionLocal
    for attempt in range(attempts):
        with factory() as db:
            try:
                result = operation(db)
                db.commit()
                return result
            except SQLAlchemyError as error:
                # Invalidate the engine's in-memory marks as well as SQLite's
                # stale read snapshot before creating the replacement Session.
                db.rollback()
                state = getattr(getattr(error, 'orig', None), 'sqlstate', '') or ''
                retryable = (is_sqlite_contention(error) or state in {'40001', '40P01'} or
                             (db.get_bind().dialect.name == 'postgresql' and is_database_unavailable(error)))
                if not retryable or attempt + 1 == attempts:
                    raise
        time.sleep(.025 * (2 ** attempt))

def utcnow():
    return datetime.now(timezone.utc)

def iso_now():
    return utcnow().isoformat(timespec='microseconds')

def get_db():
    with SessionLocal() as db:
        try:
            yield db
        except Exception:
            db.rollback()
            raise


@contextmanager
def bounded_read(db, timeout_ms=150):
    """A short read-only stream/auth snapshot, ended before any async wait.

    Keep DELETE/FULL and the writer's normal busy timeout unchanged. Connection
    pragmas are restored before returning this checked-out connection to its pool.
    No failed write is retried here and no session/read lock crosses an await.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError('A stream read cannot contain pending changes.')
    connection=db.connection()
    sqlite=connection.dialect.name=='sqlite'
    previous_timeout=previous_query_only=None
    if sqlite:
        previous_timeout=connection.exec_driver_sql('PRAGMA busy_timeout').scalar()
        previous_query_only=connection.exec_driver_sql('PRAGMA query_only').scalar()
        connection.exec_driver_sql('PRAGMA busy_timeout='+str(int(timeout_ms)))
        connection.exec_driver_sql('PRAGMA query_only=ON')
    try:
        yield db
    finally:
        try:
            if sqlite and not connection.closed and not connection.invalidated:
                connection.exec_driver_sql('PRAGMA query_only='+str(int(previous_query_only)))
                connection.exec_driver_sql('PRAGMA busy_timeout='+str(int(previous_timeout)))
        finally:
            if db.in_transaction():
                if db.new or db.dirty or db.deleted:
                    db.rollback()
                    raise RuntimeError('A stream read attempted to modify trading state.')
                # Read commits retain the in-memory quote cache; rollbacks are
                # reserved for errors by the owning Session context/dependency.
                db.commit()
