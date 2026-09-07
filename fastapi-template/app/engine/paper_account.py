"""Shared durable PAPER accounting primitives (P1-1).

Every close path that books realized PAPER P&L into a user's balance goes
through ``credit_paper_pnl`` so the credit is:

  * OWNER-SCOPED — the P&L of a position always lands in the balance of the
    user that OWNS the position (``owner_user_id == position.user_id``),
    never the authenticated caller.  An admin/operator close of another
    user's position must not credit the operator's own account (cross-user
    accounting corruption).
  * ONCE-PER-CLOSE — callers invoke this only after the atomic OPEN->CLOSED
    CAS transition won, so a replay/concurrent close can never double-credit.
  * FAIL-SAFE — a missing owner or missing user row means "no credit", never
    a fabricated balance mutation.

The invariant ``paper_balance == 1_000_000 + sum(realized_pnl)`` (a P&L
tracker) is preserved: entry never debits, credits round to 2 decimals.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.user import UserRecord

logger = get_logger("engine.paper_account")

PAPER_STARTING_BALANCE = 1_000_000.0


async def credit_paper_pnl(
    db: AsyncSession,
    owner_user_id: str | None,
    amount: float,
) -> float | None:
    """Credit realized P&L to the POSITION OWNER's paper balance.

    Args:
        db: The caller's active session (same transaction as the close CAS).
        owner_user_id: ``PositionRecord.user_id`` — the balance that MUST be
            credited.  Never pass the authenticated caller's id unless the
            caller IS the owner.
        amount: Realized P&L for this close (signed).

    Returns:
        The new ``paper_balance``, or ``None`` when there is no owner to
        credit (fail-safe: no owner => no mutation).

    Idempotency contract: this helper performs a plain user update and MUST
    only be called after the atomic OPEN->CLOSED position CAS won in the same
    transaction; the position CAS is what guarantees exactly-once booking.
    """
    if not owner_user_id:
        logger.warning(
            "Paper credit skipped: no position owner for realized P&L %s",
            amount,
        )
        return None

    user = await db.get(UserRecord, owner_user_id)
    if user is None:
        logger.warning(
            "Paper credit skipped: owner %s not found for realized P&L %s",
            owner_user_id,
            amount,
        )
        return None

    # Legacy-identical base: use the stored balance AS-IS (0.0 is a valid
    # P&L-tracker state — never rebase it), and only fall back to the paper
    # starting balance when the value is NULL/absent (a fresh or missing
    # column).  `0.0 or 1_000_000` would fabricate +1M of unearned credit.
    raw_balance = getattr(user, "paper_balance", None)
    current = float(raw_balance) if raw_balance is not None else PAPER_STARTING_BALANCE
    new_balance = round(current + float(amount), 2)
    user.paper_balance = new_balance
    db.add(user)
    logger.info(
        "Paper account credited: owner=%s realized=%+.2f new_balance=%.2f",
        owner_user_id,
        amount,
        new_balance,
    )
    return new_balance