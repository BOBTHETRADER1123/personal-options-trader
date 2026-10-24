"""Optional, owner-approved private workspace bootstrap before app import.

Public releases contain this code only. A separate private deployment may include
private_workspace/workspace.sqlite3.gz and manifest.json. No network operations,
credential logging, schema changes or replacement of populated databases occur.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

MAX_DATABASE_BYTES = 1024 * 1024 * 1024
MAX_GZIP_BYTES = MAX_DATABASE_BYTES + 1024 * 1024
COUNT_TABLES = ('owners', 'trades', 'quote_events', 'timeline')
AUTH_TABLES = ('user_sessions', 'socket_tickets')
# Any historical records make an existing database ineligible for replacement.
PROTECTED_TABLES = COUNT_TABLES + AUTH_TABLES + ('pnl_snapshots', 'trade_events',
    'latest_quotes', 'pending_quote_processing', 'daily_reports', 'daily_report_parts')
MARKER_NAME = '.workspace-import.json'
STAGE_NAME = '.workspace-import.pending.sqlite3'
BACKUP_NAME = 'trading.db.before-workspace-import'


class WorkspaceImportError(RuntimeError):
    """Safe startup failure; original files are retained."""


def _regular(path: Path):
    if path.is_symlink() or not path.is_file():
        raise WorkspaceImportError('The private workspace bundle requires regular files.')


def _digest(path: Path, maximum: int):
    _regular(path)
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            size += len(block)
            if size > maximum:
                raise WorkspaceImportError('Workspace file exceeds the bounded import allowance.')
            digest.update(block)
    return digest.hexdigest(), size


def _manifest(path: Path):
    _regular(path)
    if path.stat().st_size > 16 * 1024:
        raise WorkspaceImportError('Private workspace manifest is too large.')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise WorkspaceImportError('Invalid private workspace manifest.') from None
    expected = {'sha256_gzip', 'sha256_database', 'bytes_database', 'expected_counts'}
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkspaceImportError('Unexpected private workspace manifest fields.')
    if any(not isinstance(value[key], str) or not re.fullmatch(r'[a-f0-9]{64}', value[key])
           for key in ('sha256_gzip', 'sha256_database')):
        raise WorkspaceImportError('Invalid private workspace checksum.')
    size = value['bytes_database']
    if type(size) is not int or not 0 < size <= MAX_DATABASE_BYTES:
        raise WorkspaceImportError('Private workspace database exceeds the bounded import allowance.')
    counts = value['expected_counts']
    if (not isinstance(counts, dict) or set(counts) != set(COUNT_TABLES)
            or any(type(n) is not int or n < 0 for n in counts.values())
            or counts['owners'] != 1):
        raise WorkspaceImportError('Expected workspace counts must identify exactly one owner.')
    return value


def _connect_readonly(path: Path):
    # immutable prevents recovery writes to a candidate copy. Sidecars are
    # rejected separately, so the database is always self-contained.
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def _no_sidecars(path: Path):
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise WorkspaceImportError('Workspace has a database journal or active recovery sidecar; source retained.')


def _inspect(path: Path, expected=None, empty=False):
    _regular(path)
    _no_sidecars(path)
    with path.open('rb') as source:
        header = source.read(20)
    # immutable read connections report DELETE even for a WAL-format header.
    # Validate both SQLite header format versions before trusting read results.
    if header.startswith(b'SQLite format 3\x00') and header[18:20] != b'\x01\x01':
        raise WorkspaceImportError('Workspace must use the verified DELETE journal mode.')
    try:
        with closing(_connect_readonly(path)) as db:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise WorkspaceImportError('Workspace database integrity verification failed.')
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                raise WorkspaceImportError('Workspace must use the verified DELETE journal mode.')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if expected is not None:
                if not set(COUNT_TABLES + AUTH_TABLES).issubset(tables):
                    raise WorkspaceImportError('Workspace is missing required identity or history tables.')
                counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                          for name in COUNT_TABLES}
                if counts != expected:
                    raise WorkspaceImportError('Workspace row counts differ from the approved manifest.')
                if any(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in AUTH_TABLES):
                    raise WorkspaceImportError('Browser sessions and socket tickets must be excluded from the import.')
            if empty:
                # Only initialization metadata may exist in an empty app DB.
                # Unknown populated tables also stop replacement, preserving
                # future schemas, recovery jobs and records we do not know yet.
                for name in tables - {'settings', 'schema_version', 'sqlite_sequence'}:
                    quoted = name.replace('"', '""')
                    if name == 'trade_events':
                        # Fresh installations may audit their initial settings.
                        # No trade-linked, null or other event is disposable.
                        columns = {row[1] for row in db.execute('PRAGMA table_info("trade_events")')}
                        if {'trade_id', 'kind'}.issubset(columns):
                            if not db.execute("SELECT 1 FROM trade_events WHERE trade_id IS NOT 'SETTINGS' OR kind IS NOT 'configuration_change' LIMIT 1").fetchone():
                                continue
                    if db.execute(f'SELECT 1 FROM "{quoted}" LIMIT 1').fetchone():
                        raise WorkspaceImportError('Existing workspace contains records; it will not be replaced.')
    except sqlite3.DatabaseError:
        raise WorkspaceImportError('Workspace is not a verified self-contained SQLite database.') from None


def _sync_directory(path: Path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def _startup_lock(data: Path):
    path = data / '.workspace-import.lock'
    if path.is_symlink():
        raise WorkspaceImportError('Unsafe workspace import lock path.')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    stream = os.fdopen(descriptor, 'r+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0');stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise WorkspaceImportError('Another workspace import is in progress.') from None
    try:
        yield
    finally:
        if os.name == 'nt':
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _write_marker(data: Path, manifest):
    # Write a complete marker and sync it before publishing its fixed name.
    fd, name = tempfile.mkstemp(prefix='.workspace-marker-', suffix='.tmp', dir=data)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump({'complete': True, **manifest}, target, sort_keys=True)
            target.flush();os.fsync(target.fileno())
        os.replace(temporary, data / MARKER_NAME)
        _sync_directory(data)
    finally:
        temporary.unlink(missing_ok=True)


def _expand(bundle: Path, stage: Path, manifest):
    if stage.is_symlink():
        raise WorkspaceImportError('Unsafe workspace import staging path.')
    # Only the fixed disposable derived copy is retried. The bundle and any
    # previous database remain untouched throughout verification.
    stage.unlink(missing_ok=True)
    descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, 'wb') as target, gzip.open(bundle, 'rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                size += len(block)
                if size > manifest['bytes_database'] or size > MAX_DATABASE_BYTES:
                    raise WorkspaceImportError('Expanded workspace exceeds the approved size.')
                digest.update(block);target.write(block)
            target.flush();os.fsync(target.fileno())
    except (OSError, EOFError):
        raise WorkspaceImportError('Compressed workspace could not be verified.') from None
    if size != manifest['bytes_database'] or digest.hexdigest() != manifest['sha256_database']:
        raise WorkspaceImportError('Expanded workspace checksum or byte count does not match.')
    _inspect(stage, expected=manifest['expected_counts'])


def _import_bundle(bundle_dir: Path, data: Path):
    """Internal path-injected entry for isolated fixture tests only."""
    bundle_dir, data = Path(bundle_dir), Path(data)
    if not bundle_dir.exists():
        return {'status': 'not_present'}
    if bundle_dir.is_symlink() or not bundle_dir.is_dir() or data.is_symlink() or not data.is_dir():
        raise WorkspaceImportError('Workspace import requires verified real directories.')
    bundle, manifest_path = bundle_dir / 'workspace.sqlite3.gz', bundle_dir / 'manifest.json'
    manifest = _manifest(manifest_path)
    target, stage, marker = data / 'trading.db', data / STAGE_NAME, data / MARKER_NAME
    with _startup_lock(data):
        if marker.exists() or marker.is_symlink():
            _regular(marker)
            if marker.stat().st_size > 16 * 1024:
                raise WorkspaceImportError('Invalid workspace completion marker.')
            try:
                completed = json.loads(marker.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError):
                raise WorkspaceImportError('Invalid workspace completion marker.') from None
            if completed != {'complete': True, **manifest}:
                raise WorkspaceImportError('A different workspace import is already recorded; no replacement was made.')
            _regular(target)
            return {'status': 'already_imported'}
        checksum, _ = _digest(bundle, MAX_GZIP_BYTES)
        if checksum != manifest['sha256_gzip']:
            raise WorkspaceImportError('Compressed workspace checksum does not match.')
        if target.exists() or target.is_symlink():
            actual, size = _digest(target, MAX_DATABASE_BYTES)
            if actual == manifest['sha256_database'] and size == manifest['bytes_database']:
                _inspect(target, expected=manifest['expected_counts'])
                _write_marker(data, manifest)
                return {'status': 'recovered_completion'}
            _inspect(target, empty=True)
        if shutil.disk_usage(data).free < manifest['bytes_database'] + 1024 * 1024:
            raise WorkspaceImportError('Insufficient persistent space for a verified workspace import.')
        _expand(bundle, stage, manifest)
        backup = data / BACKUP_NAME
        if target.exists():
            # Recheck immediately before preserving the empty database.
            _inspect(target, empty=True)
            if backup.exists() or backup.is_symlink():
                raise WorkspaceImportError('An earlier empty workspace backup exists; manual review required.')
            # A hard link is atomic and refuses to overwrite an existing file.
            os.link(target, backup);target.unlink();_sync_directory(data)
        # No-clobber atomic install: any unexpected target appearing here is
        # retained and causes startup to stop, never os.replace-overwritten.
        os.link(stage, target);stage.unlink();_sync_directory(data)
        _write_marker(data, manifest)
        return {'status': 'imported', 'counts': manifest['expected_counts']}


def import_approved_workspace(root: Path):
    """Use fixed production locations; optional private data never enters Git."""
    return _import_bundle(Path(root).resolve() / 'private_workspace', Path('/app/data'))
