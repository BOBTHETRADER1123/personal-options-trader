"""One transactional journal shared by paper execution, feed capture and exports."""
import os
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
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
engine = create_engine(DATABASE_URL, pool_pre_ping=True,
    connect_args={'check_same_thread': False, 'timeout': 30} if DATABASE_URL.startswith('sqlite') else {})
def configure_sqlite(target):
    """Disable sqlite3 legacy transactions so SAVEPOINT release cannot commit early."""
    @event.listens_for(target, 'connect')
    def sqlite_pragmas(connection, _):
        connection.isolation_level=None
        connection.execute('PRAGMA journal_mode=WAL')
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
