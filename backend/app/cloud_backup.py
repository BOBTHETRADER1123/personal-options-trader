"""Owner-authorized, server-side Google Drive archives.

PostgreSQL is authoritative. The persistent volume holds a bounded staging cache,
never the sole trade journal. This module contains no record-deletion operation.
All Drive requests use the owner's explicitly configured OAuth client; Codex's
connected Google account is deliberately unrelated to this application.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Boolean, Float, Integer, JSON, String, Text, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, SessionLocal, iso_now

log = logging.getLogger(__name__)
SCOPE = "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,parents,size,md5Checksum,sha256Checksum,trashed,appProperties"
IST = ZoneInfo("Asia/Kolkata")


class CloudDriveCredential(Base):
    __tablename__ = "cloud_drive_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text)
    connected_at: Mapped[str] = mapped_column(String(40), default=iso_now)
    auth_required: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class CloudDriveOAuthState(Base):
    __tablename__ = "cloud_drive_oauth_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_hash: Mapped[str] = mapped_column(String(64))
    encrypted_verifier: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class CloudBackupFolder(Base):
    """Reserve a Drive ID before creating a folder, including interrupted calls."""
    __tablename__ = "cloud_backup_folders"
    path: Mapped[str] = mapped_column(String(240), primary_key=True)
    drive_id: Mapped[str] = mapped_column(String(150))


class CloudBackupJob(Base):
    __tablename__ = "cloud_backup_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(80), index=True)
    source_checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[float] = mapped_column(Float, default=0)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=iso_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=iso_now)
    verified_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class BackupError(Exception):
    """Only fixed safe messages cross the API/logging boundary."""


class AuthorizationRequired(BackupError):
    pass


class AccessTokenExpired(BackupError):
    pass


@dataclass(frozen=True)
class DriveConfig:
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    encryption_key: str = ""
    data_dir: str = "/app/data"
    max_spool_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    min_free_bytes: int = 512 * 1024 * 1024
    max_pending_jobs: int = 1000
    interval_seconds: int = 60

    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.getenv("GOOGLE_DRIVE_BACKUP_ENABLED", "false").lower() == "true",
            client_id=os.getenv("GOOGLE_DRIVE_CLIENT_ID", ""),
            client_secret=os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", ""),
            redirect_uri=os.getenv("GOOGLE_DRIVE_REDIRECT_URI", ""),
            encryption_key=os.getenv("GOOGLE_DRIVE_TOKEN_KEY", ""),
            data_dir=os.getenv("DATA_DIR", "/app/data"),
            max_spool_bytes=max(1, int(os.getenv("DRIVE_SPOOL_MAX_MB", "256"))) * 1024 * 1024,
            max_file_bytes=max(1, int(os.getenv("DRIVE_MAX_FILE_MB", "64"))) * 1024 * 1024,
            min_free_bytes=max(0, int(os.getenv("DRIVE_MIN_FREE_MB", "512"))) * 1024 * 1024,
            max_pending_jobs=max(1, int(os.getenv("DRIVE_MAX_PENDING_JOBS", "1000"))),
            interval_seconds=max(15, int(os.getenv("DRIVE_BACKUP_INTERVAL_SECONDS", "60"))),
        )

    def validate(self):
        if not all((self.client_id, self.client_secret, self.redirect_uri, self.encryption_key)):
            raise BackupError("Google Drive server configuration is incomplete.")
        uri = urlparse(self.redirect_uri)
        if uri.scheme != "https" or not uri.hostname or uri.username or uri.fragment:
            raise BackupError("Google Drive callback must be an HTTPS application URL.")
        try:
            Fernet(self.encryption_key.encode())
        except (ValueError, TypeError):
            raise BackupError("Google Drive encryption key is invalid.") from None

    def encrypt(self, value: str) -> str:
        self.validate()
        return Fernet(self.encryption_key.encode()).encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        self.validate()
        try:
            return Fernet(self.encryption_key.encode()).decrypt(value.encode()).decode()
        except (InvalidToken, ValueError, UnicodeError):
            raise AuthorizationRequired("Google Drive authorization must be renewed.") from None


def begin_authorization(db, session_hash: str, config: DriveConfig | None = None) -> str:
    """Call only from an owner-authenticated, CSRF-protected POST endpoint.

    The callback must supply the same authenticated session's token_hash. A
    short-lived state and PKCE verifier are one-use and encrypted at rest.
    """
    config = config or DriveConfig.from_env()
    config.validate()
    if not session_hash:
        raise AuthorizationRequired("Sign in before connecting Google Drive.")
    raw_state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    db.add(CloudDriveOAuthState(state_hash=hashlib.sha256(raw_state.encode()).hexdigest(),
        session_hash=session_hash, encrypted_verifier=config.encrypt(verifier),
        expires_at=time.time() + 600, used=False))
    db.commit()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": config.client_id, "redirect_uri": config.redirect_uri,
        "response_type": "code", "scope": SCOPE, "state": raw_state,
        "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "true",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _token_request(client: httpx.Client, data: dict) -> dict:
    try:
        response = client.post(TOKEN_URL, data=data)
    except httpx.HTTPError:
        raise BackupError("Google authorization is temporarily unavailable.") from None
    if response.status_code in (400, 401, 403):
        raise AuthorizationRequired("Google Drive authorization must be renewed.")
    if response.status_code != 200:
        raise BackupError("Google authorization is temporarily unavailable.")
    try:
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("access_token"), str):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise BackupError("Google authorization returned an invalid response.") from None


def complete_authorization(db, session_hash: str, state: str, code: str,
                           config: DriveConfig | None = None, client: httpx.Client | None = None) -> dict:
    """Authenticated callback. Never return tokens or reflect Google's errors."""
    config = config or DriveConfig.from_env()
    config.validate()
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    pending = db.get(CloudDriveOAuthState, state_hash)
    if not pending or not hmac.compare_digest(pending.session_hash, session_hash) or pending.used or pending.expires_at < time.time():
        raise AuthorizationRequired("Google Drive connection request expired. Start again.")
    claimed = db.execute(update(CloudDriveOAuthState).where(
        CloudDriveOAuthState.state_hash == state_hash, CloudDriveOAuthState.used == False,
        CloudDriveOAuthState.expires_at >= time.time()).values(used=True))
    db.commit()
    if claimed.rowcount != 1:
        raise AuthorizationRequired("Google Drive connection request expired. Start again.")
    verifier = config.decrypt(pending.encrypted_verifier)
    own_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        token = _token_request(client, {"client_id": config.client_id, "client_secret": config.client_secret,
            "code": code, "grant_type": "authorization_code", "redirect_uri": config.redirect_uri,
            "code_verifier": verifier})
        if token.get("scope") and not set(SCOPE.split()).issubset(str(token["scope"]).split()):
            raise AuthorizationRequired("Google Drive file permission was not granted.")
        previous = db.get(CloudDriveCredential, 1)
        refresh = token.get("refresh_token")
        if not refresh:
            raise AuthorizationRequired("Offline access was not granted. Reconnect with consent.")
        if previous:
            previous.encrypted_refresh_token = config.encrypt(refresh)
            previous.connected_at, previous.auth_required, previous.last_error = iso_now(), False, None
        else:
            db.add(CloudDriveCredential(id=1, encrypted_refresh_token=config.encrypt(refresh), auth_required=False))
        db.commit()
        return {"connected": True, "offline_access": True}
    finally:
        if own_client:
            client.close()


