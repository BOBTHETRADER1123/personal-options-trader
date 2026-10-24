from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import cloud_backup as backup
from app import resource_health
from app.database import Base
from app.models import Trade
from app.reporting import ExportJob


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'cloud.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def config(tmp_path):
    return backup.DriveConfig(enabled=True, client_id="public-client", client_secret="private-client-secret",
        redirect_uri="https://example.test/api/cloud/drive/callback", encryption_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path), min_free_bytes=0)


def artifact(content=b"public mock trade report", kind="xlsx"):
    return {"kind": kind, "filename": f"report.{kind}", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if kind == "xlsx" else "application/gzip", "file_bytes": content,
        "sha256": hashlib.sha256(content).hexdigest(), "md5": hashlib.md5(content).hexdigest(), "size": len(content)}


def seed(factory, config, item=None):
    item = item or artifact()
    with factory() as db:
        db.add(Trade(id="trade-one", broker="5paisa", status="closed", data={"id": "trade-one", "broker": "5paisa",
            "closed_at": "2026-10-23T07:00:00+00:00", "status": "closed"}))
        db.add(ExportJob(id="export-one", trade_id="trade-one", broker="5paisa", filename="report.xlsx",
            relative_path="report.xlsx", status="ready", payload={}, file_bytes=item["file_bytes"], checksum=item["sha256"], size=item["size"]))
        db.add(backup.CloudDriveCredential(id=1, encrypted_refresh_token=config.encrypt("private-refresh-token"), auth_required=False))
        db.commit()


class MockDrive:
    def __init__(self):
        self.files, self.bodies, self.created, self.uploads = {}, {}, [], []
        self.fail_after_upload = False

    def close(self):
        pass

    def generate_id(self):
        return f"file-{len(self.created) + len(self.files) + 1}"

    def metadata(self, file_id):
        return dict(self.files[file_id]) if file_id in self.files else None

    def find(self, parent, key):
        return [dict(v) for v in self.files.values() if v.get("appProperties", {}).get("pot_backup_key") == key
                and (parent is None or v.get("parents") == [parent])]

    def create_folder(self, file_id, name, parent, key):
        self.created.append(file_id)
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": backup.FOLDER_MIME,
                              "parents": [parent] if parent else [], "appProperties": {"pot_backup_key": key}}
        return self.metadata(file_id)

    def upload(self, file_id, name, parent, mime, key, content):
        self.uploads.append(file_id)
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "parents": [parent],
            "appProperties": {"pot_backup_key": key}, "md5Checksum": hashlib.md5(content).hexdigest(), "size": str(len(content))}
        self.bodies[file_id] = content
        if self.fail_after_upload:
            self.fail_after_upload = False
            raise backup.BackupError("Interrupted after remote creation.")
        return self.metadata(file_id)

    def download_digest(self, file_id, max_bytes):
        value = self.bodies[file_id]
        assert len(value) <= max_bytes
        return {k: artifact(value)[k] for k in ("size", "md5", "sha256")}


def service(factory, config, drive, items=None):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "access_token": "private-access-token", "expires_in": 3600})))
    return backup.CloudBackupService(config, factory, lambda db, job_id: items or [artifact()], lambda token: drive, client)


def test_oauth_has_offline_scope_pkce_and_session_bound_encrypted_state(factory, config):
    with factory() as db:
        query = parse_qs(urlparse(backup.begin_authorization(db, "session-one", config)).query)
        assert query["scope"] == [backup.SCOPE]
        assert query["access_type"] == ["offline"]
        assert query["code_challenge_method"] == ["S256"]
        state = db.scalars(select(backup.CloudDriveOAuthState)).one()
        assert state.state_hash != query["state"][0]
        assert state.encrypted_verifier.startswith("gAAAA")
        with pytest.raises(backup.AuthorizationRequired):
            backup.complete_authorization(db, "different-session", query["state"][0], "unused", config)


