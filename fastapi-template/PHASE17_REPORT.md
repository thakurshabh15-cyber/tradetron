# PHASE 17 REPORT — Strategy Deployment Tenant-Scoping (owner column)

**HEAD after commit:** `e1f5f959` → new commit
**Alembic head:** `0005_strategy_deployments_owner`
**Date:** 2026-09-09

## Summary

Fixed the documented Phase 15/16 backlog item: `strategy_deployments` carried
**no owner column**, so a strategy deployment could not be attributed to the
user who deployed it. This made every admin / per-user deployment surface
(`/strategies/oversight`, per-user halts) tenant-ambiguous and prevented
tenant-scoped deployment queries.

## Defect (P1, cross-tenant operational)

- `app/models/marketplace.py::StrategyDeploymentRecord` had **no `owner_user_id`**.
- `POST /api/strategies/{id}/deploy` created deployment rows with no owner
  identity; admin `/strategies/oversight` returned them with no owner.
- Phase 15's `kill_switch_user` had to work around this by disabling the
  target's `StrategyRecord.enabled` instead of pausing deployments, and
  recorded the owner-column as an explicit backlog P1.

## RED → GREEN

- **RED:** `tests/test_phase17_deployment_owner_red.py` — two tests:
  1. A deployment created via `POST /deploy` MUST persist
     `owner_user_id == <authenticated user id>` (derived from the bearer
     token, never client-supplied). Pre-fix the deployment had no owner
     (`getattr(row, "owner_user_id", None)` → `None`) → **assert failed**.
  2. Admin `/strategies/oversight` MUST expose the deployment owner. Pre-fix
     the response carried no `owner_user_id` → **assert failed**.
- **Fix:**
  - **Model** (`marketplace.py`): added nullable `owner_user_id`
    (String(36), FK→`users.id` `ON DELETE SET NULL`, indexed).
  - **Migration** (`alembic/versions/0005_strategy_deployments_owner.py`):
    inspector-guarded, online (nullable) add-column + index; guarded against a
    missing table (startup-migration minimal-DB tests); downgrade best-effort.
  - **Deploy endpoint** (`strategies.py`): sets `owner_user_id=user.id`.
  - **Admin oversight** (`admin.py`): exposes `owner_user_id`.
  - **Dev/test bootstrap** (`db/session.py`): adds the column to pre-existing
    local SQLite DBs via the legacy idempotent ALTER list (dev/test only).
- **GREEN:** both RED tests pass.
- **Regression:** full suite **651 passed** (was 649), startup-migration head
  assertion updated to `0005_strategy_deployments_owner` (targeted migration
  tests green).

## Verification

| Gate | Result |
|------|--------|
| pytest (full) | **651 passed** |
| Alembic drift | single head `0005`, empty-DB upgrade clean, 23-table parity |
| Secret scan | clean (360 tracked files) |
| Python compile | clean |
| Frontend build | clean (0 errors) |
| Frontend lint | 0 errors / 25 warnings (unchanged baseline) |
| `git diff --check` | clean |

## External dependency

PostgreSQL FK `ON DELETE SET NULL` + index on a real Postgres is an external
operator acceptance check (SQLite-local verified only).