class DriveClient:
    """Small official Drive REST client with no implicit request retries.

    Durable job retry owns retry timing. A failed upload is inspected by its
    pre-generated file ID before it can be tried again.
    """
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)
        self.owns_client = client is None
        self.headers = {"Authorization": f"Bearer {token}"}

    def close(self):
        if self.owns_client:
            self.client.close()

    def request(self, method: str, url: str, **kwargs):
        headers = {**self.headers, **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise BackupError("Google Drive connection failed; archive retained for retry.") from None
        if response.status_code == 401:
            raise AccessTokenExpired("Google Drive access needs a secure refresh; archive queued for retry.")
        if response.status_code in (404, 409):
            return response
        if not 200 <= response.status_code < 300:
            raise BackupError(f"Google Drive request failed (HTTP {response.status_code}); archive retained.")
        return response

    def metadata(self, file_id: str) -> dict | None:
        response = self.request("GET", f"{DRIVE}/files/{file_id}", params={"fields": FILE_FIELDS})
        return None if response.status_code == 404 else response.json()

    def generate_id(self) -> str:
        response = self.request("GET", f"{DRIVE}/files/generateIds", params={"count": 1, "space": "drive", "type": "files"})
        return response.json()["ids"][0]

    def find(self, parent: str | None, key: str) -> list[dict]:
        # key is generated locally, never a user expression or arbitrary query.
        if not all(c in "0123456789abcdef" for c in key):
            raise BackupError("Invalid archive lookup key.")
        query = f"trashed = false and appProperties has {{ key='pot_backup_key' and value='{key}' }}"
        if parent:
            if not all(c.isalnum() or c in "_-" for c in parent):
                raise BackupError("Invalid Drive parent identifier.")
            query += f" and '{parent}' in parents"
        found, page = [], None
        while True:
            params = {"q": query, "spaces": "drive", "fields": f"nextPageToken,files({FILE_FIELDS})", "pageSize": 100}
            if page:
                params["pageToken"] = page
            data = self.request("GET", f"{DRIVE}/files", params=params).json()
            found.extend(data.get("files", []))
            page = data.get("nextPageToken")
            if not page:
                return found
            if len(found) >= 1000:
                raise BackupError("Too many duplicate Drive records; review is required.")

    def create_folder(self, file_id: str, name: str, parent: str | None, key: str):
        body = {"id": file_id, "name": name, "mimeType": FOLDER_MIME, "appProperties": {"pot_backup_key": key}}
        if parent:
            body["parents"] = [parent]
        self.request("POST", f"{DRIVE}/files", json=body, params={"fields": FILE_FIELDS})
        return self.metadata(file_id)

    def upload(self, file_id: str, name: str, parent: str, mime: str, key: str, content: bytes):
        metadata = {"id": file_id, "name": name, "parents": [parent], "mimeType": mime,
                    "appProperties": {"pot_backup_key": key, "pot_sha256": hashlib.sha256(content).hexdigest()}}
        response = self.request("POST", UPLOAD, params={"uploadType": "resumable", "fields": FILE_FIELDS},
            headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(content))}, json=metadata)
        if response.status_code == 409:
            return self.metadata(file_id)
        location = response.headers.get("location", "")
        parsed = urlparse(location)
        if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com" or parsed.username or parsed.port not in (None, 443):
            raise BackupError("Google Drive returned an invalid upload destination.")
        self.request("PUT", location, content=content, headers={"Content-Type": mime, "Content-Length": str(len(content))})
        return self.metadata(file_id)

    def download_digest(self, file_id: str, max_bytes: int) -> dict:
        # Only used if Drive omits MD5 metadata. The streamed read is bounded.
        md5, sha, size = hashlib.md5(), hashlib.sha256(), 0
        try:
            with self.client.stream("GET", f"{DRIVE}/files/{file_id}", params={"alt": "media"}, headers=self.headers) as response:
                if response.status_code == 401:
                    raise AccessTokenExpired("Google Drive access needs a secure refresh; archive queued for retry.")
                if response.status_code != 200:
                    raise BackupError("Google Drive verification download failed.")
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise BackupError("Google Drive verification exceeded expected size.")
                    md5.update(chunk)
                    sha.update(chunk)
        except httpx.HTTPError:
            raise BackupError("Google Drive verification download failed.") from None
        return {"size": size, "md5": md5.hexdigest(), "sha256": sha.hexdigest()}


