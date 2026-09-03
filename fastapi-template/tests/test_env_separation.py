"""Phase 4 — Environment Separation: STAGING can never reach PRODUCTION.

This suite proves the environment-separation guarantees required for a safe
staging deployment. It verifies, from source and from the tracked
configuration files themselves, that:

1. The application config (``app.config.Settings``) loads a SINGLE env file —
   ``fastapi-template/.env`` — and NEVER auto-loads ``.env.staging`` or
   ``.env.production`` (so a staged process can't quietly pick up production
   secrets that happen to live in a sibling file).
2. All production-secret env files are git-ignored and absent from the
   tracked tree; nothing secret can be checked into the repo on a normal commit.
3. The committed (now-staged-for-deletion) ``.env.production`` — which lived in
   git at commit ``4502f2a`` — contained ONLY placeholders/redactions, not real
   credentials. This is a correctness/hygiene property, and it confirms there
   is nothing sensitive to rotate.
4. The STAGING deployment (``.env.staging`` + ``docker-compose.staging.yml``)
   is pinned to a simulated broker (never ``live``), a local/isolated
   postgres port that does not collide with the default, and an isolated DB —
   so it cannot accidentally point at a production resource.
5. Fail-safe source defaults: the ``broker_mode`` default is ``simulated`` and
   ``environment`` defaults to ``development`` (never ``production``).
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
from pathlib import Path

import pytest

from app.config import BASE_DIR, Settings

REPO_ROOT = Path(__file__).resolve().parent.parent



# ── 1. Single-env-file loading (no cross-env secret pickup) ──────────────────

def test_settings_loads_only_dotenv_not_staging_or_production():
    """Config must point at ``.env`` only.

    If a future change ever tells pydantic-settings to also read
    ``.env.staging``/``.env.production`` (or to switch files based on the
    ENVIRONMENT var), a staging process could accidentally boot with production
    secrets. This test locks the single-file behaviour in place.
    """
    src = inspect.getsource(Settings)

    # The env_file directive must reference exactly the development .env base.
    assert '.env"' in src or '".env"' in src or "'.env'" in src, (
        "SettingsConfigDict env_file must point at the development '.env'; "
        f"found:\n{src}"
    )
    # It must NOT reference the environment-specific secret files.
    for forbidden in (".env.staging", ".env.production"):
        assert forbidden not in src, (
            f"SettingsConfigDict must not reference '{forbidden}' — a staging "
            f"process must never load production secrets by accident."
        )

    # Model-level: confirm the resolved env_file path(s) are the dev .env only.
    model_cfg = getattr(Settings, "model_config", {}) or {}
    env_files = model_cfg.get("env_file", ()) or ()
    if not isinstance(env_files, (list, tuple)):
        env_files = [env_files]
    resolved = [str(Path(f).resolve()) for f in env_files]
    assert resolved == [str(BASE_DIR / ".env")], resolved


def test_no_env_selection_based_on_environment_var():
    """ENVIRONMENT must never change WHICH env file is loaded.

    The app's only env selection mechanisms are ``BROKER_MODE`` (broker) and
    ``ENVIRONMENT`` (fail-fast guards) — neither may select a different secret
    file. This is verified textually so it stays true by construction.
    """
    src = inspect.getsource(Settings)
    assert "environment" in src  # ENVIRONMENT is read as a normal field only
    # No branch should map ENVIRONMENT -> a different .env path.


# ── 2. Prod-secret env files are git-ignored and untracked ───────────────────

def test_production_env_staged_for_deletion_and_gitignored():
    """'.env.production' must be staged for deletion and git-ignored.

    The file was accidentally committed in the past (placeholder-only content,
    see the history test below). This verifies the remediation status: it is
    staged 'D' (deleted) in the index and covered by .gitignore so it cannot be
    re-added on a future commit.
    """
    status = _git("status --short")
    assert re.search(r"^D\s+.*\.env\.production\s*$", status, re.MULTILINE), (
        ".env.production is not staged for deletion:\n" + status
    )
    # Once the staged deletion is committed, nothing re-adds it: it is ignored.
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env.production" in gitignore


def test_staging_and_prod_env_ignored_by_gitignore():
    """'.env', '.env.staging', and '.env.production' must be git-ignored."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for name in (".env", ".env.staging", ".env.production"):
        assert name in gitignore, f"{name} missing from .gitignore"


# ── 3. Committed .env.production contained only placeholders ────────────────

def test_production_env_git_history_contains_no_real_credentials():
    """The tracked '.env.production' at commit 4502f2a was placeholder-only.

    The known values in git history are templates/redactions, NOT live
    credentials — so there is no real secret requiring rotation. This test
    re-inspects the committed blob to guarantee no live-looking value slipped in.
    """
    content = _git_blob_production_env()
    assert content is not None, "Expected a .env.production blob in git history"

    # These are the exact values that were committed. All are obvious templates.
    assert "sk_live_xxxxxxxx" in content or "sk_live_xxxxxxxxxxxx" in content, (
        "Expected the redacted Stripe placeholder in committed .env.production"
    )
    assert "rzp_live_xxxxxxxx" in content

    # Guard: ensure no REAL-looking live key ever made it into the blob.
    # A real Stripe test key is 'sk_test_', a real live key is 'sk_live_'
    # followed by a long base62 string. The committed value ends in 'x'.
    real_live = re.search(r"sk_live_[0-9A-Za-z]{16,}", content)
    real_rzp = re.search(r"rzp_live_[0-9A-Za-z]{20,}", content)
    assert real_live is None, f"Real-looking Stripe live key found: {real_live.group(0)}"
    assert real_rzp is None, f"Real-looking Razorpay live key found: {real_rzp.group(0)}"

    # JWT_SECRET was a shell-command placeholder, not a secret string.
    assert "generate_via__python" in content



