"""Refuse unverified network SQLite volumes; diagnostics contain no secrets."""
import json
import os
from pathlib import Path,PurePosixPath
import sqlite3
import subprocess
import sys

LOCAL_FILESYSTEMS={'ext4','ext3','ext2','xfs','btrfs','zfs'}

def mount_details(path, mountinfo='/proc/self/mountinfo'):
    path=PurePosixPath(str(path));matches=[]
    for line in Path(mountinfo).read_text().splitlines():
        before,after=line.split(' - ',1);parts=before.split();mount=PurePosixPath(parts[4].replace('\\040',' '))
        if path==mount or path.is_relative_to(mount):matches.append((len(mount.parts),str(mount),after.split()[0]))
    if not matches:raise RuntimeError('Persistent filesystem type could not be verified.')
    _,mount,fstype=max(matches)
    return {'mount':mount,'filesystem':fstype,'local':fstype in LOCAL_FILESYSTEMS}

def verify_storage(data_dir='/app/data', mountinfo='/proc/self/mountinfo'):
    data=Path(data_dir).resolve();details=mount_details(data,mountinfo)
    if not details['local'] or details['mount']!='/app/data':
        raise RuntimeError('SQLite requires the verified local /app/data volume; unknown/network storage is refused.')
    if not data.is_dir() or not os.access(data,os.W_OK):raise RuntimeError('Persistent data volume is not writable.')
    # This separate diagnostic database never reads or modifies trading records.
    path=data/'sqlite-storage-check.db'
    with sqlite3.connect(path,timeout=1) as first:
        first.execute('PRAGMA journal_mode=DELETE');first.execute('PRAGMA synchronous=FULL')
        first.execute('CREATE TABLE IF NOT EXISTS probe(id INTEGER PRIMARY KEY,value INTEGER)')
        first.execute('INSERT OR REPLACE INTO probe VALUES(1,1)');first.commit()
        first.execute('BEGIN IMMEDIATE')
        child=subprocess.run([sys.executable,'-c',"import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=.1); c.execute('BEGIN IMMEDIATE')",str(path)],capture_output=True,timeout=4)
        first.rollback()
        if child.returncode==0 or b'database is locked' not in child.stderr:raise RuntimeError('SQLite cross-process write locking failed.')
        # A killed writer must not commit an unfinished transaction.
        child=subprocess.run([sys.executable,'-c',"import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE probe SET value=2'); os._exit(0)",str(path)],capture_output=True,timeout=4)
        if child.returncode or first.execute('SELECT value FROM probe WHERE id=1').fetchone()[0]!=1 or first.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('SQLite rollback recovery failed.')
    return {**details,'write_lock_verified':True,'rollback_recovery_verified':True,'journal_mode':'DELETE'}