def file_key(job_id: str, artifact: dict) -> str:
    return hashlib.sha256(f"{job_id}:{artifact['kind']}:{artifact['sha256']}".encode()).hexdigest()


def verify_remote(remote: dict | None, artifact: dict, parent: str, client: DriveClient) -> dict:
    if (not remote or remote.get("trashed") or remote.get("name") != artifact["filename"]
        or remote.get("mimeType") != artifact["mime_type"] or remote.get("parents") != [parent]
        or str(remote.get("size")) != str(artifact["size"])):
        raise BackupError("Google Drive archive metadata does not match; local records retained.")
    if remote.get("md5Checksum"):
        if not hmac.compare_digest(remote["md5Checksum"], artifact["md5"]):
            raise BackupError("Google Drive archive checksum does not match; local records retained.")
        if remote.get("sha256Checksum") and not hmac.compare_digest(remote["sha256Checksum"], artifact["sha256"]):
            raise BackupError("Google Drive archive checksum does not match; local records retained.")
        verification = "metadata_md5"
    else:
        digest = client.download_digest(remote["id"], artifact["size"])
        if any(str(digest[k]) != str(artifact[k]) for k in ("size", "md5", "sha256")):
            raise BackupError("Google Drive downloaded archive does not match; local records retained.")
        verification = "download_md5_sha256"
    return {"drive_id": remote["id"], "parent_id": parent, "verification": verification,
            "verified_at": iso_now(), "md5": artifact["md5"], "sha256": artifact["sha256"],
            "size": artifact["size"], "mime_type": artifact["mime_type"], "filename": artifact["filename"],
            "kind": artifact["kind"]}


