"""Switching Google accounts preserves trading data and old backup evidence."""
from types import SimpleNamespace
import copy
import pytest
from sqlalchemy import create_engine,func,select
from sqlalchemy.orm import sessionmaker
from app.database import Base,configure_sqlite
from app.google_accounts import bind_account,GoogleAccountBinding,PreviousGoogleAccount,current_account_key
from app.cloud_backup import CloudBackupFolder,CloudBackupJob,CloudDriveCredential,BackupError
from app.sheets_sync import SheetsDocument,SheetsProgress
from app.models import Trade

@pytest.fixture
def factory(tmp_path):
    sql=create_engine(f'sqlite:///{tmp_path / "accounts.db"}');configure_sqlite(sql,journal_mode='DELETE');Base.metadata.create_all(sql)
    yield sessionmaker(sql,expire_on_commit=False)
    sql.dispose()

A={'permissionId':'account-A','emailAddress':'first@example.test'}
B={'permissionId':'account-B','emailAddress':'second@example.test'}

def seed(db):
    bind_account(db,A)
    db.add(Trade(id='t1',broker='kotak',status='open',data={'id':'t1','legs':[],'status':'open'}))
    db.add(CloudBackupFolder(path='/Paper Trading Records',drive_id='old-folder'))
    db.add(SheetsDocument(trading_date='2026-10-26',part=1,drive_id='old-sheet',lookup_key='old-key',status='ready',duplicate_file_ids=[]))
    db.add(SheetsProgress(trade_id='t1',last_second=100,acknowledged_rows=5))
    db.add(CloudBackupJob(id='j1',trade_id='t1',source_checksum='abc',status='verified',artifacts=[{'drive_id':'old-backup'}],attempts=1))
    db.commit()

def test_switch_archives_metadata_and_preserves_open_position(factory):
    with factory() as db:
        seed(db);original=copy.deepcopy(db.get(Trade,'t1').data);old=current_account_key(db)
        assert bind_account(db,B)=={'account_email':'second@example.test','account_changed':True}
        db.commit()
        assert current_account_key(db)!=old and db.get(Trade,'t1').data==original
        assert db.scalar(select(func.count()).select_from(SheetsDocument))==0
        assert db.scalar(select(func.count()).select_from(CloudBackupFolder))==0
        assert db.scalar(select(func.count()).select_from(SheetsProgress))==0
        history=db.scalars(select(PreviousGoogleAccount)).one()
        assert history.email==A['emailAddress']
        assert history.metadata_json['sheets_documents'][0]['drive_id']=='old-sheet'
        job=db.get(CloudBackupJob,'j1');assert job.status=='pending' and job.artifacts==[]

def test_reauthorize_same_account_keeps_cursors_and_ids(factory):
    with factory() as db:
        seed(db);key=current_account_key(db)
        assert not bind_account(db,A)['account_changed'];db.commit()
        assert current_account_key(db)==key and db.get(SheetsProgress,'t1').last_second==100
        assert db.get(CloudBackupFolder,'/Paper Trading Records').drive_id=='old-folder'
        assert db.scalar(select(func.count()).select_from(PreviousGoogleAccount))==0

@pytest.mark.parametrize('profile',[{}, {'emailAddress':'valid@example.test'}, {'permissionId':'a','emailAddress':'bad'}])
def test_unverified_identity_never_replaces_existing_binding(factory,profile):
    with factory() as db:
        seed(db)
        with pytest.raises(BackupError,match='identity'):bind_account(db,profile)
        db.rollback()
        assert db.get(GoogleAccountBinding,1).email==A['emailAddress']
        assert db.get(SheetsProgress,'t1').last_second==100