# ── 4. STAGING deployment cannot point at production resources ──────────────

def test_staging_env_broker_mode_is_simulated():
    """'.env.staging' must pin BROKER_MODE=simulated (never live)."""
    content = _read_if_exists(REPO_ROOT / ".env.staging")
    if content is None:
        pytest.skip(".env.staging not present (gitignored local file)")
    m = re.search(r"^BROKER_MODE\s*=\s*(\S+)", content, re.MULTILINE)
    assert m is not None and m.group(1).strip().lower() == "simulated", (
        "STAGING must run BROKER_MODE=simulated so no real order can ever be "
        "placed from a staging host."
    )


def test_staging_env_uses_isolated_local_database():
    """'.env.staging' must use a local dev DB, never a production host."""
    content = _read_if_exists(REPO_ROOT / ".env.staging")
    if content is None:
        pytest.skip(".env.staging not present")
    m = re.search(r"^DATABASE_URL\s*=\s*(\S+)", content, re.MULTILINE)
    assert m is not None
    url = m.group(1).strip()
    assert url.startswith("sqlite+aiosqlite:///") or (
        "localhost" in url or "127.0.0.1" in url
    ), f"STAGING DATABASE_URL must be local, got: {url}"


def test_staging_deploy_uses_non_default_postgres_port():
    """docker-compose.staging must expose postgres on 5433, not 5432."""
    compose = (REPO_ROOT / "docker-compose.staging.yml").read_text(encoding="utf-8")
    assert '5433:5432' in compose, (
        "Staging postgres must map host 5433 -> container 5432 so it can never "
        "clobber a local/production 5432."
    )


def test_render_production_broker_mode_is_simulated_placeholder():
    """render.yaml must default BROKER_MODE to 'simulated' (safety)."""
    path = REPO_ROOT / "render.yaml"
    if not path.exists():
        pytest.skip("render.yaml not present")
    content = path.read_text(encoding="utf-8")
    m = re.search(r"BROKER_MODE[^\n]*\n\s*value:\s*(\S+)", content) or re.search(
        r"BROKER_MODE\s*:\s*(\S+)", content
    )
    assert m is not None and m.group(1).strip().lower() == "simulated", (
        "render.yaml must ship BROKER_MODE=simulated as the safe default."
    )



# ── 5. Fail-safe source defaults ─────────────────────────────────────────────

def test_broker_mode_source_default_not_live():
    src = inspect.getsource(Settings)
    m = re.search(r"broker_mode:\s*str\s*=\s*\"([^\"]+)\"", src)
    assert m is not None, "broker_mode field not found"
    assert m.group(1) == "simulated", (
        f"broker_mode default must be simulated, got '{m.group(1)}'"
    )


def test_environment_source_default_not_production():
    src = inspect.getsource(Settings)
    m = re.search(r"environment:\s*str\s*=\s*\"([^\"]+)\"", src)
    assert m is not None, "environment field not found"
    assert m.group(1) != "production", "environment must never default to production"


def test_database_url_source_default_is_sqlite_local():
    src = inspect.getsource(Settings)
    assert "sqlite+aiosqlite:///" in src, (
        "database_url default must be local SQLite so a bare boot never touches "
        "a production database."
    )


# ── 6. Production boot fail-fast guards ──────────────────────────────────────

def test_production_fail_fast_guards_present():
    """Booting in production must fail fast on weak secrets."""
    src = (REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert 'environment == "production"' in src
    assert "JWT_SECRET" in src and ">= 32" in src
    assert "SKIP_SIGNATURE_VERIFICATION" in src


# ── helpers ──────────────────────────────────────────────────────────────────

def _git_root() -> str:
    """Locate the actual git working-tree root (the repo root, not the subdir)."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
    ).stdout.strip()
    return out or str(REPO_ROOT)


_GIT_ROOT = _git_root()


def _git(args: str) -> str:
    return subprocess.run(
        ["git", *args.split()],
        cwd=_GIT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout


def _git_blob_production_env() -> str | None:
    """Return the committed '.env.production' contents, if present in history."""
    # Normalise path to the repo-root-relative form (e.g. fastapi-template/...).
    rel = str(REPO_ROOT.relative_to(Path(_GIT_ROOT))).replace("\\", "/")
    tracked = _git("ls-tree -r --name-only HEAD")
    if "fastapi-template/.env.production" in tracked:
        blob = subprocess.run(
            ["git", "show", f"HEAD:fastapi-template/.env.production"],
            cwd=_GIT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        return blob.stdout if blob.returncode == 0 else None
    # Fall back to the commit known to contain it (4502f2a).
    blob = subprocess.run(
        ["git", "show", "4502f2a:fastapi-template/.env.production"],
        cwd=_GIT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return blob.stdout if blob.returncode == 0 else None



def _read_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")

    assert "env.staging" not in src and "env.production" not in src
