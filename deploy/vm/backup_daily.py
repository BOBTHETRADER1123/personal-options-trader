"""One private, verified SQLite backup; limits stop creation, never delete archives."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

STATE = Path("/var/lib/personal-options-trader")
ROOT = Path(__file__).resolve().parents[2]


def limit(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive whole number.") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive whole number.")
    return value


def check_capacity(sizes: list[int], estimated_size: int, free: int,
                   max_files: int, max_bytes: int, min_free: int) -> None:
    if len(sizes) >= max_files:
        raise ValueError("Backup count limit reached. Existing archives were retained; arrange a reviewed off-site copy and capacity plan.")
    if sum(sizes) + estimated_size > max_bytes:
        raise ValueError("Backup byte limit reached. Existing archives were retained; review backup capacity.")
    if free - estimated_size < min_free:
        raise ValueError("Insufficient safe free space for a new backup. Existing records and archives were retained.")


def main() -> int:
    try:
        # EnvironmentFile is supplied by systemd. Do not load a Windows dotenv
        # file, connect to Neon, or silently back up a different database.
        required = f"sqlite:///{STATE}/trader.db"
        if os.getenv("DATABASE_URL") != required or os.getenv("DATA_DIR") != str(STATE):
            raise ValueError("The backup service requires the configured persistent VM SQLite database.")
        if STATE.resolve() != STATE:
            raise ValueError("The persistent state directory must not contain symlinks.")
        database = STATE / "trader.db"
        folder = STATE / "backups"
        if not database.is_file() or database.is_symlink() or not folder.is_dir() or folder.is_symlink():
            raise ValueError("The private database or backup directory is unavailable or unsafe.")
        entries = list(folder.iterdir())
        if any(not entry.is_file() or entry.is_symlink() for entry in entries):
            raise ValueError("Review unexpected entries in the private backup directory before creating another backup.")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as source:
            source.execute("PRAGMA query_only=ON")
            estimated = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
        check_capacity([entry.stat().st_size for entry in entries], estimated, shutil.disk_usage(STATE).free,
                       limit("BACKUP_MAX_FILES", 30), limit("BACKUP_MAX_BYTES", 1_073_741_824),
                       limit("BACKUP_MIN_FREE_BYTES", 2_147_483_648))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = folder / f"journal_{stamp}_{uuid.uuid4().hex[:12]}.sqlite3"
        partial = target.with_suffix(".sqlite3.partial")
        subprocess.run([sys.executable, str(ROOT / "scripts/backup.py"), "backup", "--file", str(partial)],
                       check=True, timeout=240, capture_output=True, text=True)
        if target.exists() or not partial.is_file() or partial.is_symlink():
            raise ValueError("The verified backup cannot be finalized safely.")
        partial.chmod(0o600)
        partial.rename(target)
        print(f"Verified private SQLite backup created: {target.name}")
        return 0
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        # SQL/connection strings and subprocess output are deliberately omitted.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"Private backup stopped: {message} No existing archive or source record was deleted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
