"""Add private broker credential storage and one-use daily sign-in state."""
from alembic import op
from app.broker_setup import PrivateBrokerCredential
from app.broker_auth import FivepaisaCredential, FivepaisaOAuthState, FivepaisaOAuthSlot
revision='0006_private_broker_auth'
down_revision='0005_google_accounts'
branch_labels=None
depends_on=None


def upgrade():
    for model in (PrivateBrokerCredential,FivepaisaCredential,FivepaisaOAuthState,FivepaisaOAuthSlot):
        model.__table__.create(op.get_bind(),checkfirst=True)


def downgrade():
    raise RuntimeError('Private broker state is preserved; use a verified recovery plan.')
