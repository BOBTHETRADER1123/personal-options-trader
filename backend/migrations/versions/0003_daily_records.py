"""Add daily exports and recoverable Google Sheets cursors; preserve old data."""
from alembic import op
from app.database import Base
from app import daily_reports,sheets_sync
revision='0003_daily_records'
down_revision='0002_recording_and_cloud'
branch_labels=None
depends_on=None

def upgrade():
    for table in Base.metadata.sorted_tables:table.create(op.get_bind(),checkfirst=True)

def downgrade():
    raise RuntimeError('Daily reports, synchronization cursors and archives are never dropped automatically.')
