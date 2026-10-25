"""Preserve Google account-specific backup/sync metadata during reconnection."""
from alembic import op
from app.google_accounts import GoogleAccountBinding,PreviousGoogleAccount
revision='0005_google_accounts'
down_revision='0004_verified_archival'
branch_labels=None
depends_on=None

def upgrade():
    for table in (GoogleAccountBinding.__table__,PreviousGoogleAccount.__table__):table.create(op.get_bind(),checkfirst=True)

def downgrade():
    raise RuntimeError('Previous Google account evidence is retained; use a verified recovery plan.')
