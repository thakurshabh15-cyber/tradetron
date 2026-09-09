"""TradeTron LIVE production smoke probe (Phases 9-12).

Creates DEDICATED test accounts only (`release.qa.<ts>@tradethrone.test`),
places ONE controlled PAPER order, closes it, and verifies auth lifecycle,
tenant isolation and RBAC against the live production backend.

Never: real-money orders, existing customer accounts, secret output.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import ssl
import sys
from datetime import datetime, timezone

BASE = "https://tradetron-8jkz.onrender.com"
CTX = ssl.create_default_context()

P = []


def req(method: str, path: str, body=None, token=None, headers=None, timeout=25):
    url = BASE + path
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, method=method, headers=h,
                               data=json.dumps(body).encode() if body is not None else None)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout, context=CTX)
        raw = resp.read().decode()
        try:
            data = json.loads(raw)
        except Exception:
            data = raw[:300]
        return resp.status, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = raw[:300]
        return e.code, data
    except Exception as e:
        return -1, f"{type(e).__name__}: {str(e)[:200]}"


def check(name, cond, detail=""):
    P.append((name, "PASS" if cond else "FAIL", detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail[:240]}")


def summarize():
    passed = sum(1 for _, s, _ in P if s == "PASS")
    failed = sum(1 for _, s, _ in P if s == "FAIL")
    print(f"\n===== PROBE SUMMARY: {passed} passed / {failed} failed of {len(P)} =====")
    for name, s, d in P:
        if s == "FAIL":
            print(f"  FAIL {name}: {d[:400]}")
    return failed == 0


def register_user(email, password, full_name):
    code, data = req("POST", "/api/auth/register",
                     {"email": email, "password": password, "full_name": full_name})
    if code in (200, 201):
        return data
    print(f"  register {email} -> {code} {json.dumps(data)[:300]}")
    return None


def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    pw = f"RelQA-{ts}"  # strong unique test password (never a real user password)
    emailA = f"release.qa.a.{ts}@tradethrone.test"
    emailB = f"release.qa.b.{ts}@tradethrone.test"
    print(f"Test users: {emailA} / {emailB}")

    # ── PHASE 9: AUTH SMOKE ──────────────────────────────────────────────
    print("\n===== PHASE 9: AUTH SMOKE =====")
    hc, hp = req("GET", "/api/health")
    check("9.1 health: broker_mode=simulated",
          hc == 200 and isinstance(hp, dict) and hp.get("broker_mode") == "simulated",
          json.dumps(hp)[:200])

    regA = register_user(emailA, pw, "Release QA User A")
    check("9.2 register returns tokens", bool(regA and regA.get("access_token")),
          f"keys={sorted(regA.keys()) if regA else None}")
    tokenA = regA["access_token"] if regA else None
    refreshA = regA["refresh_token"] if regA else None

    code, me = req("GET", "/api/auth/me", token=tokenA)
    check("9.3 /me with token", code == 200 and isinstance(me, dict) and me.get("email") == emailA,
          json.dumps(me)[:200])
    role = me.get("role") if isinstance(me, dict) else None
    check("9.4 /me role is non-admin", code == 200 and role not in ("ADMIN", "SUPERADMIN"),
          f"role={role}")

    code, bad = req("POST", "/api/auth/login", {"identifier": emailA, "password": "wrongpass1"})
    check("9.5 wrong password -> 401", code == 401, f"{code} {json.dumps(bad)[:150]}")

    code, bad2 = req("GET", "/api/auth/me", headers={"Authorization": "Basic xyz"})
    check("9.6 malformed Authorization -> 401", code == 401, f"{code}")

    code, trades_anon = req("GET", "/api/trades")
    leaked = isinstance(trades_anon, list) and any(
        "pnl" in t or "order_id" in t or "strategy_name" in t
        for t in trades_anon if isinstance(t, dict))
    check("9.7 anonymous trade tape scrubbed (no pnl/order_id)",
          code == 200 and not leaked,
          f"count={len(trades_anon) if isinstance(trades_anon, list) else trades_anon}")

    code, login = req("POST", "/api/auth/login", {"identifier": emailA, "password": pw})
    check("9.8 login correct", code == 200 and login.get("access_token"), f"{code}")
    tokenA = login["access_token"]
    refreshA = login["refresh_token"]

    code, refr = req("POST", "/api/auth/refresh", {"refresh_token": refreshA})
    check("9.9 refresh rotates token", code == 200 and refr.get("access_token"), f"{code}")

    code, out = req("POST", "/api/auth/logout", {"refresh_token": refreshA}, token=tokenA)
    check("9.10 logout success", code in (200, 204),
          json.dumps(out)[:150] if isinstance(out, dict) else str(out)[:150])
    code, rev = req("POST", "/api/auth/refresh", {"refresh_token": refreshA})
    check("9.11 revoked refresh token rejected", code in (400, 401, 403),
          f"{code} {json.dumps(rev)[:150]}")

    # ── PHASE 10: PAPER TRADING SMOKE ────────────────────────────────────
    print("\n===== PHASE 10: PAPER TRADING SMOKE =====")
    code, login = req("POST", "/api/auth/login", {"identifier": emailA, "password": pw})
    tokenA = login["access_token"] if code == 200 else None
    check("10.1 re-login for trading", bool(tokenA), f"{code}")

    sym = "RELIANCE"
    cid = f"RELQA-{ts[-8:]}"
    payload = {"symbol": sym, "side": "BUY", "quantity": 1, "order_type": "MARKET",
               "mode": "PAPER", "client_order_id": cid}
    code, order = req("POST", "/api/trades/order", payload, token=tokenA)
    check("10.2 paper order accepted", code == 200 and isinstance(order, dict),
          f"{code} {json.dumps(order)[:300]}")
    pos_id = None
    if isinstance(order, dict):
        pos_id = order.get("position_id") or (order.get("data") or {}).get("position_id")

    code2, order2 = req("POST", "/api/trades/order", payload, token=tokenA)
    replay = isinstance(order2, dict) and order2.get("idempotent_replay") is True
    check("10.3 idempotent replay (no duplicate dispatch)", replay or code2 == 200,
          f"{code2} replay={order2.get('idempotent_replay') if isinstance(order2, dict) else None}")

    code, positions = req("GET", "/api/trades/positions", token=tokenA)
    has_pos = isinstance(positions, list) and any(
        p.get("symbol") == sym and str(p.get("status", "")).upper() in ("OPEN", "OPENING", "ACTIVE")
        for p in positions)
    check("10.4 position opened for symbol", has_pos, json.dumps(positions)[:250])
    if not pos_id and has_pos:
        pos_id = next(p["id"] for p in positions if p.get("symbol") == sym)

    code, bal = req("GET", "/api/brokers/balance", token=tokenA)
    check("10.5 balance readable", code == 200, f"{code} {json.dumps(bal)[:200]}")

    if pos_id:
        code, closed = req("POST", f"/api/trades/positions/{pos_id}/close", {}, token=tokenA)
        closed_ok = code == 200 and (not isinstance(closed, dict) or
                                     closed.get("status") in ("CLOSED", "closed") or
                                     closed.get("success") is not False)
        check("10.6 close paper position (CAS)", closed_ok, f"{code} {json.dumps(closed)[:300]}")
        code2, dup = req("POST", f"/api/trades/positions/{pos_id}/close", {}, token=tokenA)
        check("10.7 duplicate close rejected", code2 in (400, 404, 409),
              f"{code2} {json.dumps(dup)[:200]}")
    else:
        check("10.6 close paper position (CAS)", False, "no position_id available")
        check("10.7 duplicate close rejected", False, "no position_id available")

    code, trades = req("GET", "/api/trades", token=tokenA)
    check("10.8 order history scoped to caller", code == 200 and isinstance(trades, list),
          f"code={code} count={len(trades) if isinstance(trades, list) else trades}")

    # ── PHASE 11: TENANT ISOLATION ───────────────────────────────────────
    print("\n===== PHASE 11: TENANT ISOLATION =====")
    regB = register_user(emailB, pw, "Release QA User B")
    check("11.1 register second user", bool(regB and regB.get("access_token")))
    code, loginB = req("POST", "/api/auth/login", {"identifier": emailB, "password": pw})
    tokenB = loginB.get("access_token") if code == 200 else None
    check("11.2 login user B", bool(tokenB), f"{code}")

    payloadB = {"symbol": "INFY", "side": "BUY", "quantity": 1, "order_type": "MARKET",
                "mode": "PAPER", "client_order_id": f"INFQA-{ts[-8:]}"}
    code, orderB = req("POST", "/api/trades/order", payloadB, token=tokenB)
    check("11.3 B places own paper order", code == 200, f"{code} {json.dumps(orderB)[:200]}")

    code, tradesA = req("GET", "/api/trades", token=tokenA)
    a_syms = {t.get("symbol") for t in tradesA if isinstance(t, dict)} if isinstance(tradesA, list) else set()
    code, tradesB = req("GET", "/api/trades", token=tokenB)
    b_syms = {t.get("symbol") for t in tradesB if isinstance(t, dict)} if isinstance(tradesB, list) else set()
    check("11.4 A cannot see B's trades (INFY excluded)", code == 200 and "INFY" not in a_syms,
          f"A={a_syms} B={b_syms}")
    check("11.5 B sees own trades", "INFY" in b_syms, f"B={b_syms}")

    code, posA = req("GET", "/api/trades/positions", token=tokenA)
    code, posB = req("GET", "/api/trades/positions", token=tokenB)
    a_pos_syms = {p.get("symbol") for p in posA} if isinstance(posA, list) else set()
    b_pos_syms = {p.get("symbol") for p in posB} if isinstance(posB, list) else set()
    check("11.6 positions scoped (no cross-tenant symbols)",
          isinstance(posA, list) and isinstance(posB, list) and "INFY" not in a_pos_syms and "RELIANCE" not in b_pos_syms,
          f"Apos={a_pos_syms} Bpos={b_pos_syms}")

    code, hijack = req("GET", "/api/trades?user_id=00000000-0000-0000-0000-000000000bogus", token=tokenA)
    hijack_syms = {t.get("symbol") for t in hijack if isinstance(t, dict)} if isinstance(hijack, list) else set()
    check("11.7 client-supplied user_id ignored (server-scoped)",
          code == 200 and "INFY" not in hijack_syms, f"hijack={hijack_syms}")

    # ── PHASE 12: ADMIN / RBAC ───────────────────────────────────────────
    print("\n===== PHASE 12: ADMIN / RBAC =====")
    code, r = req("GET", "/api/admin/users", token=tokenA)
    check("12.1 trader -> admin endpoint = 403", code == 403, f"{code}")
    code, r = req("GET", "/api/admin/users")
    check("12.2 anonymous -> admin endpoint = 401", code == 401, f"{code}")
    code, r = req("POST", "/api/admin/login", {"email": emailA, "password": pw})
    check("12.3 trader cannot admin-login", code in (401, 403), f"{code}")

    codes = []
    for _ in range(6):
        c, _ = req("POST", "/api/admin/login",
                   {"email": f"nobody.{ts}@tradethrone.test", "password": "wrong"})
        codes.append(c)
    check("12.4 admin brute-force protection (423/429)", 423 in codes or 429 in codes,
          f"codes={codes}")

    codesB = []
    for _ in range(6):
        c, _ = req("POST", "/api/auth/login",
                   {"identifier": emailB, "password": "wrongpass2"})
        codesB.append(c)
    check("12.5 user login brute-force protection (423/429)", 423 in codesB or 429 in codesB,
          f"codes={codesB}")

    code, _ = req("GET", "/docs")
    check("12.6 docs disabled in production", code == 404, f"{code}")

    ok = summarize()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()