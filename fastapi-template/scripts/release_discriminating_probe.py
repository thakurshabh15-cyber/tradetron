"""Discriminating probe: unkeyed vs keyed vs DMA paper orders against live backend."""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://tradetron-8jkz.onrender.com"
CTX = ssl.create_default_context()


def rq(m, p, b=None, t=None):
    h = {}
    if t:
        h["Authorization"] = "Bearer " + t
    if b is not None:
        h["Content-Type"] = "application/json"
    q = urllib.request.Request(BASE + p, method=m, headers=h,
                               data=json.dumps(b).encode() if b is not None else None)
    try:
        r = urllib.request.urlopen(q, timeout=25, context=CTX)
        raw = r.read().decode()
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, raw[:300]
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:300]
    except Exception as e:
        return -1, f"{type(e).__name__}: {str(e)[:150]}"


def main():
    ts = int(time.time())
    em = f"unkey.qa.{ts}@tradethrone.test"
    cp, cd = rq("POST", "/api/auth/register",
                {"email": em, "password": f"UnkeyQA-{ts}", "full_name": "Unkey QA"})
    print("REG", cp, json.dumps(cd)[:200] if isinstance(cd, dict) else cd[:200])
    tok = cd.get("access_token") if isinstance(cd, dict) and cp in (200, 201) else None
    if not tok:
        print("NO TOKEN — abort"); sys.exit(1)

    s1, b1 = rq("POST", "/api/trades/order",
                {"symbol": "RELIANCE", "side": "BUY", "quantity": 1,
                 "order_type": "MARKET", "mode": "PAPER"}, tok)
    print("\nUNKEYED ORDER (legacy path):", s1, json.dumps(b1)[:600])

    s2, b2 = rq("POST", "/api/trades/order",
                {"symbol": "INFY", "side": "BUY", "quantity": 1,
                 "order_type": "MARKET", "mode": "PAPER",
                 "client_order_id": f"DISCMIN-{str(ts)[-6:]}"}, tok)
    print("\nKEYED ORDER:", s2, json.dumps(b2)[:600])

    s2b, b2b = rq("POST", "/api/trades/order",
                  {"symbol": "INFY", "side": "BUY", "quantity": 1,
                   "order_type": "MARKET", "mode": "PAPER",
                   "client_order_id": f"DISCMIN-{str(ts)[-6:]}"}, tok)
    print("\nKEYED REPLAY:", s2b, json.dumps(b2b)[:600])

    s3, b3 = rq("POST", "/api/v1/orders/execute-dma",
                {"symbol": "NIFTY", "side": "BUY", "lots": 1, "product": "MIS",
                 "order_type": "MARKET", "mode": "PAPER",
                 "client_order_id": f"DMAQA-{str(ts)[-6:]}"}, tok)
    print("\nDMA PAPER:", s3, json.dumps(b3)[:600])

    s4, b4 = rq("GET", "/api/trades/positions", tok)
    print("\nPOSITIONS:", s4, json.dumps(b4)[:800])

    s5, b5 = rq("GET", "/api/trades", tok)
    print("\nTRADES:", s5, json.dumps(b5)[:800])

    s6, b6 = rq("GET", "/api/brokers/balance", tok)
    print("\nBALANCE:", s6, json.dumps(b6)[:400])


if __name__ == "__main__":
    main()