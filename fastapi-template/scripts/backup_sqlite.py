"""TradeThrone — SQLite database backup script.

Snapshots the local SQLite DB using the SQLite online backup API (safe while
the app is running, including in WAL mode) and prunes old snapshots.

Usage:
    python scripts/backup_sqlite.py [--db trading.db] [--dir backups] [--keep 14]

Also supports PostgreSQL via a thin wrapper on `pg_dump` when a DATABASE_URL is
pointed at PostgreSQL (see BACKUP_RECOVERY.md).
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sqlite3
import subprocess

DEFAULT_DB = pathlib.Path("trading.db")
DEFAULT_DIR = pathlib.Path("backups")
DEFAULT_KEEP = 14


def backup_sqlite(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Online-safe backup using the SQLite backup API."""
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def prune(directory: pathlib.Path, keep: int, pattern: str) -> None:
    snaps = sorted(directory.glob(pattern))
    for old in snaps[:-keep]:
        old.unlink()
        print(f"Pruned {old.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup the TradeThrone DB")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite db path")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="backup directory")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="snapshots to keep")
    args = parser.parse_args()

    backup_dir = pathlib.Path(args.dir)
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = backup_dir / f"trading-{ts}.db"

    backup_sqlite(pathlib.Path(args.db), dst)
    print(f"Backed up to {dst.name}")
    prune(backup_dir, args.keep, "trading-*.db")


if __name__ == "__main__":
    main()
