"""PostgreSQL drift guard — the strongest available production-DB parity check.

The SQLite CI guard (``ci_alembic_check.py``) validates the migration DAG, a
clean apply, and ORM/migration table+column parity, but it CANNOT see
constraint-level drift that only manifests on PostgreSQL (FK constraints, unique
constraints, partial indexes, CHECK constraints).  This guard closes that gap:

  1. SINGLE HEAD  — exactly one alembic head revision.
  2. CLEAN APPLY  — ``alembic upgrade head`` against a pristine PostgreSQL
                    database (both ``parity_migrate`` and ``parity_orm`` schemas
                    are reset first so every run is reproducible).
  3. FULL ORM PARITY — a second pristine database is created with
                    ``Base.metadata.create_all`` and BOTH databases are
                    introspected through the same SQLAlchemy dialect; the guard
                    compares tables, columns (name/type/nullability/default),
                    primary keys, unique constraints, foreign keys and indexes.
                    Any difference between what migrations produce and what the
                    ORM declares fails the run.

Usage (PostgreSQL reachable — e.g. ``docker compose up postgres`` or a local
server; defaults match the repo compose service on host port 5434):

    python scripts/ci_postgres_check.py

Tunable via environment:
    PARITY_PG_HOST      (default localhost)
    PARITY_PG_PORT      (default 5434)
    PARITY_PG_USER      (default tradetron)
    PARITY_PG_PASSWORD  (default tradetron_secure_password)

Exit 0 = all invariants hold; 1 = drift or failure (CI blocks merge).

This guard is DEPENDENCY-INJECTED on a live PostgreSQL server.  It never touches
application data: the two databases it creates are disposable scratch databases
inside the supplied server instance.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Same ORM import list as alembic/env.py and ci_alembic_check.py — every model
# module must be imported so Base.metadata is complete.
ORM_MODULES = (
    "audit", "billing", "broker_account", "broker_state", "copy_trading",
    "marketplace", "notification", "protective_order", "trading", "user",
    "visual_strategy", "watchlist",
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
    """Raised when a PostgreSQL parity invariant does not hold."""


def _pg_host() -> str:
    return os.environ.get("PARITY_PG_HOST", "localhost")


def _pg_port() -> str:
    return os.environ.get("PARITY_PG_PORT", "5434")


def _pg_user() -> str:
    return os.environ.get("PARITY_PG_USER", "tradetron")


def _pg_password() -> str:
    return os.environ.get("PARITY_PG_PASSWORD", "tradetron_secure_password")


def base_admin_url() -> str:
    """Control-plane URL used to reset the two scratch database schemas."""
    return (
        f"postgresql+psycopg://{_pg_user()}:{_pg_password()}"
        f"@{_pg_host()}:{_pg_port()}/postgres"
    )


def scratch_db_url(dbname: str, sync: bool) -> str:
    driver = "psycopg" if sync else "asyncpg"
    return (
        f"postgresql+{driver}://{_pg_user()}:{_pg_password()}"
        f"@{_pg_host()}:{_pg_port()}/{dbname}"
    )


def _reset_db(dbname: str) -> None:
    """Drop and recreate the scratch database so the run is reproducible."""
    from sqlalchemy import create_engine, text

    admin = create_engine(base_admin_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        # Terminate lingering connections (nothing should be connected to the
        # scratch DBs, but a previous failed run could have left one).
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": dbname},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin.dispose()


def _run_ok(args: list[str], env: dict[str, str]) -> str:
    """Run a subprocess; return stdout on success, else raise GuardFailure."""
    completed = subprocess.run(
        args, cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise GuardFailure(
            f"command {' '.join(args)} failed (rc={completed.returncode})\n"
            f"stdout: {completed.stdout[-2000:]}\nstderr: {completed.stderr[-2000:]}"
        )
    return completed.stdout


def _schema_snapshot(db_url: str) -> dict[str, dict]:
    """Return a comparable fingerprint of the schema at ``db_url``.

    Uses SQLAlchemy Inspector over a synchronous psycopg engine so both sides
    (migration-produced and ORM-produced) are read through the SAME dialect and
    comparison is apples-to-apples.
    """
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect

    engine = create_engine(db_url)
    try:
        inspector = sa_inspect(engine)
        snapshot: dict[str, dict] = {}
        for table in sorted(inspector.get_table_names()):
            cols = []
            for c in inspector.get_columns(table):
                default = c.get("default")
                cols.append(
                    (
                        c["name"],
                        str(c["type"]),
                        bool(c.get("nullable", True)),
                        "" if default is None else str(default),
                    )
                )
            fks = []
            for fk in inspector.get_foreign_keys(table):
                fks.append(
                    repr(
                        (
                            fk.get("name") or "",
                            tuple(fk.get("constrained_columns") or []),
                            fk.get("referred_table") or "",
                            tuple(fk.get("referred_columns") or []),
                            tuple(sorted((fk.get("options") or {}).items())),
                        )
                    )
                )
            uniques = []
            for uq in inspector.get_unique_constraints(table):
                uniques.append(
                    repr((uq.get("name") or "", tuple(sorted(uq.get("column_names") or []))))
                )
            indexes = []
            for ix in inspector.get_indexes(table):
                indexes.append(
                    repr(
                        (
                            ix.get("name") or "",
                            tuple(sorted(ix.get("column_names") or [])),
                            bool(ix.get("unique", False)),
                        )
                    )
                )
            pk = []
            try:
                pk_constraint = inspector.get_pk_constraint(table)
                pk = sorted(pk_constraint.get("constrained_columns", []))
            except Exception:
                pk = []
            snapshot[table] = {
                "columns": sorted(cols),
                "fks": sorted(fks),
                "uniques": sorted(uniques),
                "indexes": sorted(indexes),
                "pk": pk,
            }
        return snapshot
    finally:
        engine.dispose()


def main() -> int:
    migrate_dbname = "parity_migrate"
    orm_dbname = "parity_orm"

    print(f"[info] postgres host={_pg_host()} port={_pg_port()} "
          f"dbs={migrate_dbname},{orm_dbname}")

    for dbname in (migrate_dbname, orm_dbname):
        _reset_db(dbname)
        print(f"[pass] reset scratch database {dbname}")

    base_env = dict(os.environ)
    base_env.update(
        {
            "BROKER_MODE": "simulated",
            "ENVIRONMENT": "testing",
            "PYTHONUNBUFFERED": "1",
            "JWT_SECRET": "p" * 40,
        }
    )

    # 1. Single head
    env = {**base_env, "DATABASE_URL": scratch_db_url(migrate_dbname, sync=False)}
    heads = _run_ok([sys.executable, "-m", "alembic", "heads"], env).strip()
    head_lines = [line for line in heads.splitlines() if line.strip() and "(head)" in line]
    if len(head_lines) != 1:
        raise GuardFailure(f"expected exactly 1 alembic head, found {len(head_lines)!r}\n{heads}")
    print(f"[pass] single alembic head: {head_lines[0].strip()}")

    # 2. Clean apply on pristine PostgreSQL
    _run_ok([sys.executable, "-m", "alembic", "upgrade", "head"], env)
    print("[pass] alembic upgrade head applied cleanly on PostgreSQL")

    # 3. ORM create_all into the second schema
    _run_ok(
        [sys.executable, "-c", CREATE_ALL_SNIPPET],
        {**base_env, "CI_ORM_DB_URL": scratch_db_url(orm_dbname, sync=True)},
    )
    print("[pass] ORM create_all produced the reference schema")

    migrated = _schema_snapshot(scratch_db_url(migrate_dbname, sync=True))
    orm = _schema_snapshot(scratch_db_url(orm_dbname, sync=True))

    migrated.pop("alembic_version", None)
    orm.pop("alembic_version", None)

    problems: list[str] = []

    missing_tables = sorted(set(orm) - set(migrated))
    extra_tables = sorted(set(migrated) - set(orm))
    if missing_tables or extra_tables:
        problems.append(
            f"table-set drift: ORM-only={missing_tables} migration-only={extra_tables}"
        )

    for table in sorted(set(orm) & set(migrated)):
        o, m = orm[table], migrated[table]
        for key in ("columns", "fks", "uniques", "indexes", "pk"):
            if o[key] != m[key]:
                orm_only = sorted(set(map(str, o[key])) - set(map(str, m[key])))
                mig_only = sorted(set(map(str, m[key])) - set(map(str, o[key])))
                problems.append(
                    f"{table}.{key}: ORM-only={orm_only[:8]} migration-only={mig_only[:8]}"
                )

    if problems:
        raise GuardFailure("PostgreSQL schema drift found:\n  " + "\n  ".join(problems[:40]))

    print(f"[pass] full ORM parity on PostgreSQL: {len(orm)} tables, "
          f"constraints + indexes match exactly")
    print("ALL POSTGRES DRIFT GUARDS PASSED")
    return 0


if __name__ == "__main__":
    try:
        # Import guard: fail early with an actionable message if psycopg is
        # unavailable (it is declared in requirements.txt).
        import psycopg  # noqa: F401
    except ImportError:
        print("[FAIL] psycopg is not installed (required for the PostgreSQL guard).", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(main())
    except GuardFailure as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)