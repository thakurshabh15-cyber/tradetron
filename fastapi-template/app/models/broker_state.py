"""Broker-truth state ORM model (Phase 15B).

Every authenticated LIVE broker account gets exactly ONE durable, freshness-
tracked snapshot row.  The row stores the NORMALIZED view of what the broker
actually reported (positions + margins), never a fabrication:

  source        : "BROKER" (genuine adapter fetch) | "SIMULATED" | "UNAVAILABLE"
  status        : stored raw capture state, then DERIVED at read time to the
                  live semantic status:
                    LIVE        – genuine broker data, captured within threshold
                    STALE       – genuine broker data, older than threshold
                    UNAVAILABLE – no snapshot yet / account inaccessible
                    ERROR       – last sync attempt raised / returned no data
                    PAPER       – simulated broker account (never LIVE truth)

The row is account-scoped (``broker_account_id``) and carries ``user_id`` so
every reconciliation operation can be enforced against the account OWNER —
tenant/account A can never affect user/account B.  ``positions_json`` stores
the normalized canonical positions and ``positions_hash`` a fingerprint so
idempotent re-syncs can detect a no-change pass.

Rule (NO FABRICATION): fields the broker does not provide are stored as NULL
(``None``) and surfaced to the caller/API as unavailable – never synthesised
from paper balances or previous sessions.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class BrokerStateRecord(Base):
    """Persisted, freshness-tracked, account-scoped snapshot of broker truth."""

    __tablename__ = "broker_state"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id
    )
    broker_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    # Stored capture state: LIVE | ERROR | UNAVAILABLE | PAPER
    status: Mapped[str] = mapped_column(
        String(20), default="UNAVAILABLE", server_default="UNAVAILABLE"
    )
    # BROKER | SIMULATED | UNAVAILABLE
    source: Mapped[str] = mapped_column(
        String(20), default="BROKER", server_default="BROKER"
    )

    # Normalized broker positions (canonical contract) + fingerprint
    positions_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )
    positions_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Normalized broker margins / accounting (NULL = broker did not provide)
    available_cash: Mapped[float | None] = mapped_column(Float, nullable=True)
    utilized_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_collateral: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(12), nullable=True)

    # Freshness metadata
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_good_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("broker_account_id", name="uq_broker_state_broker_account"),
        Index("ix_broker_state_user_status", "user_id", "status"),
    )