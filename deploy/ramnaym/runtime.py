"""Production guards and query redaction; no app import or network side effects."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit


def validate_database_url(value: str):
    if value != 'sqlite:////app/data/trading.db':
        raise RuntimeError('This deployment requires SQLite at /app/data/trading.db; the old cloud database is not modified.')


def prepare_environment(root: Path):
    root = Path(root).resolve()
    if os.getenv("GUNICORN_WORKERS") != "1" or os.getenv("WEB_CONCURRENCY", "1") != "1":
        raise RuntimeError("A provider-supported single Gunicorn worker is required; application startup refused.")
    if os.getenv("DATA_DIR", "") != "/app/data":
        raise RuntimeError("Set DATA_DIR=/app/data and attach the confirmed persistent volume.")
    if (root / ".env").exists() or (root / "backend/.env").exists():
        raise RuntimeError("Private dotenv files must not be included in a cloud release.")
    os.environ.setdefault("DATABASE_URL","sqlite:////app/data/trading.db")
    validate_database_url(os.environ["DATABASE_URL"])
    os.environ.setdefault("SQLITE_JOURNAL_MODE","DELETE")
    if os.environ["SQLITE_JOURNAL_MODE"]!="DELETE":raise RuntimeError("Use the verified rollback journal mode for this deployment.")
    import sys
    sys.path.insert(0,str(root/"backend"))
    from app.storage import verify_storage
    proof=verify_storage("/app/data")
    from import_workspace import import_approved_workspace
    import_approved_workspace(root)
    os.environ["SQLITE_STORAGE_VERIFIED"]="true"
    os.environ["SQLITE_FILESYSTEM"]=proof["filesystem"]
    expected = {"ENVIRONMENT": "production", "COOKIE_SECURE": "true", "COOKIE_SAMESITE": "lax"}
    for name, value in expected.items():
        if name in os.environ and os.environ[name] != value:
            raise RuntimeError(f"Review private {name}; this deployment requires secure production sessions.")
        os.environ.setdefault(name, value)
    if os.getenv("EXPORT_ROOT", "").strip():
        raise RuntimeError("Disable EXPORT_ROOT for this deployment; report queues and daily exports use the managed /app/data directories.")
    if os.getenv("MARKET_CALENDAR_PATH", "").strip():
        raise RuntimeError("Remove the legacy MARKET_CALENDAR_PATH setting; use the packaged verified calendar.")
    calendar = root / "backend/calendars/exchange_sessions.json"
    if not calendar.is_file():
        raise RuntimeError("The verified exchange-session calendar is missing from this release.")
    configured_calendar = os.getenv("MARKET_CALENDAR_FILE", "").strip()
    if configured_calendar and Path(configured_calendar).resolve() != calendar:
        raise RuntimeError("MARKET_CALENDAR_FILE must select this release's maintained calendar.")
    origins = [part.strip().rstrip("/") for part in os.getenv("ALLOWED_ORIGINS", "").split(",") if part.strip()]
    if not origins:
        raise RuntimeError("Set ALLOWED_ORIGINS to the exact approved HTTPS website origins.")
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            valid = (parsed.scheme == "https" and bool(parsed.hostname) and "." in parsed.hostname
                     and "*" not in origin and parsed.username is None and parsed.password is None
                     and parsed.port in (None, 443) and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("ALLOWED_ORIGINS requires exact HTTPS origins without paths, credentials or wildcards.")
    os.environ["ALLOWED_ORIGINS"] = ",".join(origins)
    os.environ["MARKET_CALENDAR_FILE"] = str(calendar)
    os.environ.setdefault("INSTRUMENT_MASTER_PATH", "/app/data/instruments.json")
    os.environ.setdefault("DB_POOL_SIZE", "2")
    os.environ.setdefault("DB_MAX_OVERFLOW", "1")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"


_URL_QUERY = re.compile(r'((?:https?://[^\s?"<>]+|/[^\s?"<>]*))\?[^\s"<>]*')
_TOKEN_VALUE = re.compile(r'(?i)((?:access_token|refresh_token|client_secret|authorization_code|ticket|code)=)[^&\s"<>]+')


def redact_queries(value: str) -> str:
    return _TOKEN_VALUE.sub(r"\1[redacted]", _URL_QUERY.sub(r"\1?[redacted]", value))


class SensitiveQueryFilter(logging.Filter):
    """Cover HTTP access, websocket acceptance and exception log messages."""
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = redact_queries(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: ("[redacted]" if key == "q" else redact_queries(value) if isinstance(value, str) else value)
                           for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_queries(value) if isinstance(value, str) else value for value in record.args)
        return True


def install_safe_logging():
    for name in ("", "gunicorn.error", "gunicorn.access", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, SensitiveQueryFilter) for f in logger.filters):
            logger.addFilter(SensitiveQueryFilter())
        for handler in logger.handlers:
            if not any(isinstance(f, SensitiveQueryFilter) for f in handler.filters):
                handler.addFilter(SensitiveQueryFilter())


def port():
    try:
        value = int(os.getenv("PORT", "8000"))
    except ValueError:
        raise RuntimeError("PORT must be a valid TCP port.") from None
    if not 1 <= value <= 65535:
        raise RuntimeError("PORT must be a valid TCP port.")
    return value
