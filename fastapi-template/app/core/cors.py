"""Centralised, testable CORS configuration.

Production must never grant credentialed browser access to arbitrary public
``*.vercel.app`` deployments.  ``vercel.app`` is a public hosting surface:
any third party can register ``https://evil.vercel.app`` or a prefix-squat
like ``https://tradethrone-evil.vercel.app`` and serve a hostile page.  A
regex cannot distinguish a genuine official preview deploy hash from an
attacker-registered project slug on the shared domain, so **production uses
exact origins only** — the two official TradeThrone hosts, any operator-set
``ALLOWED_ORIGINS`` values, and the localhost dev frontends.  Deployments that
need a specific Vercel preview origin add its exact URL to ``ALLOWED_ORIGINS``.

Development keeps a permissive posture so local workflow (any Vercel preview)
stays friction-free.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Official deployment surface ─────────────────────────────────────────────
# Exact production origins.  Localhost dev frontends are kept because a page
# on the operator's own localhost is not an attacker-controlled origin; this
# keeps ``npm run dev`` working against any deployment.
DEFAULT_PROD_ORIGINS: list[str] = [
    "https://tradethrone.vercel.app",
    "https://tradethron.vercel.app",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
]

# Development only: the entire Vercel host space, so local testing against any
# preview/deploy subdomain keeps working.  Never used in production.
DEV_VERCEL_REGEX: str = r"https://([a-z0-9-]+\.)*vercel\.app"


@dataclass(frozen=True)
class CorsConfig:
    """The exact inputs Starlette's CORSMiddleware should be given."""

    origins: list[str]
    origin_regex: str | None = None


def build_cors_config(
    *,
    environment: str,
    allowed_origins: str,
    frontend_url: str,
) -> CorsConfig:
    """Resolve (origins, origin_regex) for the given environment.

    Behaviour is identical to the historical config except for one deliberate
    hardening: in production ``origin_regex`` is ``None`` — no ``*.vercel.app``
    host (nor any prefix-squat variant of the official slugs) is trusted via
    regex.  Extra exact origins (e.g. an official Vercel preview URL) must be
    listed explicitly via ``ALLOWED_ORIGINS``.
    """
    origins = [o.strip() for o in (allowed_origins or "").split(",") if o.strip()]
    if frontend_url and frontend_url not in origins and "*" not in origins:
        origins.append(frontend_url)

    if not origins or origins == ["*"]:
        if environment == "production":
            origins = list(DEFAULT_PROD_ORIGINS)
        else:
            origins = ["*"]

    origin_regex = None if environment == "production" else DEV_VERCEL_REGEX
    return CorsConfig(origins=origins, origin_regex=origin_regex)