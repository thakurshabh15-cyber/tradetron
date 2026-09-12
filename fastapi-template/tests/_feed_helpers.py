"""Shared helpers for tests that exercise the LIVE strategy execution path.

The Phase 15A realtime feed gate (`TradingEngine._feed_gate_for_live`) blocks
LIVE execution unless the symbol has a fresh, genuinely-live quote in the
unified manager cache.  Tests that intend to exercise gates *after* the feed
gate (margin, token expiry, broker-mode, durable claims, idempotency) must seed
a fresh live quote first so the feed gate passes.
"""

from datetime import datetime, timezone

from app.market_data.base import AssetClass, DataFeedMode, NormalizedTick


def seed_live_quote(symbol: str = "RELIANCE", price: float = 2500.0) -> None:
    """Place a fresh, non-stale, genuine vendor quote for ``symbol`` in the
    unified manager cache so the LIVE feed gate passes."""
    from app.market_data.unified_manager import unified_market_manager

    unified_market_manager._quotes[symbol] = NormalizedTick(
        symbol=symbol,
        price=price,
        bid=price - 0.05,
        ask=price + 0.05,
        open=price,
        high=round(price * 1.01, 2),
        low=round(price * 0.99, 2),
        close=price,
        change=0.0,
        change_pct=0.0,
        volume=1000,
        asset_class=AssetClass.EQUITY,
        feed_mode=DataFeedMode.LIVE_BROKER_VENDOR,
        data_source="NSE (Live Broker Feed)",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


async def seed_live_broker_state(
    broker_account_id: str,
    user_id: str | None = None,
    *,
    status: str = "LIVE",
    source: str = "BROKER",
    positions: list | None = None,
    available_cash: float | None = 500000.0,
    utilized_margin: float | None = 0.0,
    total_collateral: float | None = None,
    total_equity: float | None = None,
    unrealized_pnl: float | None = None,
    realized_pnl: float | None = None,
    currency: str | None = "INR",
    captured_at: datetime | None = None,
    stale: bool = False,
) -> object:
    """Seed a persisted broker-truth snapshot (Phase 15B helper).

    Exactly like ``seed_live_quote`` lets tests pass the Phase 15A feed gate,
    this lets tests pass the Phase 15B broker-state gate: it writes a fresh
    (or deliberately stale/error) ``BrokerStateRecord`` for a broker account so
    the LIVE risk gate under test sees deterministic broker truth.

    ``stale=True`` back-dates ``captured_at`` beyond the configured threshold
    so the snapshot derives STALE instead of LIVE.
    """
    import json
    from sqlalchemy import select

    from app.config import settings
    from app.db.session import SessionLocal
    from app.models.broker_state import BrokerStateRecord

    positions = positions or []
    captured = captured_at or datetime.now(timezone.utc)
    if stale:
        from datetime import timedelta
        captured = captured - timedelta(seconds=settings.broker_state_stale_after + 60)

    positions_json = json.dumps(positions, sort_keys=True, separators=(",", ":"))

    async with SessionLocal() as db:
        stmt = select(BrokerStateRecord).where(
            BrokerStateRecord.broker_account_id == broker_account_id
        )
        snap = (await db.execute(stmt)).scalar_one_or_none()
        if snap is None:
            snap = BrokerStateRecord(
                broker_account_id=broker_account_id,
                user_id=user_id,
                status=status,
                source=source,
                positions_json=positions_json,
                available_cash=available_cash,
                utilized_margin=utilized_margin,
                total_collateral=total_collateral,
                total_equity=total_equity,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
                currency=currency,
                captured_at=captured,
                last_good_captured_at=captured if status == "LIVE" and not stale else None,
                sync_count=1,
            )
            db.add(snap)
        else:
            snap.status = status
            snap.source = source
            snap.user_id = user_id
            snap.positions_json = positions_json
            snap.available_cash = available_cash
            snap.utilized_margin = utilized_margin
            snap.total_collateral = total_collateral
            snap.total_equity = total_equity
            snap.unrealized_pnl = unrealized_pnl
            snap.realized_pnl = realized_pnl
            snap.currency = currency
            snap.captured_at = captured
            if status == "LIVE" and not stale:
                snap.last_good_captured_at = captured
            snap.sync_count = (snap.sync_count or 0) + 1
            db.add(snap)
        await db.commit()
        return snap