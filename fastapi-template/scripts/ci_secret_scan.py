"""CI secret scanner — whole-repo, high-precision credential patterns.

Replaces the ad-hoc git grep snippets with a deterministic Python scanner that:

* Enumerates every file tracked by git (``git ls-files``) from the repo root —
  including documentation and config that the ``app/tests/scripts``-only grep
  in the old CI missed.
* Skips binary files, lockfiles, node_modules, and the committed ``.env.example``
  / ``.env.production.example`` placeholder templates (which legitimately
  contain ``your_*`` / ``replace_with_*`` sample values).
* Applies precise regular expressions that match only *real* credential shapes
  (live Stripe/Razorpay/AWS/GitHub/Slack keys, CoinGecko keys, private keys,
  32+ char env-assigned JWT secrets, and ``KEY=value`` env-file dumps).
* Exits non-zero and lists every hit path on failure, so CI fails the build
  before a leaked credential can be merged.

Usage (from the repo root):
    python scripts/ci_secret_scan.py

Exit codes: 0 = clean, 1 = secrets found, 2 = usage/environment error.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT_ROOT = REPO_ROOT.parent

# ── Real-credential patterns (file content, whole tracked tree) ──────────────
_B64 = "[A-Za-z0-9]"
CONTENT_PATTERNS = {
    # LIVE secrets only. TEST secrets (sk_test_/rzp_test_) are deliberately
    # allowed: they cannot move real money, and test suites legitimately commit
    # them as fixtures. CI's app/tests/scripts pass still blocks test keys from
    # app/ source, but a test file using an rzp_test_ fixture is fine.
    "stripe_live": re.compile("sk_live_" + _B64 + "{10,}"),
    "razorpay_live": re.compile("rzp_live_" + _B64 + "{10,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile("ghp_" + _B64 + "{20,}"),
    "slack_token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "coingecko_key": re.compile("CG-" + _B64 + "{15,}"),
    "anthropic_key": re.compile("sk-ant-" + _B64 + "{20,}"),
    "private_key": re.compile(
        "-----BEGIN" + " (RSA|OPENSSH|EC|DSA|PRIVATE) " + "PRIVATE KEY-----"
    ),
    # Env-style assignments are flagged only when the value is *not* an obvious
    # documentation placeholder (<...> or ...). 32+ char JWT_SECRET values even
    # inline are flagged (the known-bad historical value was that long).
    "long_jwt_env": re.compile(
        r"JWT_SECRET\s*=\s*" + _B64 + r"[A-Za-z0-9_+\-]{31,}"
    ),
    "env_dump_eq": re.compile(
        r"(?m)^(?:\s*)(?:"
        r"ANGEL_API_KEY|ANGEL_CLIENT_CODE|ANGEL_PASSWORD|ANGEL_TOTP_SECRET"
        r"|RAZORPAY_KEY_ID|RAZORPAY_KEY_SECRET|RAZORPAY_WEBHOOK_SECRET"
        r"|TWILIO_AUTH_TOKEN|TWILIO_ACCOUNT_SID|STRIPE_SECRET_KEY"
        r"|RESEND_API_KEY|TRADETHRONE_WEBHOOK_SECRET|UPSTASH_REDIS_URL"
        r"|DATABASE_URL)"
        r"\s*=\s*(\S+?)"
        r"(?:\s|#.*)?(?:$|(?=\r?\n))"
    ),
}

# ── Files that are allowed to contain sample/placeholder values ───────────────
_EXCLUDE_NAMES = {
    ".env.example",
    ".env.production.example",
    ".env.local.example",
    "package-lock.json",
    "yarn.lock",
    "pipfile.lock",
    "poetry.lock",
    "requirements.txt",
}
_EXCLUDE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".svg", ".pyc", ".pdf", ".zip", ".gz", ".bin", ".db", ".sqlite", ".sqlite3",
}
_EXCLUDE_PREFIXES = (
    "client/node_modules/",
    "fastapi-template/.venv/",
    ".git/",
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=GIT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        print(f"error: git ls-files failed: {out.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return out.stdout.splitlines()


def _should_skip(rel: str) -> bool:
    name = Path(rel).name.lower()
    if name in _EXCLUDE_NAMES:
        return True
    if rel.lower().endswith(tuple(_EXCLUDE_SUFFIXES)):
        return True
    if rel.startswith(_EXCLUDE_PREFIXES):
        return True
    return False


def _looks_like_placeholder(value: str) -> bool:
    """True if the env-assignment value is clearly a doc placeholder."""
    stripped = value.strip().strip('"').strip("'")
    return (
        not stripped
        or stripped.startswith("<")
        or "..." in stripped
        or stripped.lower().startswith(("your_", "replace_with"))
        or stripped in {"secret", "changeme", "change-me", "xxx", "xxxx"}
    )


def main() -> int:
    tracked = _tracked_files()
    hits: list[tuple[str, str]] = []
    for rel in tracked:
        if _should_skip(rel):
            continue
        path = GIT_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_bytes()
        except OSError as exc:
            print(f"warning: cannot read {rel}: {exc}", file=sys.stderr)
            continue
        # Binary detection: reject if NUL in the first 8 KiB.
        if b"\x00" in text[:8192]:
            continue
        try:
            content = text.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - non-UTF8 text files are uncommon
            continue
        for name, rx in CONTENT_PATTERNS.items():
            found = False
            if name == "env_dump_eq":
                for m in rx.finditer(content):
                    if not _looks_like_placeholder(m.group(1)):
                        found = True
                        break
            elif rx.search(content):
                found = True
            if found:
                hits.append((rel, name))
                break  # one hit per file is enough for the report
    if hits:
        print("S E C U R I T Y   S C A N   F A I L E D", file=sys.stderr)
        print("Credential-like patterns found in tracked files:", file=sys.stderr)
        for rel, name in sorted(hits):
            print(f"  - {name}: {rel}", file=sys.stderr)
        print(
            "Action: remove the real credential value from tracked source and "
            "rotate it (treat it as compromised).",
            file=sys.stderr,
        )
        return 1
    print(f"Secret scan clean ({len(tracked)} tracked files checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())