def test_oauth_callback_encrypts_refresh_token_and_state_cannot_be_replayed(factory, config):
    requests = []
    def token(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "access-only", "refresh_token": "secret-refresh", "scope": backup.SCOPE})
    with httpx.Client(transport=httpx.MockTransport(token)) as client, factory() as db:
        state = parse_qs(urlparse(backup.begin_authorization(db, "session", config)).query)["state"][0]
        result = backup.complete_authorization(db, "session", state, "sensitive-code", config, client)
        assert result == {"connected": True, "offline_access": True}
        row = db.get(backup.CloudDriveCredential, 1)
        assert "secret-refresh" not in row.encrypted_refresh_token
        assert config.decrypt(row.encrypted_refresh_token) == "secret-refresh"
        assert b"code_verifier" in requests[0].content
        with pytest.raises(backup.AuthorizationRequired):
            backup.complete_authorization(db, "session", state, "sensitive-code", config, client)
        assert len(requests) == 1


def test_oauth_expired_state_and_missing_offline_access_are_rejected(factory, config):
    with factory() as db:
        state = parse_qs(urlparse(backup.begin_authorization(db, "session", config)).query)["state"][0]
        pending = db.get(backup.CloudDriveOAuthState, hashlib.sha256(state.encode()).hexdigest())
        pending.expires_at = 0
        db.commit()
        with pytest.raises(backup.AuthorizationRequired):
            backup.complete_authorization(db, "session", state, "unused", config)
        state = parse_qs(urlparse(backup.begin_authorization(db, "session", config)).query)["state"][0]
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"access_token": "access-only"}))) as client:
            with pytest.raises(backup.AuthorizationRequired, match="Offline access"):
                backup.complete_authorization(db, "session", state, "unused", config, client)
        assert db.get(backup.CloudDriveCredential, 1) is None


def test_disabled_worker_does_not_access_database_or_network(config):
    def forbidden():
        raise AssertionError("must remain idle")
    worker = backup.CloudBackupService(replace(config, enabled=False), forbidden)
    assert worker.process_pending_once() == 0


def test_archives_xlsx_and_csv_with_verified_durable_manifest(factory, config):
    seed(factory, config)
    drive, items = MockDrive(), [artifact(), artifact(b"gzip fixture", "csv_gz")]
    worker = service(factory, config, drive, items)
    assert worker.process_pending_once() == 1
    assert worker.process_pending_once() == 0
    with factory() as db:
        row = db.get(backup.CloudBackupJob, "export-one")
        assert row.status == "verified" and len(row.artifacts) == 2
        assert backup.verified_archive_matches(db, "export-one", items)
        assert db.get(Trade, "trade-one").status == "closed"
        assert db.get(ExportJob, "export-one").file_bytes == artifact()["file_bytes"]
        assert "private-" not in str(worker.status(db))
    assert len(drive.uploads) == 2
    assert [drive.files[i]["name"] for i in drive.created] == ["Paper Trading Records", "2026", "October", "5paisa"]
    assert not list((Path(config.data_dir) / "cloud-backup-spool").glob("*.staged"))


def test_laptop_claimed_report_is_still_eligible_for_independent_cloud_backup(factory, config):
    seed(factory, config)
    with factory() as db:
        db.get(ExportJob, "export-one").status = "claimed"
        db.commit()
    assert service(factory, config, MockDrive()).process_pending_once() == 1


def test_expired_access_token_refreshes_without_revoking_offline_consent(factory, config):
    seed(factory, config)
    drive = MockDrive()
    original = drive.metadata
    calls = []
    def expired_once(file_id):
        calls.append(file_id)
        if len(calls) == 1:
            raise backup.AccessTokenExpired("Refresh needed.")
        return original(file_id)
    drive.metadata = expired_once
    worker = service(factory, config, drive)
    assert worker.process_pending_once() == 0
    assert worker._token is None
    with factory() as db:
        assert not db.get(backup.CloudDriveCredential, 1).auth_required
        db.get(backup.CloudBackupJob, "export-one").next_attempt = 0
        db.commit()
    assert worker.process_pending_once() == 1


