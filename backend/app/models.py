from sqlalchemy import String, Integer, BigInteger, Float, JSON, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base, iso_now

class Setting(Base):
    __tablename__ = 'settings'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)

class Owner(Base):
    __tablename__ = 'owners'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    password_hash: Mapped[str] = mapped_column(Text)

class UserSession(Base):
    __tablename__ = 'user_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[float] = mapped_column(Float)

class SocketTicket(Base):
    __tablename__ = 'socket_tickets'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_hash: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[float] = mapped_column(Float)

class Trade(Base):
    __tablename__ = 'trades'
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    broker: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    data: Mapped[dict] = mapped_column(JSON)

class Event(Base):
    __tablename__ = 'trade_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(60), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    timestamp: Mapped[str] = mapped_column(String(50), default=iso_now)
    data: Mapped[dict] = mapped_column(JSON)

class Timeline(Base):
    __tablename__ = 'timeline'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(60), index=True)
    timestamp: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict] = mapped_column(JSON)

class QuoteEvent(Base):
    __tablename__ = 'quote_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker: Mapped[str] = mapped_column(String(30), index=True)
    instrument_id: Mapped[str] = mapped_column(String(150), index=True)
    received_at: Mapped[str] = mapped_column(String(50), default=iso_now, index=True)
    data: Mapped[dict] = mapped_column(JSON)

class PendingQuote(Base):
    __tablename__ = 'pending_quote_processing'
    quote_id: Mapped[int] = mapped_column(Integer,primary_key=True)
    created_at: Mapped[str] = mapped_column(String(50),default=iso_now)

class LatestQuote(Base):
    """One durable current mark per broker contract, rather than tick history."""
    __tablename__ = 'latest_quotes'
    broker: Mapped[str] = mapped_column(String(30), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(150), primary_key=True)
    received_at: Mapped[str] = mapped_column(String(50), index=True)
    data: Mapped[dict] = mapped_column(JSON)

class QuoteReceiptCounter(Base):
    """Receipt IDs must never repeat after compact receipts are discarded."""
    __tablename__ = 'quote_receipt_counter'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int] = mapped_column(Integer)

class CompactQuoteReceipt(Base):
    """Only new compact receipts may be discarded; legacy journals stay intact."""
    __tablename__ = 'compact_quote_receipts'
    quote_id: Mapped[int] = mapped_column(Integer, primary_key=True)

class ActionRecord(Base):
    __tablename__ = 'actions'
    key: Mapped[str] = mapped_column(String(150), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)

class SchemaVersion(Base):
    __tablename__ = 'schema_version'
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(50), default=iso_now)


class PnlSnapshot(Base):
    """One captured observation per active trade and UTC second, never backfilled.

    The composite primary key is the only high-volume index. The JSON contains
    broker-specific contract identities and the original quote timestamps.
    """
    __tablename__ = 'pnl_snapshots'
    trade_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    snapshot_second: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timestamp: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict] = mapped_column(JSON)


class RecordingCursor(Base):
    __tablename__ = 'recording_cursors'
    trade_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    last_second: Mapped[int] = mapped_column(BigInteger)
    last_quote_ids: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String(50))


class RecordingGap(Base):
    """Explicit missing intervals; these rows never stand in for observations."""
    __tablename__ = 'recording_gaps'
    trade_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    start_second: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    end_second: Mapped[int] = mapped_column(BigInteger)
    detected_at: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON)


class SchedulerLease(Base):
    """Transactional fencing lease; works through transaction-pooled PgBouncer."""
    __tablename__ = 'scheduler_leases'
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[float] = mapped_column(Float)
