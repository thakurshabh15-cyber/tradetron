#!/usr/bin/env python
"""Database initialization script for Tradetron Webhook Platform.

This script creates all database tables including the trade_audit_logs table.
Run this script directly to initialize the database schema.
"""

import asyncio
import sys
from pathlib import Path

# Ensure project root is in path
ROOT_DIR = Path(__file__).parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db.session import engine, Base
# Import all models to register them with SQLAlchemy metadata
from app.models.user import UserRecord, RevokedTokenRecord  # noqa: F401
from app.models.broker_account import BrokerAccountRecord, BrokerSessionLogRecord  # noqa: F401
from app.models.trading import (
    OrderRecord,
    PositionRecord,
    StrategyRecord,
    TradeRecord,
)  # noqa: F401
from app.models.visual_strategy import VisualStrategyRecord  # noqa: F401
from app.models.marketplace import (
    MarketplaceStrategyRecord,
    StrategyDeploymentRecord,
)  # noqa: F401
from app.models.watchlist import (
    WatchlistRecord,
    PriceAlertRecord,
)  # noqa: F401
from app.models.notification import (
    NotificationPreferenceRecord,
)  # noqa: F401
from app.models.alerts import UserNotificationSettings  # noqa: F401
from app.models.billing import (
    SubscriptionRecord,
    PaymentRecord,
    InvoiceRecord,
    PlanRecord,
)  # noqa: F401
from app.models.subscription import PlanRecord as SubscriptionPlanRecord  # noqa: F401
from app.models.subscription import SubscriptionRecord as UserSubscriptionRecord  # noqa: F401
from app.models.copy_trading import (
    CopyGroupRecord,
    CopyFollowerRecord,
)  # noqa: F401
from app.models.audit import AuditLogRecord, TradeAuditRecord  # noqa: F401


async def init() -> None:
    """Initialize database tables."""
    print("Initializing database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables verified/created successfully.")


if __name__ == "__main__":
    asyncio.run(init())