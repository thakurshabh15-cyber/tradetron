# TradeThrone — Database Backup & Recovery Procedure (Staging / Production)

**Scope:** TradeThrone FastAPI backend. Applies to both SQLite (local/dev) and
PostgreSQL (staging/production) databases.

**Goal:** Guarantee a point-in-time restorable copy of all trading data (orders,
positions, trades, broker accounts, users, documents, audit logs) so a failure
never results in silent data loss.

---

## 1. What must be backed up

| Data | Backend location | Criticality |
| :--- | :--- | :--- |
| Relational data (users, orders, positions, trades, broker_accounts, billing, audit_logs, copy_trading, marketplace) | PostgreSQL (staging/prod) or `trading.db` (SQLite dev) | 🔴 Critical |
| Broker credentials (encrypted at rest via Fernet) | In the DB (`*_encrypted` columns) | 🔴 Critical — need `JWT_SECRET` to decrypt |
| Secret material (JWT_SECRET, webhook secrets, payment keys) | `.env` / deployment secrets | 🔴 Never backed up as plaintext in backups |

> **IMPORTANT:** DB backups alone are **not** enough for broker credentials.
> `app/core/crypto.py` derives the Fernet key from `JWT_SECRET`. Restoring a DB
> without the matching `JWT_SECRET` yields undecryptable broker secrets. Store
> `JWT_SECRET` in a secret manager (vault/KMS), never in the backup.

---

## 2. SQLite (local / dev)

### Backup (online-safe, uses SQLite backup API)
```bash
# From the project root (fastapi-template)
./.venv/Scripts/python.exe scripts/backup_sqlite.py    # see below
# or one-liner via sqlite3 CLI:
sqlite3 trading.db ".backup backup-$(date +%Y%m%d-%H%M).db"
```
WAL-mode: a running app is safe to back up with `.backup` (it snapshots cleanly).

### Restore
```bash
# Stop the app, replace the db file, restart
Copy-Item backup-20260904-1200.db trading.db
```

### Automated rotation (recommended)
```python
# scripts/backup_sqlite.py — keeps last N daily snapshots
import shutil, sqlite3, datetime, pathlib, os

SRC = pathlib.Path("trading.db")
BACKUP_DIR = pathlib.Path("backups")
KEEP = 14

def backup():
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"trading-{ts}.db"
    target = sqlite3.connect(dst)
    src = sqlite3.connect(SRC)
    src.backup(target)
    target.close(); src.close()
    # prune old backups
    snaps = sorted(BACKUP_DIR.glob("trading-*.db"))
    for old in snaps[:-KEEP]:
        old.unlink()
    print(f"Backed up to {dst.name}; kept {KEEP} snapshots")

if __name__ == "__main__":
    backup()
```

---

## 3. PostgreSQL (staging / production)

### Backup with `pg_dump` (logical, schema + data, portable)
```bash
pg_dump "postgresql://user:pass@host:5432/tradetron" \
  -F c -f backups/tradetron-$(date +%Y%m%d-%H%M).dump
```
`-F c` custom format supports selective restore and is compressed.

### Restore with `pg_restore` (custom format)
```bash
# Create target DB first
createdb "postgresql://user:pass@host:5432/tradetron_restored"
pg_restore -d "postgresql://user:pass@host:5432/tradetron_restored" \
  backups/tradetron-20260904-1200.dump
```

### Managed providers (content/behaviour)
| Provider | Tool | Notes |
| :--- | :--- | :--- |
| Supabase | Dashboard → Database → Backups | Point-in-time recovery built in |
| Neon | `neon` branch / PITR | Branch for instant restore |
| Railway/Render | `pg_dump` / platform snapshots | Use `pg_dump` for portability |

---

## 4. Verify a backup is restorable (do this, don't skip)

A backup is worthless if you can't restore it. Script the check:

```bash
# Restore to a throwaway DB and run a smoke query
pg_restore -d "postgresql://.../restore_test" backups/...dump
psql "postgresql://.../restore_test" -c "SELECT count(*) FROM orders;"
```

Add `scripts/verify_backup.sh` (S3: fetch latest, restore to temp, assert
`users`/`orders` counts > 0, drop temp).

---

## 5. RPO / RTO targets (staging)

| Metric | Target | Achieved via |
| :--- | :--- | :--- |
| RPO (max data loss) | ≤ 15 min | cron `pg_dump` every 15 min (staging), nightly full for prod |
| RTO (time to recover) | ≤ 30 min | documented restore procedure + verified backup |

---

## 6. Failure runbook

### A. Corrupt / lost DB
1. Stop backend.
2. Restore from latest verified backup (§3 or §2).
3. Re-apply any migrations after the backup snapshot:
   `alembic upgrade head` (idempotent).
4. Restart backend; confirm `/api/health` OK; run staging smoke tests.

### B. Lost `JWT_SECRET` (can't decrypt broker creds)
1. Restore DB.
2. Re-link brokers manually (new encryptions with new secret) — encrypted old
   secrets are non-recoverable without the original secret.
3. Document the rotation in the audit trail.

### C. Restore to a different environment
- Use `.env` for that env; do **not** copy `.env.production` secrets into staging.
- Always run `alembic upgrade head` after restore.

---

## 7. Encrypted secrets hygiene

- `JWT_SECRET` → secret manager (EnvKey / AWS KMS / HashiCorp Vault / Doppler).
- `.env`, `.env.production`, `.env.staging` are gitignored (verified).
- Never store plaintext broker keys in backups; they live encrypted in the DB and
  depend on `JWT_SECRET` for decryption.
