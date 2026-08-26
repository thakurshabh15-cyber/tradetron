# TradeThrone Algo Strategy Marketplace — System Audit

**Audit Date:** 2026-08-20  
**Repository Scope:** Backend (`fastapi-template/app`), Frontend (`fastapi-template/client`)

---

## 1. Feature Implementation Audit

| Feature | Status | File(s) | Notes |
| :--- | :--- | :--- | :--- |
| **Auth (JWT / OAuth / OTP)** | ✅ **Done** | Backend: [`app/core/security.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/core/security.py), [`app/api/auth.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/auth.py), [`app/models/user.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/models/user.py)<br>Frontend: [`client/src/services/oauth.js`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/services/oauth.js), [`client/src/components/AuthModal.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/AuthModal.jsx) | RFC 7519 JWT auth (15-min access token, 7-day refresh token), 6-digit OTP verification flow (`/api/auth/verify-otp`), Google/Apple OAuth service, and React modal with profile display. |
| **Dashboard Summary API** | ✅ **Done** | Backend: [`app/api/dashboard.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/dashboard.py), [`app/api/market_data.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/market_data.py)<br>Frontend: [`client/src/pages/Dashboard.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/pages/Dashboard.jsx), [`client/src/components/TopStrategiesCard.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/TopStrategiesCard.jsx), [`client/src/components/PendingTasksList.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/PendingTasksList.jsx) | `GET /api/dashboard/summary` includes `weekReturn`, `monthReturn`, `topStrategies`, `pendingTasks`, and `engineStatus`. `POST /api/dashboard/complete-task` handles isolated task completion state updates. |
| **Strategy Marketplace** | ✅ **Done** | Backend: [`app/api/strategies.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/strategies.py), [`app/models/marketplace.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/models/marketplace.py)<br>Frontend: [`client/src/pages/Marketplace.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/pages/Marketplace.jsx), [`client/src/components/DeploymentModal.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/DeploymentModal.jsx), [`client/src/components/StrategyWizardScreen.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/StrategyWizardScreen.jsx) | `GET /api/strategies/marketplace` (pagination, category filters, symbol-search, sort), `POST /api/strategies/{id}/deploy`, `POST /api/strategies/{id}/pause`, `POST /api/strategies/marketplace/publish`, and multi-step Wizard. |
| **Strategy CRUD** | ✅ **Done** | Backend: [`app/api/strategies.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/strategies.py), [`app/schemas/trading.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/schemas/trading.py)<br>Frontend: [`client/src/components/StrategyBuilder.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/StrategyBuilder.jsx), [`client/src/components/StrategyList.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/StrategyList.jsx) | Full CRUD with visual multi-condition rule builder (RSI, SMA, EMA, Price crossover conditions) and SQLite persistence. |
| **Deploy / Pause Strategy** | ✅ **Done** | Backend: [`app/api/strategies.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/strategies.py), [`app/engine/trading_engine.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/engine/trading_engine.py)<br>Frontend: [`client/src/components/StrategyList.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/StrategyList.jsx) | Dynamic enable/disable/pause toggle with instant synchronization to the running trading engine. |
| **Backtest Engine** | ✅ **Done** | Backend: [`app/engine/backtester.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/engine/backtester.py), [`app/api/backtest.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/backtest.py)<br>Frontend: [`client/src/pages/BacktestLab.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/pages/BacktestLab.jsx), [`client/src/components/EquityCurve.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/EquityCurve.jsx) | Truthful backtesting: deterministic seeded OHLC series, live-parity signals via `StrategyEvaluator`, SEBI lot-size enforcement (min-lot floor), exact statutory charges per leg (brokerage ₹20/order, STT, exchange txn, GST 18%, stamp duty, SEBI fee, slippage), equity curve, annualised Sharpe, drawdown, profit factor. Routes `POST /api/backtest/run` + legacy `/api/strategies/backtest`. Bonus: AI Quant Lab NL parser + Strategy Doctor robustness score (`/api/quant-lab/*`) and Auto-Pilot Risk Guard kill-switch (`/api/risk-guard/*`). |
| **Order Execution** | ✅ **Done** | Backend: [`app/engine/order_manager.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/engine/order_manager.py), [`app/brokers/simulated.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/brokers/simulated.py), [`app/brokers/angelone.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/brokers/angelone.py)<br>Frontend: [`client/src/components/TradeLog.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/TradeLog.jsx) | `OrderManager` with pre-trade risk validation, position tracking, stop-loss and take-profit auto-exits, and broker routing (Paper & Live). |
| **Trade History** | ✅ **Done** | Backend: [`app/api/trades.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/trades.py), [`app/models/trading.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/models/trading.py)<br>Frontend: [`client/src/pages/TradeHistory.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/pages/TradeHistory.jsx) | Paginated audit log with side, symbol, execution price, timestamp, P&L, and asset filtering. |
| **Market Data Feed** | ✅ **Done** | Backend: [`app/market_data/simulator.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/market_data/simulator.py), [`app/api/market_data.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/market_data.py)<br>Frontend: [`client/src/components/MarketTicker.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/MarketTicker.jsx) | Geometric Brownian Motion tick generator producing real-time quotes, 24h change %, and volumes for multiple assets. |
| **WebSocket** | ✅ **Done** | Backend: [`app/market_data/manager.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/market_data/manager.py), [`app/api/websocket.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/websocket.py)<br>Frontend: [`client/src/hooks/useWebSocket.js`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/hooks/useWebSocket.js) | Low-latency multi-channel broadcaster (`/ws/market/{sym}`, `/ws/trades`) with automatic exponential backoff reconnection. |
| **Watchlist + Alerts** | ⚠️ **Partial** | Backend: Simulator watches symbols in [`app/config.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/config.py)<br>Frontend: Tickers grid in [`client/src/pages/Dashboard.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/pages/Dashboard.jsx) | Default watchlist is rendered on the dashboard, but custom user watchlist CRUD and price alert notifications are missing. |
| **User Profile / Settings** | ⚠️ **Partial** | Backend: [`app/api/user.py`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/app/api/user.py)<br>Frontend: [`client/src/components/SetupChecklist.jsx`](file:///c:/Users/HP/Desktop/tradetron/fastapi-template/client/src/components/SetupChecklist.jsx) | Onboarding setup checklist is implemented, but full user profile (broker credentials manager, API keys, notification preferences) is missing. |
| **Reports / Export** | ❌ **Missing** | Backend: *Missing*<br>Frontend: *Missing* | CSV/PDF trade log export, tax PnL reports, and strategy performance analytics exports are missing. |

---

## 2. Database Schema Comparison

### Existing Database Tables (`app/models/trading.py`):
1. **`strategies`**:
   - `id` (VARCHAR(36), PK)
   - `name` (VARCHAR(100))
   - `symbols_json` (TEXT)
   - `conditions_json` (TEXT)
   - `action_json` (TEXT)
   - `enabled` (BOOLEAN)
   - `created_at` (DATETIME)
   - `updated_at` (DATETIME)
2. **`orders`**:
   - `id` (VARCHAR(36), PK)
   - `strategy_id` (VARCHAR(36), Nullable)
   - `broker_order_id` (VARCHAR(100), Nullable)
   - `symbol` (VARCHAR(30))
   - `side` (VARCHAR(10))
   - `quantity` (INTEGER)
   - `order_type` (VARCHAR(10))
   - `status` (VARCHAR(20))
   - `created_at` (DATETIME)
3. **`trades`**:
   - `id` (VARCHAR(36), PK)
   - `order_id` (VARCHAR(36), Nullable)
   - `strategy_id` (VARCHAR(36), Nullable)
   - `strategy_name` (VARCHAR(100), Nullable)
   - `symbol` (VARCHAR(30))
   - `side` (VARCHAR(10))
   - `quantity` (INTEGER)
   - `price` (FLOAT)
   - `pnl` (FLOAT, Nullable)
   - `executed_at` (DATETIME)

---

### Spec Schema Comparison & Missing Tables/Columns

> **Non-Breaking Schema Rule:** All existing tables (`strategies`, `orders`, `trades`) will remain untouched. The following new tables and non-destructive optional columns will be added to support the full marketplace spec:

#### Missing Tables to Add:
1. **`users`** *(For Auth & Profiles)*:
   - `id` (VARCHAR(36), PK)
   - `email` (VARCHAR(255), UNIQUE, Indexed)
   - `hashed_password` (VARCHAR(255))
   - `full_name` (VARCHAR(100))
   - `role` (VARCHAR(20), Default: "trader")
   - `is_active` (BOOLEAN, Default: True)
   - `totp_secret` (VARCHAR(100), Nullable)
   - `created_at` (DATETIME)

2. **`marketplace_strategies`** *(For Strategy Marketplace)*:
   - `id` (VARCHAR(36), PK)
   - `creator_name` (VARCHAR(100))
   - `name` (VARCHAR(150))
   - `description` (TEXT)
   - `category` (VARCHAR(50))  *(e.g., Trend Following, Mean Reversion, Options)*
   - `pricing_type` (VARCHAR(20))  *(FREE, MONTHLY, REVENUE_SHARE)*
   - `price` (FLOAT, Default: 0.0)
   - `min_capital` (FLOAT)
   - `win_rate` (FLOAT)
   - `total_return_pct` (FLOAT)
   - `max_drawdown_pct` (FLOAT)
   - `subscribers_count` (INTEGER, Default: 0)
   - `rating` (FLOAT, Default: 5.0)
   - `strategy_config_json` (TEXT)
   - `is_published` (BOOLEAN, Default: True)
   - `created_at` (DATETIME)

3. **`strategy_deployments`** *(For Marketplace Subscriptions & Execution)*:
   - `id` (VARCHAR(36), PK)
   - `marketplace_strategy_id` (VARCHAR(36), FK / Ref)
   - `strategy_name` (VARCHAR(150))
   - `execution_mode` (VARCHAR(20))  *(PAPER, LIVE)*
   - `broker_name` (VARCHAR(50))  *(Simulated, Angel One)*
   - `multiplier` (FLOAT, Default: 1.0)
   - `capital_allocated` (FLOAT)
   - `status` (VARCHAR(20))  *(RUNNING, PAUSED, STOPPED)*
   - `realized_pnl` (FLOAT, Default: 0.0)
   - `deployed_at` (DATETIME)

4. **`backtests`** *(For Backtest Engine)*:
   - `id` (VARCHAR(36), PK)
   - `strategy_name` (VARCHAR(100))
   - `symbol` (VARCHAR(30))
   - `timeframe` (VARCHAR(10))
   - `start_date` (DATETIME)
   - `end_date` (DATETIME)
   - `initial_capital` (FLOAT)
   - `final_capital` (FLOAT)
   - `total_trades` (INTEGER)
   - `win_rate` (FLOAT)
   - `sharpe_ratio` (FLOAT)
   - `max_drawdown_pct` (FLOAT)
   - `equity_curve_json` (TEXT)
   - `created_at` (DATETIME)

5. **`watchlists` & `alerts`** *(For Custom Watchlists & Price Alerts)*:
   - `id` (VARCHAR(36), PK)
   - `symbol` (VARCHAR(30))
   - `condition` (VARCHAR(20))  *(CROSS_ABOVE, CROSS_BELOW)*
   - `target_price` (FLOAT)
   - `is_triggered` (BOOLEAN, Default: False)
   - `created_at` (DATETIME)
