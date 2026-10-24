"""Private bootstrap importer tests use tiny synthetic databases, never user data."""
from contextlib import closing
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('workspace_import_under_test', ROOT / 'deploy/ramnaym/import_workspace.py')
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


def make_database(path, owners=1, trades=2, sessions=0, tickets=0):
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA journal_mode=DELETE')
        for name in workspace.COUNT_TABLES + workspace.AUTH_TABLES:
            db.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, value TEXT)')
        for name, count in {'owners': owners, 'trades': trades, 'quote_events': 3,
                            'timeline': 4, 'user_sessions': sessions, 'socket_tickets': tickets}.items():
            for n in range(count):
                db.execute(f'INSERT INTO "{name}" VALUES (?,?)', (n, 'SYNTHETIC TEST ONLY'))
        db.commit()
    return path.read_bytes()


@pytest.fixture
def prepared(tmp_path):
    bundle = tmp_path / 'private_workspace';bundle.mkdir()
    data = tmp_path / 'persistent';data.mkdir()
    source = tmp_path / 'synthetic.sqlite3'
    content = make_database(source)
    compressed = gzip.compress(content,mtime=0)
    (bundle / 'workspace.sqlite3.gz').write_bytes(compressed)
    manifest = {'sha256_gzip': hashlib.sha256(compressed).hexdigest(),
        'sha256_database': hashlib.sha256(content).hexdigest(), 'bytes_database': len(content),
        'expected_counts': {'owners': 1, 'trades': 2, 'quote_events': 3, 'timeline': 4}}
    (bundle / 'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    return bundle, data, manifest, content


def update_manifest(bundle,manifest):
    (bundle / 'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')


def test_absent_private_bundle_does_nothing(tmp_path):
    data=tmp_path / 'data';data.mkdir()
    assert workspace._import_bundle(tmp_path / 'missing',data)=={'status':'not_present'}
    assert list(data.iterdir())==[]


def test_verified_import_is_idempotent_after_database_changes(prepared):
    bundle,data,manifest,content=prepared
    result=workspace._import_bundle(bundle,data)
    assert result=={'status':'imported','counts':manifest['expected_counts']}
    assert (data / 'trading.db').read_bytes()==content
    assert json.loads((data / workspace.MARKER_NAME).read_text())=={'complete':True,**manifest}
    assert not (data / workspace.STAGE_NAME).exists()
    # Subsequent real usage must not be reset to the original import.
    with closing(sqlite3.connect(data / 'trading.db')) as db:
        db.execute('INSERT INTO trades VALUES (99,?)',('LATER SYNTHETIC TRADE',));db.commit()
    changed=(data / 'trading.db').read_bytes()
    assert workspace._import_bundle(bundle,data)=={'status':'already_imported'}
    assert (data / 'trading.db').read_bytes()==changed


def test_empty_existing_database_is_preserved_before_import(prepared):
    bundle,data,manifest,content=prepared
    target=data / 'trading.db'
    with closing(sqlite3.connect(target)) as db:
        db.execute('CREATE TABLE owners (id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE trades (id INTEGER PRIMARY KEY)')
    original=target.read_bytes()
    assert workspace._import_bundle(bundle,data)['status']=='imported'
    assert (data / workspace.BACKUP_NAME).read_bytes()==original
    assert target.read_bytes()==content


@pytest.mark.parametrize('table', ['owners','trades','quote_events','timeline','pnl_snapshots','user_sessions','future_unknown_history'])
def test_existing_records_are_never_overwritten(prepared,table):
    bundle,data,_,_=prepared
    target=data / 'trading.db'
    with closing(sqlite3.connect(target)) as db:
        db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
        db.execute(f'INSERT INTO "{table}" VALUES (1)');db.commit()
    original=target.read_bytes()
    with pytest.raises(workspace.WorkspaceImportError,match='contains records'):
        workspace._import_bundle(bundle,data)
    assert target.read_bytes()==original
    assert not (data / workspace.BACKUP_NAME).exists()
    assert not (data / workspace.MARKER_NAME).exists()


def test_marker_failure_after_install_is_recoverable(prepared,monkeypatch):
    bundle,data,_,content=prepared
    original=workspace._write_marker
    def interrupted(*args):raise OSError('Synthetic power interruption before completion marker')
    monkeypatch.setattr(workspace,'_write_marker',interrupted)
    with pytest.raises(OSError,match='Synthetic power'):
        workspace._import_bundle(bundle,data)
    assert (data / 'trading.db').read_bytes()==content
    assert not (data / workspace.MARKER_NAME).exists()
    monkeypatch.setattr(workspace,'_write_marker',original)
    assert workspace._import_bundle(bundle,data)=={'status':'recovered_completion'}


@pytest.mark.parametrize('field,value,error', [
 ('sha256_gzip','0'*64,'Compressed workspace checksum'),
 ('sha256_database','0'*64,'Expanded workspace checksum'),
 ('bytes_database',1,'exceeds the approved size'),
 ('bytes_database',workspace.MAX_DATABASE_BYTES+1,'bounded import allowance'),
 ('expected_counts',{'owners':1,'trades':99,'quote_events':3,'timeline':4},'row counts'),
])
def test_tampered_manifest_or_bundle_never_installs(prepared,field,value,error):
    bundle,data,manifest,_=prepared
    manifest[field]=value;update_manifest(bundle,manifest)
    with pytest.raises(workspace.WorkspaceImportError,match=error):
        workspace._import_bundle(bundle,data)
    assert not (data / 'trading.db').exists()
    assert not (data / workspace.MARKER_NAME).exists()


@pytest.mark.parametrize('sessions,tickets',[(1,0),(0,1)])
def test_active_browser_credentials_are_rejected(prepared,sessions,tickets):
    bundle,data,manifest,_=prepared
    source=bundle.parent / 'unsafe.sqlite3'
    content=make_database(source,sessions=sessions,tickets=tickets)
    compressed=gzip.compress(content,mtime=0)
    (bundle / 'workspace.sqlite3.gz').write_bytes(compressed)
    manifest.update(sha256_gzip=hashlib.sha256(compressed).hexdigest(),
        sha256_database=hashlib.sha256(content).hexdigest(),bytes_database=len(content))
    update_manifest(bundle,manifest)
    with pytest.raises(workspace.WorkspaceImportError,match='sessions and socket tickets'):
        workspace._import_bundle(bundle,data)
    assert not (data / 'trading.db').exists()


def test_corrupt_sqlite_with_matching_checksums_is_rejected(prepared):
    bundle,data,manifest,_=prepared
    content=b'not a SQLite database';compressed=gzip.compress(content,mtime=0)
    (bundle / 'workspace.sqlite3.gz').write_bytes(compressed)
    manifest.update(sha256_gzip=hashlib.sha256(compressed).hexdigest(),
        sha256_database=hashlib.sha256(content).hexdigest(),bytes_database=len(content))
    update_manifest(bundle,manifest)
    with pytest.raises(workspace.WorkspaceImportError,match='verified self-contained SQLite'):
        workspace._import_bundle(bundle,data)
    assert not (data / 'trading.db').exists()


def test_lock_rejects_concurrent_import_and_releases_after_failure(prepared):
    bundle,data,_,_=prepared
    with workspace._startup_lock(data):
        with pytest.raises(workspace.WorkspaceImportError,match='in progress'):
            workspace._import_bundle(bundle,data)
    assert workspace._import_bundle(bundle,data)['status']=='imported'


def test_changed_bundle_cannot_replace_already_imported_workspace(prepared):
    bundle,data,manifest,_=prepared
    workspace._import_bundle(bundle,data)
    original=(data / 'trading.db').read_bytes()
    manifest['sha256_gzip']='0'*64;update_manifest(bundle,manifest)
    with pytest.raises(workspace.WorkspaceImportError,match='different workspace import'):
        workspace._import_bundle(bundle,data)
    assert (data / 'trading.db').read_bytes()==original


def test_existing_journal_is_not_ignored(prepared):
    bundle,data,_,_=prepared
    with closing(sqlite3.connect(data / 'trading.db')) as db:db.execute('CREATE TABLE owners (id INTEGER)')
    (data / 'trading.db-journal').write_bytes(b'synthetic recovery marker')
    with pytest.raises(workspace.WorkspaceImportError,match='journal or active recovery'):
        workspace._import_bundle(bundle,data)
    assert not (data / workspace.MARKER_NAME).exists()


def test_incomplete_bundle_and_existing_backup_stop_safely(prepared):
    bundle,data,_,_=prepared
    compressed=(bundle / 'workspace.sqlite3.gz').read_bytes()
    (bundle / 'workspace.sqlite3.gz').unlink()
    with pytest.raises(workspace.WorkspaceImportError,match='regular files'):
        workspace._import_bundle(bundle,data)
    (bundle / 'workspace.sqlite3.gz').write_bytes(compressed)
    with closing(sqlite3.connect(data / 'trading.db')) as db:db.execute('CREATE TABLE owners (id INTEGER)')
    original=(data / 'trading.db').read_bytes()
    (data / workspace.BACKUP_NAME).write_bytes(b'PRESERVE SYNTHETIC OLD BACKUP')
    with pytest.raises(workspace.WorkspaceImportError,match='earlier empty workspace backup'):
        workspace._import_bundle(bundle,data)
    assert (data / 'trading.db').read_bytes()==original
    assert (data / workspace.BACKUP_NAME).read_bytes()==b'PRESERVE SYNTHETIC OLD BACKUP'


def test_no_clobber_install_preserves_unexpected_target(prepared,monkeypatch):
    bundle,data,_,_=prepared
    original_link=workspace.os.link
    def race(source,target):
        if Path(target).name=='trading.db':
            Path(target).write_bytes(b'UNEXPECTED DATABASE APPEARED')
        return original_link(source,target)
    monkeypatch.setattr(workspace.os,'link',race)
    with pytest.raises(FileExistsError):workspace._import_bundle(bundle,data)
    assert (data / 'trading.db').read_bytes()==b'UNEXPECTED DATABASE APPEARED'
    assert not (data / workspace.MARKER_NAME).exists()


def test_non_delete_journal_bundle_is_rejected(prepared):
    bundle,data,manifest,_=prepared
    source=bundle.parent / 'wal.sqlite3'
    make_database(source)
    with closing(sqlite3.connect(source)) as db:assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
    content=source.read_bytes();compressed=gzip.compress(content,mtime=0)
    (bundle / 'workspace.sqlite3.gz').write_bytes(compressed)
    manifest.update(sha256_gzip=hashlib.sha256(compressed).hexdigest(),
        sha256_database=hashlib.sha256(content).hexdigest(),bytes_database=len(content))
    update_manifest(bundle,manifest)
    with pytest.raises(workspace.WorkspaceImportError,match='DELETE journal'):
        workspace._import_bundle(bundle,data)
    assert not (data / 'trading.db').exists()


def initialized_target(path,trade_id='SETTINGS',kind='configuration_change'):
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE owners (id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE trades (id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO settings VALUES ('settings','SYNTHETIC INITIAL SETTINGS')")
        db.execute('CREATE TABLE schema_version (version INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO schema_version VALUES (?)',[(1,),(2,),(3,)])
        db.execute('CREATE TABLE trade_events (id INTEGER PRIMARY KEY,trade_id TEXT,kind TEXT,data TEXT)')
        db.execute('INSERT INTO trade_events VALUES (1,?,?,?)',(trade_id,kind,'SYNTHETIC INIT EVENT'))
        db.commit()


def test_known_initial_settings_event_is_preserved_in_empty_target_backup(prepared):
    bundle,data,_,content=prepared
    initialized_target(data / 'trading.db')
    original=(data / 'trading.db').read_bytes()
    assert workspace._import_bundle(bundle,data)['status']=='imported'
    assert (data / 'trading.db').read_bytes()==content
    assert (data / workspace.BACKUP_NAME).read_bytes()==original
    with closing(sqlite3.connect(data / workspace.BACKUP_NAME)) as db:
        assert db.execute('SELECT trade_id,kind FROM trade_events').fetchall()==[('SETTINGS','configuration_change')]


@pytest.mark.parametrize('trade_id,kind',[
    ('real-trade','configuration_change'),
    ('SETTINGS','trade_opened'),
    ('real-trade','filled'),
    (None,'configuration_change'),
    ('SETTINGS',None),
])
def test_initialization_exception_never_allows_actual_or_unknown_trade_events(prepared,trade_id,kind):
    bundle,data,_,_=prepared
    initialized_target(data / 'trading.db',trade_id,kind)
    original=(data / 'trading.db').read_bytes()
    with pytest.raises(workspace.WorkspaceImportError,match='contains records'):
        workspace._import_bundle(bundle,data)
    assert (data / 'trading.db').read_bytes()==original
    assert not (data / workspace.BACKUP_NAME).exists()
    assert not (data / workspace.MARKER_NAME).exists()
