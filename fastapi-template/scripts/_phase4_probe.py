"""Phase 4 staging connectivity probe (PostgreSQL + Redis)."""
import asyncio
import sys


async def main() -> int:
    print("=== PostgreSQL probe ===")
    try:
        import asyncpg
        c = await asyncpg.connect(
            "postgresql://tradetron:tradetron_staging_password@localhost:5433/tradetron_staging",
            timeout=5,
        )
        v = await c.fetchval("SELECT 1")
        print("PG SELECT 1 ->", v)
        db = await c.fetchval("SELECT current_database()")
        user = await c.fetchval("SELECT current_user")
        print("PG database:", db, "| user:", user)
        # Confirm this is the staging DB, not production
        appname = await c.fetchval("SHOW application_name")
        print("application_name:", appname)
        await c.close()
    except Exception as e:  # noqa: BLE001
        print("PG ERROR:", repr(e))
        return 1

    print("=== Redis probe ===")
    try:
        import redis
        r = redis.from_url(
            "redis://localhost:6379/0",
            socket_connect_timeout=5,
            socket_timeout=5,
            decode_responses=True,
        )
        print("REDIS PING ->", r.ping())
        r.set("staging_probe", "ok")
        print("REDIS GET staging_probe ->", r.get("staging_probe"))
        info = r.info("server")
        print("REDIS version:", info.get("redis_version"))
        r.close()
    except Exception as e:  # noqa: BLE001
        print("REDIS ERROR:", repr(e))
        return 1

    print("=== ALL PROBES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
