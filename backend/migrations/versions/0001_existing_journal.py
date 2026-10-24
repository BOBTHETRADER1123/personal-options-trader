"""Baseline the installed journal without rewriting or dropping existing rows."""
from alembic import op
from app.database import Base
from app import models, reporting

revision = '0001_existing_journal'
down_revision = None
branch_labels = None
depends_on = None

TABLES = ('settings','owners','user_sessions','socket_tickets','trades','trade_events',
          'timeline','quote_events','pending_quote_processing','latest_quotes',
          'quote_receipt_counter','compact_quote_receipts','actions','schema_version',
          'export_jobs','export_daily_reconciliation')

def upgrade():
    connection = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(connection, checkfirst=True)

def downgrade():
    raise RuntimeError('Existing trading records are never dropped by a downgrade.')
