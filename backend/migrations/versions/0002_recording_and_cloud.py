"""Add second-level observations, fencing, gaps, artifacts and cloud job state."""
from alembic import op
from app.database import Base
from app import models, reporting, cloud_backup

revision = '0002_recording_and_cloud'
down_revision = '0001_existing_journal'
branch_labels = None
depends_on = None

TABLES = ('pnl_snapshots','recording_cursors','recording_gaps','scheduler_leases',
          'export_artifacts','cloud_drive_credentials','cloud_drive_oauth_states',
          'cloud_backup_folders','cloud_backup_jobs')

def upgrade():
    connection = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(connection, checkfirst=True)

def downgrade():
    raise RuntimeError('Captured snapshots, archive evidence and recovery records are never dropped by a downgrade.')