def test_interrupted_upload_reuses_preallocated_id_and_does_not_duplicate(factory, config):
    seed(factory, config)
    drive = MockDrive()
    drive.fail_after_upload = True
    worker = service(factory, config, drive)
    assert worker.process_pending_once() == 0
    with factory() as db:
        row = db.get(backup.CloudBackupJob, "export-one")
        assert row.status == "retry" and row.next_attempt > time.time()
        reserved = row.artifacts[0]["drive_id"]
        assert reserved == drive.uploads[0]
        row.next_attempt = 0
        db.commit()
    assert worker.process_pending_once() == 1
    assert drive.uploads == [reserved]


def test_checksum_mismatch_never_marks_verified_or_deletes_records(factory, config):
    seed(factory, config)
    drive = MockDrive()
    worker = service(factory, config, drive)
    original = drive.upload
    def corrupt(*args):
        result = original(*args)
        drive.files[result["id"]]["md5Checksum"] = "0" * 32
        return result
    drive.upload = corrupt
    assert worker.process_pending_once() == 0
    with factory() as db:
        row = db.get(backup.CloudBackupJob, "export-one")
        assert row.status == "retry" and row.verified_at is None
        assert not backup.verified_archive_matches(db, "export-one", [artifact()])
        assert db.get(Trade, "trade-one") and db.get(ExportJob, "export-one").file_bytes


def test_missing_remote_md5_downloads_once_and_checks_sha256(factory, config):
    item = artifact()
    drive = MockDrive()
    drive.upload("remote", item["filename"], "parent", item["mime_type"], "x", item["file_bytes"])
    remote = drive.metadata("remote")
    remote.pop("md5Checksum")
    calls = []
    original = drive.download_digest
    drive.download_digest = lambda *args: calls.append(args) or original(*args)
    result = backup.verify_remote(remote, item, "parent", drive)
    assert result["verification"] == "download_md5_sha256"
    assert calls == [("remote", item["size"])]


@pytest.mark.parametrize("field,value", [("parents", ["wrong"]), ("mimeType", "text/plain"), ("size", "1"), ("trashed", True)])
def test_wrong_remote_metadata_is_never_accepted(field, value):
    item, drive = artifact(), MockDrive()
    remote = {"id": "x", "name": item["filename"], "parents": ["parent"], "mimeType": item["mime_type"],
              "size": str(item["size"]), "md5Checksum": item["md5"]}
    remote[field] = value
    with pytest.raises(backup.BackupError):
        backup.verify_remote(remote, item, "parent", drive)


def test_active_lease_cannot_be_taken_and_expired_lease_recovers(factory, config):
    seed(factory, config)
    worker = service(factory, config, MockDrive())
    assert worker.enqueue_ready() == 1
    with factory() as db:
        row = db.get(backup.CloudBackupJob, "export-one")
        row.status, row.lease_owner, row.lease_until = "uploading", "another-worker", time.time() + 600
        db.commit()
    assert worker.process_pending_once() == 0
    with factory() as db:
        db.get(backup.CloudBackupJob, "export-one").lease_until = 0
        db.commit()
    assert worker.process_pending_once() == 1


def test_idle_worker_allows_database_autosuspend_and_active_jobs_use_due_time(factory, config):
    seed(factory, config)
    worker = service(factory, config, MockDrive())
    assert worker.next_work_delay() >= 600
    worker.enqueue_ready()
    assert worker.next_work_delay() == config.interval_seconds
    with factory() as db:
        db.get(backup.CloudBackupJob, "export-one").next_attempt = time.time() + 120
        db.commit()
    assert 119 <= worker.next_work_delay() <= 120


def test_refresh_token_rotation_remains_encrypted(factory, config):
    seed(factory, config)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 3600})))
    worker = backup.CloudBackupService(config, factory, token_client=client)
    assert worker.access_token() == "new-access"
    with factory() as db:
        row = db.get(backup.CloudDriveCredential, 1)
        assert "rotated-refresh" not in row.encrypted_refresh_token
        assert config.decrypt(row.encrypted_refresh_token) == "rotated-refresh"


