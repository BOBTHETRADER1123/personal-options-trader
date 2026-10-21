"""Check that local backups are consistent and restores retain prior records."""
import importlib.util
import sqlite3
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('backup_helper',Path(__file__).resolve().parents[2]/'scripts'/'backup.py')
backup=importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)

def journal(path,value):
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE trades(id TEXT); CREATE TABLE trade_events(id INTEGER); CREATE TABLE owners(id INTEGER);')
        db.execute('INSERT INTO trades VALUES (?)',(value,))

def test_backup_verifies_and_preserves_source(tmp_path):
    source=tmp_path/'original.sqlite3';target=tmp_path/'copy.sqlite3'
    journal(source,'original-trade')
    backup.copy_database(source,target)
    backup.validate(target)
    with sqlite3.connect(source) as db:assert db.execute('SELECT id FROM trades').fetchone()[0]=='original-trade'
    with sqlite3.connect(target) as db:assert db.execute('SELECT id FROM trades').fetchone()[0]=='original-trade'

def test_restore_keeps_before_snapshot(tmp_path,monkeypatch):
    selected=tmp_path/'selected.sqlite3';current=tmp_path/'current.sqlite3'
    journal(selected,'older-trade');journal(current,'newer-trade')
    monkeypatch.setattr(backup,'ROOT',tmp_path)
    monkeypatch.setattr(backup,'database_path',lambda:current)
    monkeypatch.setattr(backup,'urlopen',lambda *args,**kwargs:(_ for _ in ()).throw(OSError('offline')))
    monkeypatch.setattr('builtins.input',lambda _: 'RESTORE')
    monkeypatch.setattr('sys.argv',['backup.py','restore','--file',str(selected)])
    backup.main()
    safety=list((tmp_path/'backups').glob('before_restore_*.sqlite3'))
    assert len(safety)==1
    with sqlite3.connect(current) as db:assert db.execute('SELECT id FROM trades').fetchone()[0]=='older-trade'
    with sqlite3.connect(safety[0]) as db:assert db.execute('SELECT id FROM trades').fetchone()[0]=='newer-trade'

def test_invalid_backup_does_not_replace_target(tmp_path):
    bad=tmp_path/'bad.sqlite3';bad.write_bytes(b'not a database')
    target=tmp_path/'current.sqlite3';journal(target,'keep-trade')
    with pytest.raises((RuntimeError,sqlite3.Error)):backup.validate(bad)
    with sqlite3.connect(target) as db:assert db.execute('SELECT id FROM trades').fetchone()[0]=='keep-trade'
