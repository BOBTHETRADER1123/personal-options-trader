"""Consistent local SQLite backups and guarded restores. Cloud: use Neon tools."""
from pathlib import Path
import argparse
from datetime import datetime
import sqlite3
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

def database_path():
    from app.database import DATABASE_URL
    if not DATABASE_URL.startswith('sqlite:///'):
        raise RuntimeError('This helper is for local SQLite only. Use the Neon backup/restore controls for PostgreSQL.')
    return Path(DATABASE_URL.removeprefix('sqlite:///')).resolve()

def validate(path):
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError('The selected backup does not exist or is empty.')
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity verification failed.')
        names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'trades','trade_events','owners'}.issubset(names):
            raise RuntimeError('The selected file is not a Personal Options Trader journal.')

def copy_database(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(f'{source.as_uri()}?mode=ro',uri=True) as original, sqlite3.connect(target) as out:
        original.backup(out)
    validate(target)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup','restore'])
    parser.add_argument('--file', help='Explicit backup filename, optional in interactive use')
    args=parser.parse_args()
    database=database_path()
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if args.action=='backup':
        validate(database)
        target=Path(args.file).expanduser().resolve() if args.file else ROOT/'backups'/f'journal_{stamp}.sqlite3'
        if target.exists():raise RuntimeError('That destination already exists. Choose a unique backup filename.')
        copy_database(database,target)
        print(f'Backup saved and verified: {target}')
        print('This file contains private account and trade records. Store it securely, outside GitHub.')
        return
    try:
        with urlopen('http://127.0.0.1:8000/api/health',timeout=2):
            raise RuntimeError('Stop Start-Windows.cmd before restoring. The backend is still running on port 8000.')
    except OSError:
        pass
    value=args.file or input('Full path of the SQLite backup to restore: ').strip().strip('"')
    source=Path(value).expanduser().resolve()
    validate(source)
    if source==database:raise RuntimeError('Choose a separate backup file, not the current live database.')
    print(f'This restores the local app from: {source}')
    print('It replaces the current account and journal contents. A verified before-restore backup will be kept.')
    if input('Type RESTORE to continue: ').strip()!='RESTORE':
        print('Restore cancelled. Current records were kept.')
        return
    if database.exists():
        retained=ROOT/'backups'/f'before_restore_{stamp}.sqlite3'
        copy_database(database,retained)
        print(f'Previous records retained at: {retained}')
    copy_database(source,database)
    print('Restore verified. Start the app and sign in with the password from the selected backup.')

if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,sqlite3.Error) as error:
        print(f'Operation stopped: {error}',file=sys.stderr)
        raise SystemExit(1)
