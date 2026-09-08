"""P0 Secret Remediation regression tests.

Scans the ENTIRE tracked repository (including repo-root documentation and
config, which the narrower ``test_p0_remediation`` scanner that only walks
``fastapi-template/`` does not cover) for real credential values or patterns.

Historically, real production secrets (a CoinGecko key, Angel One broker
credentials, JWT secrets, a Supabase DB URL, an Upstash Redis URL, a Stripe
live key, Razorpay keys and a Claude API key) were committed via `.env`
snapshots and persisted in git history. Those specific literal values were
removed from all tracked documentation and source; this suite locks out any
reoccurrence anywhere in the tracked tree.

No secret values are asserted verbatim here — patterns only.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

GIT_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
REPO_ROOT = Path(__file__).resolve().parent.parent  # fastapi-template/


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=GIT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return out.stdout


def _tracked_files() -> list[str]:
    """All files tracked by git under the whole repo root."""
    return [p for p in _git("ls-files").splitlines() if p]


# Real secret-shaped patterns that must NEVER appear in tracked files.
# These are assembled via concatenation so this very file's source never
# literally contains the tokens the CI grep and these tests detect.
_B64 = "[A-Za-z0-9]"
SECRET_PATTERNS = {
    "coingecko_key": re.compile("CG-" + _B64 + "{20,}"),
    "angel_key_id": re.compile(r"AACE[0-9]{6,}"),
    "jwt_env_literal": re.compile(
        r"JWT_SECRET\s*=\s*" + _B64 + "{32,}"
    ),
    "stripe_live": re.compile("sk_live_" + _B64 + "{10,}"),
    "stripe_test": re.compile("sk_test_" + _B64 + "{10,}"),
    "razorpay_live": re.compile("rzp_live_" + _B64 + "{10,}"),
    "claude_key": re.compile("sk-ant-" + _B64 + "{20,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "gh_token": re.compile("ghp_" + _B64 + "{20,}"),
    "jwt_known": re.compile(
        "(?:super[-_]secret[-_]jwt[-_]key|dev[-_]super[-_]secret[-_]jwt[-_]key)"
    ),
    "env_secret_assign": re.compile(
        r"(?m)^\s*(?:ANGEL_API_KEY|ANGEL_CLIENT_CODE|ANGEL_PASSWORD"
        r"|ANGEL_TOTP_SECRET|RAZORPAY_KEY_ID|RAZORPAY_KEY_SECRET"
        r"|RAZORPAY_WEBHOOK_SECRET|TWILIO_AUTH_TOKEN|STRIPE_SECRET_KEY"
        r"|RESEND_API_KEY|TRADETHRONE_WEBHOOK_SECRET)\s*=\s*\S+"
    ),
}

# Whole-repo scan (excluding .env example files which legitimately use
# placeholder values, and excluding binary artifacts).
_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
    ".eot", ".svg", ".pyc", ".svg", ".pdf", ".zip",
)
_SKIP_PREFIXES = ("client/node_modules/", "fastapi-template/.venv/")


def _is_ignored(rel: str) -> bool:
    return (
        rel.endswith(_SKIP_SUFFIXES)
        or any(rel.startswith(p) for p in _SKIP_PREFIXES)
        or rel in ("package-lock.json", "yarn.lock", "Pipfile.lock")
        or rel.endswith(".env.example")
        or rel.endswith(".env.production.example")
    )


@pytest.mark.parametrize("name", sorted(SECRET_PATTERNS), ids=lambda n: n)
def test_no_real_secret_patterns_anywhere_tracked(name):
    """No tracked file in the whole repo may contain a real secret pattern."""
    rx = SECRET_PATTERNS[name]
    hits = []
    for rel in _tracked_files():
        if _is_ignored(rel):
            continue
        path = GIT_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if rx.search(text):
            hits.append(rel)
    assert not hits, (
        f"Pattern '{name}' matched in tracked files: {hits}. "
        "Remove the real credential value and rotate it before merging."
    )


_ENV_DUMP_PATTERNS = re.compile(
    r"(?m)^\s*(?:JWT_SECRET|DATABASE_URL|UPSTASH_REDIS_URL|REDIS_URL"
    r"|STRIPE_SECRET_KEY|ANGEL_API_KEY|ANGEL_CLIENT_CODE|ANGEL_PASSWORD"
    r"|RAZORPAY_KEY_SECRET)\s*=\s*[^\s#]"
)


def test_no_env_secret_dumps_in_tracked_docs():
    """Repo-root docs must not contain real env-file secret assignments."""
    hits = []
    for rel in _tracked_files():
        if _is_ignored(rel) or not rel.startswith(("PRODUCTION_", "README", "docs/")):
            continue
        path = GIT_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _ENV_DUMP_PATTERNS.finditer(text):
            hits.append((rel, m.group(0).split("=")[0].strip()))
    assert not hits, f"Real env-secret assignments found in tracked docs: {hits}"


def test_tracked_dotenv_ignored():
    """The real env/secret files must all be git-ignored and untracked."""
    for name in (".env", ".env.production", ".env.staging"):
        path = REPO_ROOT / name
        if path.exists():
            tracked = _git("ls-files", str(path.relative_to(GIT_ROOT)))
            assert not tracked, f"{name} must never be tracked (found in git)"
    # Nothing under a path containing '.env.' other than the examples may be
    # tracked.
    for rel in _tracked_files():
        base = Path(rel).name
        if base.startswith(".env") and "example" not in base:
            raise AssertionError(f"Untracked-worthy env file is tracked: {rel}")


# ─── Known-bad JWT secret guard ──────────────────────────────────────────────
import hashlib

from pydantic import ValidationError

from app.config import _KNOWN_BAD_JWT_SECRET_SHA256, Settings


def _prod_settings(**overrides) -> Settings:
    """Construct production Settings with all safe flags pinned (no .env pickup)."""
    base = dict(
        environment="production",
        jwt_secret="h" * 48,
        skip_signature_verification=False,
        webhook_local_mode=False,
        database_url=(
            "postgresql://" + "u:test_password" + "@db.example.internal/tradetron"
        ),
        upstash_redis_url=(
            "rediss://" + "u:test_password" + "@redis.example.internal:6379/0"
        ),
        broker_mode="simulated",
    )
    base.update(overrides)
    return Settings(**base)


def test_known_bad_jwt_placeholder_hash_recorded():
    """The known-bad literal's digest must be in the blocklist (sanity)."""
    digest = hashlib.sha256(b"your_jwt_secret_here").hexdigest()
    assert digest in _KNOWN_BAD_JWT_SECRET_SHA256


def test_production_rejects_historically_committed_jwt_value():
    """The exact placeholder once committed in history must not boot prod."""
    # Build from fragments so the raw literal never appears in source.
    known_bad = "_".join(["dev", "super", "secret", "jwt", "key"])
    with pytest.raises(ValidationError, match="previously committed"):
        _prod_settings(jwt_secret=known_bad + "-" + "change" + "-in-" + "production")


def test_production_rejects_common_placeholder_jwt_values():
    """Common doc placeholders must also fail the known-bad guard.

    Values shorter than 32 chars are rejected by the length gate; the 34-char
    template secret is caught by the known-bad digest blocklist. Either way
    production must refuse to boot.
    """
    for chunk in (["your_jwt_secret_here"], ["a_secure_random_64_char_hex_secret"]):
        with pytest.raises(ValidationError):
            _prod_settings(jwt_secret=chunk[0])


def test_production_still_accepts_fresh_random_jwt():
    """A strong random secret (the only safe value) still boots."""
    s = _prod_settings(jwt_secret="h" * 48)
    assert s.environment == "production"
