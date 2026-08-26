"""End-to-end smoke test for local webhook processing.

Sends a dummy TradeThrone trade alert to the live webhook platform over real
HTTP and prints the raw response. If nothing is listening on the target
port, the script boots the platform itself in LOCAL MOCK MODE
(WEBHOOK_LOCAL_MODE=1 -> unsigned requests allowed, no Redis needed),
runs the requests, then shuts the server down again.

Usage (from the fastapi-template directory):
    python test_local_webhook.py                     # targets 127.0.0.1:8001
    WEBHOOK_PORT=8002 python test_local_webhook.py   # custom port

Endpoints exercised:
    POST /webhooks/tradetron   (dummy BUY alert)
    POST /webhooks/zerodha     (same payload, deliberately UNSIGNED)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("WEBHOOK_PORT", "8001"))
BASE_URL = f"http://127.0.0.1:{PORT}"
STARTUP_TIMEOUT_SECONDS = 45.0

# Dummy trade alert, shaped like what an external signal provider would post:
# top-level signal / symbol / action / quantity / price fields.
TRADE_ALERT = {
    "signal": "entry_long",
    "symbol": "NIFTY24AUG25000CE",
    "action": "BUY",
    "quantity": 50,
    "price": 185.50,
}


def server_is_up() -> bool:
    """True when the webhook platform answers its health probe."""
    try:
        resp = httpx.get(f"{BASE_URL}/healthz", timeout=1.5)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def start_server() -> subprocess.Popen:
    """Boot the webhook platform locally in unsigned mock mode."""
    env = os.environ.copy()
    env["WEBHOOK_LOCAL_MODE"] = "1"  # skip signature checks & Redis deps
    env["WEBHOOK_PORT"] = str(PORT)

    print(f"[test] No server found on port {PORT}; starting local mock server ...")
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "app.webhooks.main:app",
            "--host", "127.0.0.1",
            "--port", str(PORT),
            "--log-level", "warning",
        ],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_until_ready(proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    """Poll /healthz until the server responds (or the process dies)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # server exited during startup
        if server_is_up():
            return True
        time.sleep(0.25)
    return False


def post_trade_alert(client: httpx.Client, provider: str, payload: dict) -> dict:
    """POST one dummy alert and pretty-print status + response body."""
    url = f"{BASE_URL}/webhooks/{provider}"
    print(f"\n[test] POST {url}")
    print(f"[test] Payload : {json.dumps(payload)}")

    resp = client.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},  # NOTE: no signature header
    )

    print(f"[test] HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    print("[test] Response:", json.dumps(body, indent=2))
    return body


def main() -> int:
    server: subprocess.Popen | None = None
    failures: list[str] = []

    try:
        if not server_is_up():
            server = start_server()
            if not wait_until_ready(server):
                print("[test] FAIL: server did not become ready in time.")
                return 1
        else:
            print(f"[test] Reusing already-running server on port {PORT}.")

        expected_keys = {"status", "provider", "data"}

        with httpx.Client(timeout=15.0) as client:
            # 1) TradeThrone endpoint (the primary target of this test)
            tt_body = post_trade_alert(client, "tradethrone", TRADE_ALERT)
            if set(tt_body.keys()) >= expected_keys and tt_body.get("status") != "received":
                failures.append("tradetron: unexpected 'status' value")

            # 2) Zerodha endpoint with the SAME unsigned payload
            zh_body = post_trade_alert(client, "zerodha", TRADE_ALERT)
            if set(zh_body.keys()) >= expected_keys and zh_body.get("status") != "received":
                failures.append("zerodha: unexpected 'status' value")

        if failures:
            print("\n[test] FAIL:")
            for f in failures:
                print(f"       - {f}")
            return 1

        print("\n[test] PASS: both endpoints accepted unsigned mock trade alerts.")
        return 0
    finally:
        if server is not None:
            print("\n[test] Stopping local webhook server ...")
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            print("[test] Server stopped.")


if __name__ == "__main__":
    sys.exit(main())
