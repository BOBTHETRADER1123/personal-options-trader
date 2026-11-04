"""Removed broker services cannot load; legacy records remain migration-safe."""
import importlib.util
from pathlib import Path
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from app import maintenance,main
from app.database import Base,configure_sqlite
from app.models import FivepaisaCredential,FivepaisaOAuthState,FivepaisaOAuthSlot,Trade


def test_retired_implementation_modules_and_routes_are_absent():
    assert importlib.util.find_spec('app.broker_auth') is None
    assert importlib.util.find_spec('app.brokers.fivepaisa') is None
    assert not any(getattr(route,'path','').startswith('/api/brokers/fivepaisa') for route in main.app.routes)


def test_schema_migration_preserves_retired_encrypted_records_and_trade_history(tmp_path):
    sql=create_engine(f'sqlite:///{(tmp_path/"preserved.db").as_posix()}')
    configure_sqlite(sql,'DELETE');Base.metadata.create_all(sql)
    factory=sessionmaker(sql,expire_on_commit=False)
    with factory() as db:
        db.add(FivepaisaCredential(id=1,encrypted_credentials='synthetic-opaque-ciphertext',config_hash='test-hash',expires_at=1,connected_at=1))
        db.add(FivepaisaOAuthState(state_hash='retired-state',session_hash='retired-session',config_hash='test-hash',expires_at=1,used=True))
        db.add(FivepaisaOAuthSlot(id=1,current_state_hash='retired-state',last_error=None))
        db.add(Trade(id='historical-only',broker='fivepaisa',status='closed',data={'id':'historical-only','net_pnl':123,'legs':[]}));db.commit()
    maintenance.migrate(sql)
    with factory() as db:
        assert db.get(FivepaisaCredential,1).encrypted_credentials=='synthetic-opaque-ciphertext'
        assert db.get(FivepaisaOAuthState,'retired-state').used
        assert db.get(FivepaisaOAuthSlot,1).current_state_hash=='retired-state'
        assert db.get(Trade,'historical-only').data['net_pnl']==123
        assert db.connection().exec_driver_sql('PRAGMA journal_mode').scalar()=='delete'
    sql.dispose()
