"""Independent verified NSE cash catalog. This service only reads broker data."""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from . import kotak_master
from .instruments import contract_master_status
from .master_diagnostics import safe_master_traceback
from .models import Trade

NSE_CASH = {'exchange': 'NSE', 'segment': 'nse_cm'}
BACKGROUND_RETRY_SECONDS = 300
MANUAL_RETRY_SECONDS = 15
log = logging.getLogger('personal_options_trader.stocks')


class StockMaster:
    """Publish current listings without replacing the derivatives catalog."""

    def __init__(self, catalog, write_lock):
        self.catalog = catalog
        self.write_lock = write_lock
        self.refresh_lock = asyncio.Lock()
        self.last_failure = float("-inf")
        self.error = None

    def status(self):
        rows = self.catalog.stock_rows
        ready = bool(rows and contract_master_status(rows[0])['ready'])
        return {
            'status': 'ready' if ready else 'stale' if rows else 'not_loaded',
            'last_successful_refresh_at': self.catalog.stock_metadata.get('last_successful_refresh_at'),
            'message': self.error,
        }

    def search(self, query, limit, *, demo, session):
        needle = query.strip().upper()
        matches = [] if demo else [
            row for row in self.catalog.stock_rows
            if not needle or needle in row['underlying'] or needle in (row.get('name') or '').upper()
        ]
        if needle:
            matches.sort(key=lambda row: (
                row['underlying'] != needle, not row['underlying'].startswith(needle), row['underlying']
            ))
        return {
            'stocks': matches[:limit], 'total': len(matches), 'demo': demo,
            'session': session, 'master': self.status(),
        }

    def due(self):
        return (not self.refresh_lock.locked() and time.monotonic() - self.last_failure >= BACKGROUND_RETRY_SECONDS
                and self.status()['status'] != 'ready')

    async def refresh(self, db, *, background=False):
        current = self.status()
        if current['status'] == 'ready':
            return {'ok': True, 'master': current}
        if self.refresh_lock.locked():
            return {'ok': False, 'master': {**current, 'message': 'The stock list is already refreshing.'}}
        cooldown = BACKGROUND_RETRY_SECONDS if background else MANUAL_RETRY_SECONDS
        if time.monotonic() - self.last_failure < cooldown:
            return {'ok': False, 'master': {**current, 'message': 'Please wait a few seconds and try again.'}}

        async with self.refresh_lock:
            candidate = None
            try:
                batch = await asyncio.to_thread(kotak_master.download_stock_master)
                candidate = await asyncio.to_thread(self.catalog.prepare_stock_master, batch)
                async with self.write_lock:
                    active_legs = await asyncio.to_thread(self._active_legs, db)
                    self.catalog.publish_stock_master(candidate, active_legs)
                self.error = None
            except Exception as exc:
                details = exc.details if isinstance(exc, kotak_master.MasterRefreshError) else {
                    'code': 'stock_refresh_failed', 'exception_type': type(exc).__name__,
                }
                log.error('NSE stock list refresh failed. diagnostic=%s\n%s', details, safe_master_traceback(exc))
                self.error = 'Kotak could not refresh the NSE stock list. Saved positions are retained.'
                self.last_failure = time.monotonic()
            finally:
                if candidate is not None:
                    candidate.path.unlink(missing_ok=True)
            updated = self.status()
            return {'ok': updated['status'] == 'ready', 'master': updated}

    @staticmethod
    def _active_legs(db):
        try:
            active = db.scalars(select(Trade.data).where(Trade.broker == 'kotak', Trade.status == 'open'))
            return [
                leg for trade in active for leg in trade.get('legs', [])
                if leg.get('option_type') == 'EQ' and leg.get('open_lots', 0) > 0
            ]
        finally:
            db.rollback()