class BoundedSpool:
    def __init__(self, config: DriveConfig):
        self.config = config
        self.root = Path(config.data_dir).expanduser().resolve() / "cloud-backup-spool"

    def write(self, key: str, content: bytes) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise BackupError("Invalid backup staging identifier.")
        if len(content) > self.config.max_file_bytes:
            raise BackupError("Archive exceeds the configured staging limit; database copy retained.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise BackupError("Backup staging directory must not be a symbolic link.")
        path = self.root / f"{key}.staged"
        if path.is_symlink():
            raise BackupError("Invalid backup staging file.")
        if path.exists() and path.read_bytes() == content:
            return path
        usage = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file() and not p.is_symlink())
        if usage + len(content) > self.config.max_spool_bytes or shutil.disk_usage(self.root).free < self.config.min_free_bytes + len(content):
            raise BackupError("Backup staging storage is full; database records retained.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False, prefix="staging-", suffix=".tmp") as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return path

    def discard_verified_staging(self, key: str):
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise BackupError("Invalid backup staging identifier.")
        path = self.root / f"{key}.staged"
        if not path.is_symlink():
            path.unlink(missing_ok=True)


def _default_artifacts(db, job_id: str) -> list[dict]:
    from .reporting import get_export_artifacts
    return get_export_artifacts(db, job_id)


class CloudBackupService:
    def __init__(self, config: DriveConfig | None = None, session_factory=SessionLocal,
                 artifacts_provider: Callable = _default_artifacts, client_factory: Callable = DriveClient,
                 token_client: httpx.Client | None = None):
        self.config = config or DriveConfig.from_env()
        self.factory, self.artifacts_provider, self.client_factory = session_factory, artifacts_provider, client_factory
        self.token_client = token_client
        self.spool = BoundedSpool(self.config)
        self.last_error = None
        self.last_success = None
        self._token, self._expires = None, 0
        self._wake_event = None
        self._wake_loop = None

    def notify_work(self):
        """Wake on report-ready, owner queue/retry, or completed OAuth consent.

        Safe when reporting ran inside asyncio.to_thread. No database query is
        made here; an idle worker otherwise scans at most once per ten minutes.
        """
        if self._wake_loop and self._wake_event:
            try:
                self._wake_loop.call_soon_threadsafe(self._wake_event.set)
            except RuntimeError:
                pass  # Application is shutting down; durable jobs survive.

    def access_token(self) -> str:
        if self._token and self._expires > time.time() + 60:
            return self._token
        self.config.validate()
        with self.factory() as db:
            credential = db.get(CloudDriveCredential, 1)
            if not credential or credential.auth_required:
                raise AuthorizationRequired("Connect Google Drive to enable cloud backups.")
            refresh = self.config.decrypt(credential.encrypted_refresh_token)
        own_client = self.token_client is None
        client = self.token_client or httpx.Client(timeout=30, follow_redirects=False)
        try:
            data = _token_request(client, {"client_id": self.config.client_id, "client_secret": self.config.client_secret,
                "refresh_token": refresh, "grant_type": "refresh_token"})
            if data.get("refresh_token"):
                # Preserve a rotated refresh token before using the new access
                # token. No unencrypted token is written to a file or response.
                with self.factory() as db:
                    row = db.get(CloudDriveCredential, 1)
                    if row:
                        row.encrypted_refresh_token = self.config.encrypt(data["refresh_token"])
                        db.commit()
            self._token = data["access_token"]
            self._expires = time.time() + max(60, int(data.get("expires_in", 3600)))
            return self._token
        except AuthorizationRequired:
            with self.factory() as db:
                row = db.get(CloudDriveCredential, 1)
                if row:
                    row.auth_required, row.last_error = True, "Google Drive authorization must be renewed."
                    db.commit()
            raise
        finally:
            if own_client:
                client.close()

    def enqueue_ready(self) -> int:
        from .reporting import ExportJob
        with self.factory() as db:
            pending = db.scalar(select(func.count()).select_from(CloudBackupJob).where(CloudBackupJob.status != "verified")) or 0
            room = max(0, self.config.max_pending_jobs - pending)
            if not room:
                return 0
            rows = db.execute(select(ExportJob.id, ExportJob.trade_id, ExportJob.checksum).where(
                ExportJob.status.in_(["ready", "claimed", "saved"]), ExportJob.checksum.is_not(None),
                ~select(CloudBackupJob.id).where(CloudBackupJob.id == ExportJob.id).exists()
            ).order_by(ExportJob.created_at).limit(min(room, 25))).all()
            for job_id, trade_id, checksum in rows:
                db.add(CloudBackupJob(id=job_id, trade_id=trade_id, source_checksum=checksum,
                    status="pending", artifacts=[], next_attempt=0, lease_until=0))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # Another collector discovered the same immutable jobs.
                return 0
            return len(rows)

    def _folder(self, client, path: str, name: str, parent: str | None) -> str:
        key = hashlib.sha256(f"personal-options-trader:folder:{path}".encode()).hexdigest()
        with self.factory() as db:
            row = db.get(CloudBackupFolder, path)
            file_id = row.drive_id if row else None
        if not file_id:
            found = client.find(parent, key)
            file_id = found[0]["id"] if found else client.generate_id()
            with self.factory() as db:
                db.add(CloudBackupFolder(path=path, drive_id=file_id))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    file_id = db.get(CloudBackupFolder, path).drive_id
        remote = client.metadata(file_id)
        if remote is None:
            remote = client.create_folder(file_id, name, parent, key)
        if (not remote or remote.get("trashed") or remote.get("mimeType") != FOLDER_MIME or remote.get("name") != name
            or (parent is not None and remote.get("parents") != [parent])):
            raise BackupError("Google Drive backup folder changed; review its location.")
        return file_id

    def _broker_folder(self, client, trade: dict) -> str:
        closed = datetime.fromisoformat(str(trade.get("closed_at") or iso_now()).replace("Z", "+00:00"))
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=timezone.utc)
        closed = closed.astimezone(IST)
        names = ["Paper Trading Records", str(closed.year), closed.strftime("%B"), str(trade["broker"])]
        parent, path = None, ""
        for name in names:
            path += "/" + name
            parent = self._folder(client, path, name, parent)
        return parent

    def _save_artifacts(self, job_id, owner, records):
        with self.factory() as db:
            result = db.execute(update(CloudBackupJob).where(CloudBackupJob.id == job_id,
                CloudBackupJob.lease_owner == owner, CloudBackupJob.status == "uploading").values(
                artifacts=records, updated_at=iso_now(), lease_until=time.time() + 600))
            db.commit()
            if result.rowcount != 1:
                raise BackupError("Backup claim expired; archive remains queued.")

    def _process(self, job_id: str, owner: str, client):
        from .reporting import ExportJob
        from .models import Trade
        with self.factory() as db:
            job, source = db.get(CloudBackupJob, job_id), db.get(ExportJob, job_id)
            trade_row = db.get(Trade, job.trade_id)
            if not source or source.checksum != job.source_checksum or not trade_row or trade_row.status not in ("closed", "cancelled"):
                raise BackupError("Backup source is incomplete or changed; trading records retained.")
            trade, records = dict(trade_row.data), list(job.artifacts)
            artifacts = self.artifacts_provider(db, job_id)
        if not artifacts or not any(a.get("kind") == "xlsx" for a in artifacts):
            raise BackupError("Completed trade export is not ready; records retained.")
        parent = self._broker_folder(client, trade)
        for artifact in artifacts:
            content = artifact["file_bytes"]
            if (not content or len(content) != artifact["size"] or hashlib.sha256(content).hexdigest() != artifact["sha256"]
                or hashlib.md5(content).hexdigest() != artifact["md5"]):
                raise BackupError("Local archive checksum failed; records retained.")
            key = file_key(job_id, artifact)
            self.spool.write(key, content)
            existing = next((r for r in records if r.get("kind") == artifact["kind"] and r.get("sha256") == artifact["sha256"]), None)
            file_id = existing.get("drive_id") if existing else None
            if not file_id:
                # Search first, including recovery from uploads made before a
                # lost database acknowledgement; never overwrite remote files.
                found = client.find(parent, key)
                file_id = found[0]["id"] if found else client.generate_id()
                existing = {k: artifact[k] for k in ("kind", "filename", "mime_type", "size", "md5", "sha256")}
                existing.update(drive_id=file_id, parent_id=parent, verification="pending")
                records = [r for r in records if r.get("kind") != artifact["kind"]] + [existing]
                self._save_artifacts(job_id, owner, records)
            remote = client.metadata(file_id)
            if remote is None:
                client.upload(file_id, artifact["filename"], parent, artifact["mime_type"], key, content)
                remote = client.metadata(file_id)  # independent readback, never trust create alone
            verified = verify_remote(remote, artifact, parent, client)
            records = [r for r in records if r.get("kind") != artifact["kind"]] + [verified]
            self._save_artifacts(job_id, owner, records)
            self.spool.discard_verified_staging(key)
        with self.factory() as db:
            source = db.get(ExportJob, job_id)
            if not source or source.checksum != job.source_checksum:
                raise BackupError("Backup source changed before final verification; records retained.")
            changed = db.execute(update(CloudBackupJob).where(CloudBackupJob.id == job_id,
                CloudBackupJob.lease_owner == owner, CloudBackupJob.status == "uploading").values(
                status="verified", verified_at=iso_now(), updated_at=iso_now(), last_error=None,
                lease_until=0, lease_owner=None, artifacts=records))
            db.commit()
            if changed.rowcount != 1:
                raise BackupError("Backup claim expired; archive remains queued.")
        self.last_success, self.last_error = iso_now(), None

    def process_pending_once(self, limit: int = 1) -> int:
        if not self.config.enabled:
            return 0
        self.config.validate()
        self.enqueue_ready()
        with self.factory() as db:
            ids = db.scalars(select(CloudBackupJob.id).where(CloudBackupJob.status != "verified",
                CloudBackupJob.next_attempt <= time.time(), CloudBackupJob.lease_until <= time.time())
                .order_by(CloudBackupJob.created_at).limit(limit)).all()
        if not ids:
            return 0
        client = self.client_factory(self.access_token())
        done = 0
        try:
            for job_id in ids:
                owner, now = secrets.token_hex(24), time.time()
                with self.factory() as db:
                    claim = db.execute(update(CloudBackupJob).where(CloudBackupJob.id == job_id,
                        CloudBackupJob.status != "verified", CloudBackupJob.lease_until <= now,
                        CloudBackupJob.next_attempt <= now).values(status="uploading", lease_owner=owner,
                        lease_until=now + 600, attempts=CloudBackupJob.attempts + 1, updated_at=iso_now()))
                    db.commit()
                    if claim.rowcount != 1:
                        continue
                try:
                    self._process(job_id, owner, client)
                    done += 1
                except (BackupError, OSError, ValueError, KeyError, httpx.HTTPError) as error:
                    self.last_error = str(error) if isinstance(error, BackupError) else "Backup could not finish; source records retained."
                    if isinstance(error, AccessTokenExpired):
                        self._token, self._expires = None, 0
                    if isinstance(error, AuthorizationRequired):
                        self._token, self._expires = None, 0
                        with self.factory() as db:
                            row = db.get(CloudDriveCredential, 1)
                            if row:
                                row.auth_required, row.last_error = True, self.last_error
                                db.commit()
                    with self.factory() as db:
                        row = db.get(CloudBackupJob, job_id)
                        if row and row.lease_owner == owner:
                            row.status, row.last_error = "retry", self.last_error
                            row.next_attempt = time.time() + min(3600, 30 * 2 ** min(row.attempts, 7))
                            row.lease_until, row.lease_owner, row.updated_at = 0, None, iso_now()
                            db.commit()
                    log.warning("Cloud archive remains queued: %s", self.last_error)
        finally:
            client.close()
        return done

    def next_work_delay(self) -> float:
        """No active jobs: permit Neon autosuspend between infrequent scans."""
        with self.factory() as db:
            rows = db.execute(select(CloudBackupJob.next_attempt, CloudBackupJob.lease_until).where(
                CloudBackupJob.status != "verified").limit(self.config.max_pending_jobs)).all()
        if not rows:
            return max(600, self.config.interval_seconds)
        due = min(max(next_attempt, lease_until) for next_attempt, lease_until in rows)
        return min(3600, max(self.config.interval_seconds, due - time.time()))

    async def run(self, stop_event: asyncio.Event):
        """Independent of browsers. DB failures back off without tight polling."""
        self._wake_event = asyncio.Event()
        self._wake_loop = asyncio.get_running_loop()
        delay = self.config.interval_seconds
        while not stop_event.is_set():
            self._wake_event.clear()
            try:
                if self.config.enabled:
                    await asyncio.to_thread(self.process_pending_once)
                    delay = await asyncio.to_thread(self.next_work_delay)
                else:
                    delay = max(600, self.config.interval_seconds)
            except AuthorizationRequired:
                self.last_error = "Google Drive authorization is required; records retained."
                delay = max(600, self.config.interval_seconds)
            except Exception:
                self.last_error = "Cloud backup is temporarily unavailable; durable jobs retained."
                delay = min(3600, max(60, delay * 2))
                log.warning("Cloud backup unavailable; durable jobs retained")
            stop_task = asyncio.create_task(stop_event.wait())
            wake_task = asyncio.create_task(self._wake_event.wait())
            try:
                await asyncio.wait({stop_task, wake_task}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (stop_task, wake_task):
                    task.cancel()
                await asyncio.gather(stop_task, wake_task, return_exceptions=True)
        self._wake_loop, self._wake_event = None, None

    def status(self, db) -> dict:
        credential = db.get(CloudDriveCredential, 1)
        counts = dict(db.execute(select(CloudBackupJob.status, func.count()).group_by(CloudBackupJob.status)).all())
        jobs = db.scalars(select(CloudBackupJob).order_by(CloudBackupJob.updated_at.desc()).limit(30)).all()
        return {"enabled": self.config.enabled, "configured": all((self.config.client_id, self.config.client_secret,
                self.config.encryption_key, self.config.redirect_uri)),
            "connected": bool(credential and not credential.auth_required),
            "authorization_required": bool(credential and credential.auth_required), "counts": counts,
            "last_error": self.last_error or (credential.last_error if credential else None),
            "last_success": self.last_success, "retention": "All trading records retained; automatic deletion disabled.",
            "jobs": [{"id": j.id, "trade_id": j.trade_id, "status": j.status, "attempts": j.attempts,
                "last_error": j.last_error, "verified_at": j.verified_at, "next_attempt": j.next_attempt,
                "artifacts": j.artifacts} for j in jobs]}

    def retry(self, db, job_id: str):
        """Owner-authenticated manual retry; never interrupt a current upload."""
        changed = db.execute(update(CloudBackupJob).where(CloudBackupJob.id == job_id,
            CloudBackupJob.status != "verified", CloudBackupJob.lease_until <= time.time()).values(
            next_attempt=0, status="pending", last_error=None, updated_at=iso_now()))
        db.commit()
        return changed.rowcount == 1


def verified_archive_matches(db, export_job_id: str, artifacts: list[dict]) -> bool:
    """Evidence gate only. This does not authorize or perform data pruning."""
    from .reporting import ExportJob
    source, job = db.get(ExportJob, export_job_id), db.get(CloudBackupJob, export_job_id)
    if not source or not job or job.status != "verified" or not job.verified_at or source.checksum != job.source_checksum:
        return False
    for artifact in artifacts:
        matches = [r for r in job.artifacts if r.get("kind") == artifact["kind"] and r.get("verified_at")
                   and r.get("drive_id") and r.get("parent_id") and r.get("verification") in ("metadata_md5", "download_md5_sha256")]
        if len(matches) != 1 or any(str(matches[0].get(k)) != str(artifact[k]) for k in ("sha256", "md5", "size", "mime_type", "filename")):
            return False
    return bool(artifacts)
