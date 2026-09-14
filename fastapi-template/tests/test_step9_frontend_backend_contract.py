"""Phase 1 Step 9 — full-stack frontend↔backend contract regression.

Codifies the Step-9 contract audit as executable coverage so a future
refactor can never silently break the FE→BE surface:

  1. **Every endpoint reference hard-coded in ``client/src`` must resolve to a
     route registered on the running FastAPI app** (HTTP + WebSocket). Query
     strings are stripped, ``${…}`` splices are normalized to ``{param}``, and
     ``${API_BASE}/api/…`` absolute forms are unwrapped — the same
     normalization the audit report documents.
  2. **``GET /api/trades/positions`` is an authenticated-only contract.** The
     function signature uses the *optional* dependency, but the dependency is
     ``get_current_user`` which raises 401 for anonymous callers — the
     endpoint is effectively authenticated, and the frontend always calls it
     with a bearer token.  This test pins that behaviour (fail-closed, no
     silent anonymous exposure).
  3. **The legacy ``/api/trades/api/positions`` alias remains wired** to the
     same handler as ``/api/trades/positions`` (intentional, documented).

The FRONTEND_ROOT is the repo-relative ``client/src`` tree.
"""

from __future__ import annotations

import io
import os
import re

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_ROOT = os.path.join(REPO_ROOT, "client", "src")

# ── Frontend endpoint-reference extractors (mirrors _audit_fe_calls2.txt) ────
_STATIC = re.compile(r"['\"`](/(?:api|ws)/[^'\"`{}]*?)['\"`]")
_TEMPLATE = re.compile(r"`((?:/api|ws)/[^`]*?\$\{[^`]*\}[^`]*?)`")
_BASE_TEMPLATE = re.compile(r"`\$\{[^}]+}((/api|ws)/[^`]*?)`")


def _normalize_fe(path: str) -> str:
    """Strip query strings and flatten `${expr}` splices to a path fragment."""
    path = path.split("?", 1)[0]
    # Drop incomplete trailing splices ("${query" on multi-line templates) —
    # the remainder is the static path, which is what resolves.
    path = re.sub(r"\$\{\w+\s*$", "", path)
    # `${expr}` -> `{param}` so all paramised splices normalise identically
    path = re.sub(r"\$\{[^}]*}", "{param}", path)
    return path


def _collect_frontend_refs() -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for dirpath, _dirs, files in os.walk(FRONTEND_ROOT):
        for fn in files:
            if not fn.endswith((".jsx", ".js")):
                continue
            # Test files hard-code fake urls (/api/foo…) — never part of the
            # real surface contract.
            if ".test." in fn:
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT).replace("\\", "/")
            with io.open(os.path.join(dirpath, fn), "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for m in _STATIC.finditer(text):
                refs.append((rel, _normalize_fe(m.group(1))))
            for m in _TEMPLATE.finditer(text):
                refs.append((rel, _normalize_fe(m.group(1))))
            for m in _BASE_TEMPLATE.finditer(text):
                refs.append((rel, _normalize_fe(m.group(1))))
    return refs


def _collect_backend_templates() -> set[str]:
    """Walk every route (descending into lazily-mounted included routers)."""
    templates: set[str] = set()

    def _walk(routes) -> None:
        for route in routes:
            # Lazily-mounted FastAPI router — descend into the source router.
            original = getattr(route, "original_router", None)
            if original is not None and getattr(original, "routes", None):
                _walk(original.routes)
                continue
            nested = getattr(route, "routes", None)
            if nested:  # Mount — descend
                _walk(nested)
                continue
            path = getattr(route, "path", None)
            if not path:
                continue
            if path.startswith("/api") or path.startswith("/ws"):
                templates.add(path)

    _walk(app.routes)
    return templates


def _regex_for(template: str) -> re.Pattern:
    """Turn a Starlette path template into a full-match regex."""
    parts = []
    for seg in template.split("/"):
        if not seg:
            continue
        if seg.startswith("{") and seg.endswith("}"):
            parts.append("[^/]+")
        else:
            parts.append(re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "$")


def _resolve(fe_ref: str, backend_templates: set[str]) -> str | None:
    for template in backend_templates:
        if _regex_for(template).match(fe_ref):
            return template
    return None


# Documented, reference-only frontend artifacts that are NOT network calls:
#   * ``/api/agent`` — prose comment in AgentConsole.jsx describing the
#     control-plane router base (verified: no bare ``/api/agent`` route exists
#     and none is called).
_NON_CALL_COMMENTS = {
    ("client/src/pages/AgentConsole.jsx", "/api/agent"),
}

# The Agent Console uses a closed enum of lifecycle suffixes against
# ``/api/agent/config/{action}``.  Each concrete value must exist as a route.
_AGENT_LIFECYCLE_SUFFIXES = ("start", "pause", "resume", "stop")


def _resolve_agent_lifecycle(backend_templates: set[str]) -> str | None:
    for suffix in _AGENT_LIFECYCLE_SUFFIXES:
        candidate = f"/api/agent/config/{suffix}"
        if candidate not in backend_templates:
            return None
    return "/api/agent/config/{action} (start|pause|resume|stop)"


def test_every_frontend_endpoint_reference_resolves_to_a_backend_route():
    fe = _collect_frontend_refs()
    assert fe, "test harness must discover frontend references"
    be = _collect_backend_templates()

    unresolved: list[tuple[str, str]] = []
    verified: set[str] = set()
    for rel, ref in sorted(fe):
        if (rel, ref) in _NON_CALL_COMMENTS:
            continue  # prose comment, not a network call
        target = _resolve(ref, be)
        if target is None and ref == "/api/agent/config/{param}":
            target = _resolve_agent_lifecycle(be)
        if target is None:
            unresolved.append((rel, ref))
            continue
        verified.add(f"{rel} -> {target}")

    assert not unresolved, (
        "Frontend references without a matching backend route:\n"
        + "\n".join(f"  {rel}: {ref}" for rel, ref in unresolved)
    )


def test_legacy_positions_alias_and_canonical_positions_both_registered():
    be = _collect_backend_templates()
    assert "/api/trades/positions" in be
    # Documented intentional legacy alias — same handler as canonical route.
    assert "/api/trades/api/positions" in be


def test_private_positions_listing_has_no_anonymous_contract():
    """GET /api/trades/positions must not serve unfiltered data anonymously.

    The handler's ``Optional[UserRecord]`` annotation is *not* an anonymous
    allowance: the dependency is ``get_current_user`` (raises 401).  This
    test pins the fail-closed contract the frontend relies on (every
    positions consumer calls it with a bearer token).
    """
    client = TestClient(app)
    resp = client.get("/api/trades/positions")
    assert resp.status_code in (401, 403), (
        "Anonymous /api/trades/positions must be rejected (no silent "
        f"unscoped exposure); got {resp.status_code}"
    )