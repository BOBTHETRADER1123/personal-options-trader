from sqlalchemy import String, Integer, Float, JSON, Text, UniqueConstraint
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

class ActionRecord(Base):
    __tablename__ = 'actions'
    key: Mapped[str] = mapped_column(String(150), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)

class SchemaVersion(Base):
    __tablename__ = 'schema_version'
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(50), default=iso_now)
