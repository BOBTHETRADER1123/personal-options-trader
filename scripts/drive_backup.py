"""Prepare verified Excel files for the connected Google Drive backup workflow.

This utility does not hold Google credentials, contact the cloud database, or
delete source records. Codex uploads pending files with the connected Drive
account, reads back their MD5 and size, then calls ``confirm``.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DRIVE_FOLDER_ID = '1CxpHsKBCJ8dxxNYbKAx6NMwEU9tojMn5'
TRADE_FIELDS = {
    'id', 'name', 'underlying', 'broker', 'status', 'created_at', 'closed_at',
    'gross_pnl', 'charges', 'net_pnl', 'mfe', 'mae', 'mfe_at', 'mae_at', 'demo',
}
LEG_FIELDS = {
    'symbol', 'expiry', 'strike', 'option_type', 'side', 'lots', 'lot_size',
    'price_multiplier', 'entry_price', 'exit_price', 'entry_at', 'exit_at',
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_config(path: Path) -> dict:
    # Never inject private configuration into this process's environment.
    return dict(dotenv_values(path, interpolate=False)) if path.is_file() else {}


def readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError('Local data file is unavailable')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.execute('PRAGMA query_only=ON')
    return connection


def local_database(root: Path) -> Path:
    config = read_config(root / '.env')
    # Use only the laptop data directory. A configured PostgreSQL URL is never
    # connected to, parsed into output, or used as an archival data source.
    folder = Path(config.get('DATA_DIR') or root / 'OptionsTraderData').expanduser()
    if not folder.is_absolute():
        folder = root / folder
    return folder.resolve() / 'trader.db'


def companion_folders(root: Path) -> list[Path]:
    folders = []
    for filename in ('.env', '.env.online'):
        config = read_config(root / 'companion' / filename)
        value = config.get('TRADES_FOLDER')
        if not value:
            continue
        folder = Path(os.path.expandvars(value)).expanduser()
        if not folder.is_absolute():
            folder = root / folder
        folder = folder.resolve()
        if folder not in folders:
            folders.append(folder)
    return folders


def reporting_functions(state_dir: Path):
    """Import the existing report formatter without configuring a cloud DB."""
    backend = str(ROOT / 'backend')
    if backend not in sys.path:
        sys.path.insert(0, backend)
    previous = {name: os.environ.get(name) for name in ('DATABASE_URL', 'DATA_DIR')}
    try:
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        os.environ['DATA_DIR'] = str(state_dir)
        reporting = importlib.import_module('app.reporting')
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return reporting.build_workbook, reporting.verify_workbook


def summary_payload(trade: dict) -> dict:
    # Report data is an explicit allowlist, never a raw database or auth dump.
    value = {name: trade.get(name) for name in TRADE_FIELDS}
    value['legs'] = [{name: leg.get(name) for name in LEG_FIELDS}
                     for leg in trade.get('legs', []) if isinstance(leg, dict)]
    return {'format': 'summary', 'trade': value}


def hashes(content: bytes) -> dict:
    return {'size': len(content), 'md5': hashlib.md5(content).hexdigest(),
            'sha256': hashlib.sha256(content).hexdigest()}


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.partial', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


class BackupQueue:
    def __init__(self, folder: Path):
        self.folder = folder.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.files = self.folder / 'outbox'
        self.files.mkdir(exist_ok=True)
        self.connection = sqlite3.connect(self.folder / 'manifest.sqlite3', timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS backups (
            key TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
            md5 TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
            drive_id TEXT, created_at TEXT NOT NULL, uploaded_at TEXT)''')
        self.connection.commit()

    def close(self):
        self.connection.close()

    def add(self, source: str, source_id: str, fingerprint: str,
            producer: Callable[[], bytes], verifier: Callable[[bytes], None]) -> str:
        key = uuid.uuid5(uuid.NAMESPACE_URL, f'paper-trader-drive:{source_id}:{fingerprint}').hex
        filename = f'{"TradeSummary" if source == "local_summary" else "TradeArchive"}_{key}.xlsx'
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            existing = self.connection.execute('SELECT * FROM backups WHERE key=?', (key,)).fetchone()
            if existing and existing['status'] == 'uploaded':
                self.connection.commit()
                return key
            target = self.files / filename
            if existing and target.is_file():
                check = hashes(target.read_bytes())
                if all(check[name] == existing[name] for name in check):
                    self.connection.commit()
                    return key
            content = producer()
            verifier(content)
            check = hashes(content)
            atomic_write(target, content)
            self.connection.execute('''INSERT INTO backups
                (key,source,source_id,fingerprint,filename,size,md5,sha256,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                size=excluded.size, md5=excluded.md5, sha256=excluded.sha256, status='pending' ''',
                (key, source, source_id, fingerprint, filename, check['size'], check['md5'],
                 check['sha256'], 'pending', now()))
            self.connection.commit()
            return key
        except Exception:
            self.connection.rollback()
            raise

    def pending(self) -> list[dict]:
        values = []
        for row in self.connection.execute("SELECT * FROM backups WHERE status='pending' ORDER BY created_at,key"):
            path = self.files / row['filename']
            # Recheck the actual file immediately before it is offered for upload.
            check = hashes(path.read_bytes()) if path.is_file() else {}
            if not check or any(check[key] != row[key] for key in check):
                raise ValueError('A queued Excel file changed; prepare it again before uploading')
            values.append({name: row[name] for name in ('key', 'filename', 'source', 'size', 'md5', 'sha256')}
                          | {'path': str(path), 'folder_id': DRIVE_FOLDER_ID})
        return values

    def uploaded_count(self) -> int:
        return self.connection.execute("SELECT count(*) FROM backups WHERE status='uploaded'").fetchone()[0]

    def confirm(self, key: str, drive_id: str, md5: str, size: int) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', drive_id):
            raise ValueError('A valid verified Drive file ID is required')
        if not re.fullmatch(r'[a-fA-F0-9]{32}', md5) or size <= 0:
            raise ValueError('Drive MD5 and positive file size are required')
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            row = self.connection.execute('SELECT * FROM backups WHERE key=?', (key,)).fetchone()
            if not row:
                raise ValueError('Unknown backup key')
            if row['status'] == 'uploaded' and row['drive_id'] != drive_id:
                raise ValueError('This backup is already confirmed with another Drive file')
            path = self.files / row['filename']
            if not path.is_file():
                raise ValueError('The prepared Excel file is unavailable')
            check = hashes(path.read_bytes())
            if any(check[name] != row[name] for name in check):
                raise ValueError('The prepared Excel file has changed')
            if not secrets.compare_digest(md5.lower(), row['md5']) or size != row['size']:
                raise ValueError('Drive verification does not match the prepared Excel file')
            self.connection.execute("UPDATE backups SET status='uploaded',drive_id=?,uploaded_at=? WHERE key=?",
                                    (drive_id, now(), key))
            self.connection.commit()
            return {'key': key, 'status': 'uploaded', 'drive_id': drive_id, 'size': size}
        except Exception:
            self.connection.rollback()
            raise


def prepare(root: Path = ROOT, state_dir: Path | None = None,
            report_builder=None, verifier=None) -> dict:
    root = root.resolve()
    state_dir = (state_dir or root / '.desktop' / 'drive-backup').resolve()
    if report_builder is None or verifier is None:
        default_builder, default_verifier = reporting_functions(state_dir)
        report_builder = report_builder or default_builder
        verifier = verifier or default_verifier
    queue = BackupQueue(state_dir)
    issues = []
    try:
        database = local_database(root)
        if database.is_file():
            try:
                with closing(readonly(database)) as connection:
                    for identifier, broker, raw in connection.execute(
                            "SELECT id,broker,data FROM trades WHERE lower(status)='closed' ORDER BY id"):
                        try:
                            trade = json.loads(raw)
                            if not isinstance(trade, dict) or str(trade.get('id')) != str(identifier):
                                raise ValueError('Trade identity mismatch')
                            if str(trade.get('status', '')).lower() != 'closed':
                                raise ValueError('Trade state mismatch')
                            payload = summary_payload(trade)
                            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                                                     ensure_ascii=False).encode()).hexdigest()
                            queue.add('local_summary', f'local:{database}:{identifier}', fingerprint,
                                      lambda value=payload: report_builder(value), verifier)
                        except Exception:
                            issues.append({'source': 'local_summary', 'code': 'trade_could_not_be_prepared'})
            except sqlite3.Error:
                issues.append({'source': 'local_summary', 'code': 'local_database_unavailable'})

        for folder in companion_folders(root):
            manifest = folder / '.companion' / 'manifest.sqlite3'
            if not manifest.is_file():
                continue
            try:
                with closing(readonly(manifest)) as connection:
                    for identifier, raw in connection.execute(
                            "SELECT id,data FROM jobs WHERE status IN ('acknowledged','verified_pending_ack') ORDER BY id"):
                        try:
                            job = json.loads(raw)
                            path = Path(job['local_path']).resolve()
                            if folder not in path.parents or path.suffix.lower() != '.xlsx':
                                raise ValueError('Report must remain in the configured archive folder')
                            content = path.read_bytes()
                            check = hashes(content)
                            if check['sha256'] != job.get('checksum') or check['size'] != job.get('size'):
                                raise ValueError('Saved workbook does not match its companion receipt')
                            queue.add('verified_report', f'companion:{folder}:{identifier}', check['sha256'],
                                      lambda value=content: value, verifier)
                        except Exception:
                            issues.append({'source': 'verified_report', 'code': 'saved_report_could_not_be_verified'})
            except sqlite3.Error:
                issues.append({'source': 'verified_report', 'code': 'companion_manifest_unavailable'})
        pending = queue.pending()
        return {'folder_id': DRIVE_FOLDER_ID, 'pending_count': len(pending), 'pending': pending,
                'uploaded_count': queue.uploaded_count(), 'issues': issues,
                'source_records_deleted': False}
    finally:
        queue.close()


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == '--confirm':
        values[0] = 'confirm'
    parser = argparse.ArgumentParser(description='Prepare and verify Excel backups for connected Google Drive.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('prepare')
    confirm = subparsers.add_parser('confirm')
    confirm.add_argument('key')
    confirm.add_argument('--drive-id', required=True)
    confirm.add_argument('--md5', required=True)
    confirm.add_argument('--size', type=int, required=True)
    args = parser.parse_args(values or ['prepare'])
    try:
        if args.command == 'prepare':
            result = prepare()
        else:
            queue = BackupQueue(ROOT / '.desktop' / 'drive-backup')
            try:
                result = queue.confirm(args.key, args.drive_id, args.md5, args.size)
            finally:
                queue.close()
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('issues') else 0
    except Exception:
        # Raw exception text can contain configuration, SQL or private paths.
        print(json.dumps({'error': 'Backup preparation or verification failed. Source records were retained.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
