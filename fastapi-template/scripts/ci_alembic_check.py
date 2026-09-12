"""CI Alembic drift guard (P2-6).

Validates the Alembic migration chain against an ISOLATED temporary SQLite
database — safely, with no network and no production datastore involved:

  1. SINGLE HEAD   — ``alembic heads`` must yield exactly one head revision
                     (parallel/forks in the migration DAG are rejected).
  2. CLEAN APPLY   — ``alembic upgrade head`` must succeed on an empty DB.
  3. SCHEMA PARITY — the migrated schema must reproduce exactly what the
                     current ORM metadata produces via ``create_all``.

Exit code 0 = all invariants hold; 1 = drift or failure (CI blocks merge).

Run from fastapi-template/:
    python scripts/ci_alembic_check.py

NOTE: migration 0001_baseline delegates to ``Base.metadata.create_all`` (the
schema is *generated from the ORM*, not frozen).  Invariants 1 and 2 are the
hard guarantees; invariant 3 additionally catches divergence introduced by any
future hand-written migration (0002+).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every ORM model module must be imported so Base.metadata is complete —
# mirror the import list used by alembic/env.py.
ORM_MODULES = (
    "audit", "billing", "broker_account", "broker_state", "copy_trading",
    "marketplace", "notification", "trading", "user", "visual_strategy",
    "watchlist",
)

CREATE_ALL_SNIPPET = (
    "import os;"
    "from app.db.session import Base;"
    + ";".join(f"import app.models.{m}" for m in ORM_MODULES)
    + ";"
    + "from sqlalchemy import create_engine;"
    + "e=create_engine(os.environ['CI_ORM_DB_URL']);"
    + "Base.metadata.create_all(e)"
)


class GuardFailure(Exception):
    """Raised when an invariant does not hold."""


def _run_ok(args: list[str], env: dict[str, str]) -> str:
    """Run a subprocess; return stdout on success, else raise."""
    completed = subprocess.run(
        args, cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise GuardFailure(
            f"command {' '.join(args)} failed (rc={completed.returncode})\n"
            f"stdout: {completed.stdout[-2000:]}\nstderr: {completed.stderr[-2000:]}"
        )
    return completed.stdout


def _schema_snapshot(db_path: str) -> dict[str, dict[str, tuple]]:
    """Return {table: {column: (type, notnull, default, pk)}} for a SQLite file."""
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        snapshot: dict[str, dict[str, tuple]] = {}
        for table in sorted(tables):
            cols: dict[str, tuple] = {}
            for row in conn.execute(f"PRAGMA table_info('{table}')"):
                cid, name, ctype, notnull, default, pk = row
                cols[name] = (ctype or "", bool(notnull), default, bool(pk))
            snapshot[table] = cols
        return snapshot
    finally:
        conn.close()


def main() -> int:
    env = dict(os.environ)
    env.update(
        {
            "BROKER_MODE": "simulated",
            "ENVIRONMENT": "testing",
            "PYTHONUNBUFFERED": "1",
        }
    )

    with tempfile.TemporaryDirectory(prefix="ci_alembic_") as tmp:
        tmp_dir = Path(tmp)
        upgraded_db = tmp_dir / "upgraded.db"
        orm_db = tmp_dir / "orm.db"

        # 1. Single head
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{upgraded_db}"
        heads = _run_ok([sys.executable, "-m", "alembic", "heads"], env).strip()
        head_lines = [line for line in heads.splitlines() if line.strip() and "(head)" in line]
        if len(head_lines) != 1:
            raise GuardFailure(f"expected exactly 1 alembic head, found {len(head_lines)!r}\n{heads}")
        print(f"[pass] single alembic head: {head_lines[0].strip()}")

        # 2. Clean apply on an empty database
        _run_ok([sys.executable, "-m", "alembic", "upgrade", "head"], env)
        print("[pass] alembic upgrade head applied cleanly on an empty DB")

        # 3. Schema parity between the migrated DB and the ORM metadata.
        #    (The ORM DB is built in a subprocess with cwd=ROOT so `app` is
        #    importable there; we compare SQLite fingerprints here.)
        _run_ok(
            [sys.executable, "-c", CREATE_ALL_SNIPPET],
            {**env, "DATABASE_URL": f"sqlite+aiosqlite:///{orm_db}", "CI_ORM_DB_URL": f"sqlite:///{orm_db.as_posix()}"},
        )

        migrated = _schema_snapshot(str(upgraded_db))
        orm = _schema_snapshot(str(orm_db))

        migrated.pop("alembic_version", None)
        orm.pop("alembic_version", None)

        missing_tables = sorted(set(orm) - set(migrated))
        extra_tables = sorted(set(migrated) - set(orm))
        if missing_tables or extra_tables:
            raise GuardFailure(
                "table-set drift found:\n"
                f"  in ORM, not in migrations: {missing_tables}\n"
                f"  in migrations, not in ORM: {extra_tables}"
            )

        diffs: list[str] = []
        for table in sorted(orm):
            if orm[table] != migrated[table]:
                orm_cols = {k for k in orm[table]}
                mig_cols = {k for k in migrated[table]}
                diffs.append(
                    f"  {table}: ORM-only columns={sorted(orm_cols - mig_cols)} "
                    f"migration-only columns={sorted(mig_cols - orm_cols)}"
                )
        if diffs:
            raise GuardFailure("column-level drift found:\n" + "\n".join(diffs[:25]))

        print(f"[pass] schema parity: {len(orm)} tables match ORM metadata exactly")
        print("ALL ALEMBIC DRIFT GUARDS PASSED")
        return 0

    # (unreachable: TemporaryDirectory cleanup)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GuardFailure as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)