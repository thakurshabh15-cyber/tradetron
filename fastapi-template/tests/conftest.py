"""Pytest configuration for webhook integration tests."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock


# Configure asyncio mode
pytest_asyncio.plugin.pytest_configure = lambda config: None


# Set testing environment for all tests
@pytest.fixture(autouse=True, scope="session")
def set_testing_environment():
    """Set environment to testing for all tests to enable mock mode."""
    from app.config import settings
    settings.environment = "testing"
    yield
    # Reset to production after tests
    settings.environment = "production"


@pytest.fixture(autouse=True, scope="function")
def reset_broker_mode():
    """Ensure no test leaks LIVE broker mode into others.

    Tests that intentionally exercise the LIVE broker-dispatch path must set
    ``settings.broker_mode = "live"`` themselves; this fixture guarantees the
    safe ``simulated`` default before every test.
    """
    from app.config import settings

    settings.broker_mode = "simulated"
    yield
@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# Mock Redis for tests that don't need real Redis
@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """Mock Redis connections for unit tests."""
    import redis.asyncio as redis
    
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.xadd = AsyncMock(return_value="12345-0")
    mock_redis.xreadgroup = AsyncMock(return_value=[])
    mock_redis.xack = AsyncMock(return_value=True)
    mock_redis.hincrby = AsyncMock(return_value=1)
    # Lua script returns [status_code, data] where:
    # status_code=1: new key, proceed
    # status_code=0: completed, data=JSON string of existing record
    # status_code=-1: currently processing
    mock_redis.register_script = MagicMock(return_value=AsyncMock(return_value=[1, None]))
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.close = AsyncMock()
    
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: mock_redis)
    
    return mock_redis


# Mock database session
@pytest.fixture
def mock_db_session():
    """Mock database session."""
    from unittest.mock import AsyncMock, MagicMock
    from sqlalchemy.ext.asyncio import AsyncSession
    
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    session.scalar_one_or_none = AsyncMock(return_value=None)
    
    return session


# Mock WebSocket manager
@pytest.fixture
def mock_ws_manager(monkeypatch):
    """Mock WebSocket manager."""
    from unittest.mock import AsyncMock
    
    mock_manager = AsyncMock()
    mock_manager.broadcast = AsyncMock()
    
    monkeypatch.setattr("app.market_data.manager.ws_manager", mock_manager)
    
    return mock_manager


# Mock audit logger
@pytest.fixture
def mock_audit_log(monkeypatch):
    """Mock audit log function."""
    from unittest.mock import AsyncMock
    
    mock_log = AsyncMock()
    
    monkeypatch.setattr("app.core.audit.log_audit_event", mock_log)
    
    return mock_log


# Mock subscription engine to bypass billing checks (INSTITUTIONAL plan with unlimited access)
@pytest.fixture(autouse=True)
def mock_subscription_engine(monkeypatch):
    """Mock subscription engine to return INSTITUTIONAL plan for all tests.
    This allows tests to run without hitting 402 Payment Required errors.
    """
    from app.engine.subscription import PLAN_LIMITS
    
    # Create a mock engine that always returns INSTITUTIONAL plan (unlimited everything)
    class MockSubscriptionEngine:
        async def get_user_plan(self, user_id, db):
            return "INSTITUTIONAL", PLAN_LIMITS["INSTITUTIONAL"]
        
        async def verify_access(self, user_id, feature, db, current_count=0):
            return {"plan_name": "INSTITUTIONAL", "limits": PLAN_LIMITS["INSTITUTIONAL"]}
        
        async def verify_feature_access(self, db, user_id, feature, current_count=None):
            return {"plan_name": "INSTITUTIONAL", "limits": PLAN_LIMITS["INSTITUTIONAL"]}
        
        async def get_entitlements(self, user_id, db):
            limits = PLAN_LIMITS["INSTITUTIONAL"]
            return {
                "plan_name": "INSTITUTIONAL",
                "display_name": limits["display_name"],
                "max_brokers": limits["max_brokers"],
                "max_algos": limits["max_algos"],
                "copy_trading_allowed": limits["copy_trading_allowed"],
                "tick_speed": limits.get("tick_speed", "1s"),
                "historical_candles": limits.get("historical_candles", "15m"),
                "priority_support": limits.get("priority_support", False),
                "vip_vps": limits.get("vip_vps", False),
                "api_access": limits.get("api_access", False),
            }
    
    mock_engine = MockSubscriptionEngine()
    # Patch the singleton instance in the subscription engine module
    # This works because API modules import it locally from app.engine.subscription
    monkeypatch.setattr("app.engine.subscription.subscription_engine", mock_engine)
    
    return mock_engine