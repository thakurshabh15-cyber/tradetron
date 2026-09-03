"""Phase 2 — Redis multi-worker validation.

Spawns several independent worker subprocesses, each with its own private
in-memory state, all sharing ONE Redis instance.  Verifies that rate-limiter
and OTP state are globally consistent across workers (proving the Redis path
is what actually protects multi-worker deployments).

Each worker subprocess:
  - checks the sliding-window rate limit for a SHARED key
  - generates an OTP, then a second worker verifies it (cross-process single-use)
  - checks OTP single-use and cross-user isolation

Returns 0 only if every invariant holds.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time

# Ensure project root is on sys.path for spawned subprocesses
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _worker_rate_limit(shared_key: str, max_requests: int, results: dict):
    """Each worker separately hits the shared rate-limit key in Redis.

    Reports how many of its own calls were allowed (0..max_requests) and
    whether the final (limit+1)th call was blocked.
    """
    from app.core import security as sec

    allowed_count = 0
    for _ in range(max_requests):
        if sec.check_rate_limit(shared_key, max_requests, 60):
            allowed_count += 1
    # The (max_requests+1)th call from THIS worker must be blocked
    blocked = sec.check_rate_limit(shared_key, max_requests, 60)
    results[f"w{os.getpid()}_allowed_count"] = allowed_count
    results[f"w{os.getpid()}_blocked_at_limit"] = (blocked is False)


def _worker_otp(identifier: str):
    """Generate an OTP in this worker process and write it to a file for a peer."""
    from app.core import security as sec

    code = sec.generate_otp_for_identifier(identifier)
    with open(f"_otp_{identifier}.txt", "w") as f:
        f.write(code)
    return code


def _worker_otp_verify(identifier: str, expected_code: str, results: dict, tag: str):
    from app.core import security as sec

    results[f"{tag}_first_ok"] = sec.verify_otp_for_identifier(identifier, expected_code) is True
    # second use must FAIL (single-use)
    results[f"{tag}_second_blocked"] = sec.verify_otp_for_identifier(identifier, expected_code) is False


def _worker_otp_wrong(identifier: str, results: dict, tag: str):
    from app.core import security as sec

    # Verifying with an empty/wrong code must fail
    results[f"{tag}_wrong_rejected"] = sec.verify_otp_for_identifier(identifier, "000000") is False


def run_rate_limit_test() -> dict[str, bool]:
    """Three worker processes hammer one shared key; aggregate must respect the limit."""
    mgr = mp.Manager()
    results = mgr.dict()
    shared_key = f"shared-key-{int(time.time())}"
    max_requests = 3
    procs = [mp.Process(target=_worker_rate_limit, args=(shared_key, max_requests, results)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    out = dict(results)
    total_allowed = sum(v for k, v in out.items() if k.endswith("_allowed_count") and isinstance(v, int))
    total_blocked = sum(1 for k, v in out.items() if k.endswith("_blocked_at_limit") and v)
    num_workers = sum(1 for k in out if k.endswith("_allowed_count"))
    out["_total_allowed_calls"] = total_allowed
    out["_total_workers_blocked_at_limit"] = total_blocked
    out["_num_workers"] = num_workers
    out["_max_requests"] = max_requests
    return out


def run_otp_test() -> dict[str, bool]:
    """OTP created in worker A, verified+consumed in worker B, single-use respected."""
    mgr = mp.Manager()
    results = mgr.dict()
    ident_a = f"user-a-{int(time.time())}"
    ident_b = f"user-b-{int(time.time())}"

    # Worker creates OTP for A
    p = mp.Process(target=_worker_otp, args=(ident_a,))
    p.start()
    p.join(timeout=30)
    with open(f"_otp_{ident_a}.txt") as f:
        code_a = f.read().strip()

    # Different worker verifies A's code, and attempts single-use + isolation
    p1 = mp.Process(target=_worker_otp_verify, args=(ident_a, code_a, results, "a"))
    p2 = mp.Process(target=_worker_otp_wrong, args=(ident_b, results, "b_isolation"))
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)

    out = dict(results)
    # Cross-user isolation: user B must NOT accept user A's code
    from app.core import security as sec
    out["_cross_user_rejected"] = sec.verify_otp_for_identifier(ident_b, code_a) is False
    out["_otp_not_present_after_consume"] = sec.verify_otp_for_identifier(ident_a, code_a) is False
    return out


def main() -> int:
    print("=" * 60)
    print("PHASE 2 — REDIS MULTI-WORKER VALIDATION")
    print("=" * 60)

    import redis as redis_mod
    r = redis_mod.from_url("redis://localhost:6379/0", socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
    ping = r.ping()
    print("Redis reachable:", ping)
    if not ping:
        print("FATAL: Redis not reachable — cannot validate multi-worker.")
        return 1

    print("\n--- Rate-limit across 3 workers (shared key) ---")
    rl = run_rate_limit_test()
    for k, v in sorted(rl.items()):
        print(f"  {k}: {v}")

    print("\n--- OTP creation/verify/single-use/isolation across workers ---")
    otp = run_otp_test()
    for k, v in sorted(otp.items()):
        print(f"  {k}: {v}")

    failures = []
    # CRITICAL invariant: total allowed calls across ALL workers must NOT exceed max_requests
    # This proves the global rate limit is enforced across process boundaries via Redis
    if rl.get("_total_allowed_calls", 0) > rl.get("_max_requests"):
        failures.append(
            f"global rate limit violated: {rl['_total_allowed_calls']} allowed "
            f"exceeds max {rl['_max_requests']}"
        )
    # Each worker must have seen the limit enforced at least once (they each try max+1 calls)
    if rl.get("_total_workers_blocked_at_limit", 0) != rl.get("_num_workers"):
        failures.append(
            f"not all workers saw the limit: {rl.get('_total_workers_blocked_at_limit')}/"
            f"{rl.get('_num_workers')} blocked"
        )

    if not otp.get("a_first_ok"):
        failures.append("OTP created in one worker not verifiable in another")
    if not otp.get("a_second_blocked"):
        failures.append("OTP was re-usable (single-use violated) across workers")
    if not otp.get("_cross_user_rejected"):
        failures.append("cross-user OTP isolation violated")
    if not otp.get("_otp_not_present_after_consume"):
        failures.append("OTP consumed but still present")

    if failures:
        print("\nRESULT: FAIL —", failures)
        return 1
    print("\nRESULT: PASS — multi-worker rate-limit & OTP state globally consistent")
    return 0


if __name__ == "__main__":
    # Must be spawn-safe on Windows
    mp.set_start_method("spawn")
    sys.exit(main())