def test_staging_path_cannot_escape_its_directory(config):
    spool = backup.BoundedSpool(config)
    for key in ("../report", "x" * 64, "/tmp/report"):
        with pytest.raises(backup.BackupError, match="Invalid backup staging"):
            spool.write(key, b"a")


def test_refresh_revocation_is_redacted_and_requires_authorization(factory, config):
    seed(factory, config)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, json={
        "error": "invalid_grant", "error_description": "private-token-should-never-be-displayed"})))
    worker = backup.CloudBackupService(config, factory, token_client=client)
    with pytest.raises(backup.AuthorizationRequired) as error:
        worker.access_token()
    assert "private" not in str(error.value)
    with factory() as db:
        row = db.get(backup.CloudDriveCredential, 1)
        assert row.auth_required and "private" not in row.last_error


def test_spool_bound_retains_database_and_never_overwrites_unrelated_files(config, tmp_path):
    limited = replace(config, max_spool_bytes=3, max_file_bytes=3)
    spool = backup.BoundedSpool(limited)
    path = spool.write("a" * 64, b"abc")
    assert path.read_bytes() == b"abc"
    with pytest.raises(backup.BackupError, match="storage is full"):
        spool.write("b" * 64, b"x")
    assert path.read_bytes() == b"abc"
    with pytest.raises(backup.BackupError, match="staging limit"):
        spool.write("c" * 64, b"abcd")


def test_upload_rejects_non_google_resumable_destination_without_leaking_token():
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, headers={"location": "https://untrusted.example/upload"})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        drive = backup.DriveClient("secret-token", client)
        with pytest.raises(backup.BackupError, match="invalid upload destination"):
            drive.upload("file", "a.xlsx", "parent", "application/octet-stream", "key", b"a")
    assert len(requests) == 1 and requests[0].url.host == "www.googleapis.com"


def test_resource_monitor_uses_container_limits_and_emits_transition_notifications(tmp_path, monkeypatch):
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.max").write_text("100000 100000")
    (cgroup / "cpu.stat").write_text("usage_usec 1000000")
    (cgroup / "memory.current").write_text("150")
    (cgroup / "memory.max").write_text("200")
    monkeypatch.setattr(resource_health.shutil, "disk_usage", lambda _: SimpleNamespace(used=10, total=100, free=90))
    clock = iter([0., 1., 2., 3.])
    monkeypatch.setattr(resource_health.time, "monotonic", lambda: next(clock))
    monitor = resource_health.ResourceMonitor(resource_health.ResourceConfig(data_dir=str(tmp_path)), str(cgroup), str(tmp_path))
    first = monitor.sample()
    assert first["sample"]["ram"]["percent"] == 75
    assert first["sample"]["cpu"]["percent"] is None
    assert first["active_alerts"][0]["severity"] == "warning"
    (cgroup / "cpu.stat").write_text("usage_usec 1900000")
    second = monitor.sample()
    assert second["sample"]["cpu"]["capacity_vcpu"] == 1
    assert second["sample"]["cpu"]["percent"] == 90
    assert len(second["notifications"]) == 2  # unchanged RAM is not repeated
    (cgroup / "memory.current").write_text("20")
    (cgroup / "cpu.stat").write_text("usage_usec 1910000")
    final = monitor.sample()
    assert not final["active_alerts"]
    assert final["sample"]["database"]["external_subscription_required"] is False
    assert len(final["notifications"]) == 4


def test_resource_monitor_marks_unknown_limit_unavailable(tmp_path):
    monitor = resource_health.ResourceMonitor(resource_health.ResourceConfig(data_dir=str(tmp_path)), str(tmp_path), str(tmp_path))
    value = monitor.sample()
    assert value["sample"]["ram"]["percent"] is None
    assert value["sample"]["ram"]["limit_bytes"] is None
