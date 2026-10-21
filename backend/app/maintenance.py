"""Initial schema migration and portable, verified journal backups.

Run from backend: python -m app.maintenance migrate
                 python -m app.maintenance backup ../backups/trades.json.gz
                 python -m app.maintenance restore ../backups/trades.json.gz --server-stopped

Stop the backend and companion before restoration. Backups include private
account/session records and report bytes; keep them outside any public folder.
"""
from __future__ import annotations
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import uuid
from sqlalchemy import select,delete,text
from .database import Base,engine,iso_now
from . import models,reporting  # Register every table without starting feeds.

FORMAT='personal-options-trader-journal-v1'

def encode(value):
    if isinstance(value,(bytes,bytearray,memoryview)):
        return {'__pot_binary__':base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value,dict):return {key:encode(item) for key,item in value.items()}
    if isinstance(value,list):return [encode(item) for item in value]
    return value

def decode(value):
    if isinstance(value,dict):
        if set(value)=={'__pot_binary__'}:return base64.b64decode(value['__pot_binary__'],validate=True)
        return {key:decode(item) for key,item in value.items()}
    if isinstance(value,list):return [decode(item) for item in value]
    return value

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')

def migrate(target=engine):
    Base.metadata.create_all(target)
    with target.begin() as connection:
        versions=models.SchemaVersion.__table__
        if not connection.execute(select(versions.c.version).where(versions.c.version==1)).first():
            connection.execute(versions.insert(),{'version':1,'applied_at':iso_now()})

def read_backup(path):
    with gzip.open(path,'rb') as handle:envelope=json.loads(handle.read())
    checksum=envelope.pop('sha256',None)
    if envelope.get('format')!=FORMAT or not checksum or not __import__('hmac').compare_digest(checksum,hashlib.sha256(canonical(envelope)).hexdigest()):
        raise ValueError('This is not a valid, checksum-verified Personal Options Trader backup.')
    tables=envelope.get('tables')
    if not isinstance(tables,dict):raise ValueError('The backup table map is invalid.')
    allowed={t.name:t for t in Base.metadata.sorted_tables}
    if set(tables)-set(allowed):raise ValueError('The backup requires a different application schema. Use a matching application version.')
    for name,rows in tables.items():
        if not isinstance(rows,list) or any(not isinstance(row,dict) or set(row)-set(allowed[name].columns.keys()) for row in rows):
            raise ValueError(f'Invalid records for table {name}.')
    envelope['tables']={name:[decode(row) for row in rows] for name,rows in tables.items()}
    return envelope

def backup(path,target=engine):
    destination=Path(path).resolve()
    if destination.exists():raise FileExistsError('Choose a new backup filename; existing backups are never overwritten.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with target.connect() as connection:
        if target.dialect.name=='postgresql':connection=connection.execution_options(isolation_level='REPEATABLE READ')
        with connection.begin():
            tables={table.name:[encode(dict(row)) for row in connection.execute(select(table)).mappings()] for table in Base.metadata.sorted_tables}
    envelope={'format':FORMAT,'created_at':iso_now(),'tables':tables}
    envelope['sha256']=hashlib.sha256(canonical(envelope)).hexdigest()
    temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with gzip.open(temporary,'wb') as handle:handle.write(canonical(envelope))
        verified=read_backup(temporary)
        if not temporary.stat().st_size:raise ValueError('The backup is empty.')
        os.replace(temporary,destination)
        return {'path':str(destination),'size':destination.stat().st_size,'records':{name:len(rows) for name,rows in verified['tables'].items()}}
    finally:
        if temporary.exists():temporary.unlink()

def restore(path,target=engine,server_stopped=False):
    if not server_stopped:raise ValueError('Stop the backend and companion, then pass --server-stopped to restore.')
    source=Path(path).resolve();data=read_backup(source)
    safety=source.with_name('before_restore_'+iso_now().replace(':','').replace('.','_')+'_'+uuid.uuid4().hex[:6]+'.json.gz')
    backup(safety,target)
    with target.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):connection.execute(delete(table))
        for table in Base.metadata.sorted_tables:
            rows=data['tables'].get(table.name,[])
            if rows:connection.execute(table.insert(),rows)
        if target.dialect.name=='postgresql':
            for table in Base.metadata.sorted_tables:
                for column in table.primary_key:
                    if isinstance(column.type,__import__('sqlalchemy').Integer):
                        values=[row[column.name] for row in data['tables'].get(table.name,[]) if isinstance(row.get(column.name),int)]
                        connection.execute(text('SELECT setval(pg_get_serial_sequence(:table,:column), :value, false)'),{'table':table.name,'column':column.name,'value':max(values,default=0)+1})
    return {'restored':str(source),'pre_restore_backup':str(safety),'records':{name:len(rows) for name,rows in data['tables'].items()}}

def main():
    parser=argparse.ArgumentParser(description='Personal Options Trader database maintenance')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('migrate')
    for action in ('backup','restore'):
        command=sub.add_parser(action);command.add_argument('path')
        if action=='restore':command.add_argument('--server-stopped',action='store_true')
    args=parser.parse_args()
    if args.action=='migrate':migrate();result={'schema_version':1,'ok':True}
    elif args.action=='backup':result=backup(args.path)
    else:result=restore(args.path,server_stopped=args.server_stopped)
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
