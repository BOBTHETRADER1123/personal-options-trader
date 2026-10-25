"""Account-bound Google metadata. Switching never removes remote files or trades."""
import copy
import hashlib
import uuid
from sqlalchemy import JSON,String,delete,select
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base,iso_now

class GoogleAccountBinding(Base):
    __tablename__='google_account_binding'
    id:Mapped[int]=mapped_column(primary_key=True,default=1)
    account_key:Mapped[str]=mapped_column(String(64))
    email:Mapped[str]=mapped_column(String(240))
    connected_at:Mapped[str]=mapped_column(String(40),default=iso_now)

class PreviousGoogleAccount(Base):
    __tablename__='previous_google_accounts'
    id:Mapped[str]=mapped_column(String(40),primary_key=True)
    account_key:Mapped[str]=mapped_column(String(64),index=True)
    email:Mapped[str]=mapped_column(String(240))
    metadata_json:Mapped[dict]=mapped_column(JSON)
    switched_at:Mapped[str]=mapped_column(String(40),default=iso_now)

def current_account_key(db):
    row=db.get(GoogleAccountBinding,1)
    return row.account_key if row else None

def bind_account(db,profile):
    from .cloud_backup import BackupError,CloudBackupFolder,CloudBackupJob,CloudDriveCredential
    from .sheets_sync import SheetsDocument,SheetsTab,SheetsProgress,SheetsBatch,DailyCloudCopy,SheetsServiceState
    identity=str(profile.get('permissionId') or '');email=str(profile.get('emailAddress') or '').strip().lower()
    if not identity or '@' not in email:raise BackupError('Google account identity could not be verified; existing connection retained.')
    key=hashlib.sha256(identity.encode()).hexdigest();binding=db.get(GoogleAccountBinding,1)
    tables=(CloudBackupFolder,SheetsDocument,SheetsTab,SheetsProgress,SheetsBatch,DailyCloudCopy,SheetsServiceState)
    changed=bool(binding and binding.account_key!=key)
    if not binding and db.get(CloudDriveCredential,1) and any(db.scalar(select(model).limit(1)) for model in tables):
        raise BackupError('The previous Google account identity needs review before it can be changed; records retained.')
    if changed:
        archived={model.__tablename__:[{column.name:copy.deepcopy(getattr(row,column.name)) for column in model.__table__.columns}
            for row in db.scalars(select(model))] for model in (*tables,CloudBackupJob)}
        db.add(PreviousGoogleAccount(id=uuid.uuid4().hex,account_key=binding.account_key,email=binding.email,metadata_json=archived))
        for model in tables:db.execute(delete(model))
        for job in db.scalars(select(CloudBackupJob)):
            job.status,job.artifacts,job.attempts='pending',[],0
            job.next_attempt,job.lease_until,job.lease_owner=0,0,None
            job.verified_at,job.last_error=None,None
        binding.account_key,binding.email,binding.connected_at=key,email,iso_now()
    elif binding:binding.email,binding.connected_at=email,iso_now()
    else:db.add(GoogleAccountBinding(id=1,account_key=key,email=email))
    return {'account_email':email,'account_changed':changed}
