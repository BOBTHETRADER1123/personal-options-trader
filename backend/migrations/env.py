"""Additive migrations share the existing app configuration and connection pool."""
from alembic import context
from app.database import Base, engine
from app import models, reporting, cloud_backup, daily_reports, sheets_sync, archival, google_accounts


def online():
    provided=context.config.attributes.get('connection')
    if provided is not None:
        context.configure(connection=provided,target_metadata=Base.metadata,
                          compare_type=True,transaction_per_migration=True)
        with context.begin_transaction():context.run_migrations()
        return
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata,
                          compare_type=True, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError('Run additive migrations online against the selected app database; offline SQL is disabled to avoid exposing connection credentials.')
else:
    online()
