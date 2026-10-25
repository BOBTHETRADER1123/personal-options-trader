"""Verified snapshot archival receipts and durable retry state."""
from alembic import op
from app.archival import ArchiveReceipt, ArchiveState
revision='0004_verified_archival'
down_revision='0003_daily_records'
branch_labels=None
depends_on=None

def upgrade():
    for table in (ArchiveReceipt.__table__,ArchiveState.__table__):table.create(op.get_bind(),checkfirst=True)

def downgrade():
    raise RuntimeError('Archival evidence is retained; downgrade requires an explicit recovery plan.')
