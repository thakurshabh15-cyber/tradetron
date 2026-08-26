# TradeThrone — High-Frequency Algorithmic Trading Platform MVP

A modular, production-ready algorithmic trading platform featuring asynchronous order routing, real-time WebSocket market feeds, multi-threaded strategy evaluation, and pre-trade risk management.

---

## 🏛️ System Architecture

```
                                  ┌───────────────────────────┐
                                  │      React Dashboard      │
                                  │ (Vite + Tailwind CSS v3)  │
                                  └─────────────▲─────────────┘
                                                │ REST / WebSocket
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 FastAPI Core Server                                    │
│                                                                                        │
│  ┌─────────────────────────┐   ┌──────────────────────────┐   ┌─────────────────────┐  │
│  │   Market Data Simulator │   │   WebSocket Broadcast    │   │  API Route Handlers │  │
│  │  (Geometric Brownian)   ├──►│    ConnectionManager     │◄──┤  (CRUD, Logs, Risk) │  │
│  └───────────┬─────────────┘   └──────────────────────────┘   └─────────────────────┘  │
│              │ asyncio.Queue                                                           │
│              ▼                                                                         │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │                                 Trading Engine                                   │  │
│  │                                                                                  │  │
│  │  ┌────────────────────────────┐              ┌────────────────────────────────┐  │  │
│  │  │      StrategyExecutor      │              │        StrategyEvaluator       │  │  │
│  │  │   SMA Crossover (50/200)   │              │   Dynamic User Rules (RSI/EMA) │  │  │
│  │  │   (collections.deque)      │              │       (ThreadPoolExecutor)     │  │  │
│  │  └─────────────┬──────────────┘              └───────────────┬────────────────┘  │  │
│  │                └──────────────────────┬──────────────────────┘                   │  │
│  │                                       ▼                                          │  │
│  │                        ┌─────────────────────────────┐                           │  │
│  │                        │     Risk Management Gate    │                           │  │
│  │                        │ (Max Pos, Drawdown, Rate)   │                           │  │
│  │                        └──────────────┬──────────────┘                           │  │
│  │                                       ▼                                          │  │
│  │                        ┌─────────────────────────────┐                           │  │
│  │                        │       Broker Adapter        │                           │  │
│  │                        │   (Simulated / Angel One)   │                           │  │
│  │                        └──────────────┬──────────────┘                           │  │
│  └───────────────────────────────────────┼──────────────────────────────────────────┘  │
│                                          ▼                                             │
│                           ┌─────────────────────────────┐                              │
│                           │    SQLite / aiosqlite DB    │                              │
│                           │   (Orders, Trades, Rules)   │                              │
│                           └─────────────────────────────┘                              │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 📁 Repository Structure

```
fastapi-template/
├── .env.example                    # Template with placeholder configuration
├── .env                            # Active environment variables (gitignored)
├── pyproject.toml                  # Project metadata & Python dependencies
├── requirements.txt                # Pinned production dependencies
├── README.md                       # Documentation & run instructions
│
├── app/                            # Backend package
│   ├── __init__.py
│   ├── main.py                     # FastAPI application factory & lifespan
│   ├── config.py                   # Pydantic Settings env management
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   └── logging.py              # Structured logging configuration
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   └── session.py              # Async SQLite engine & SessionLocal factory
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   └── trading.py              # SQLAlchemy ORM: OrderRecord, TradeRecord, StrategyRecord
│   │
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── trading.py              # Pydantic data contracts (Side, Tick, Strategy, Trade)
│   │
│   ├── brokers/
│   │   ├── __init__.py
│   │   ├── base.py                 # Abstract BrokerClient interface
│   │   ├── simulated.py            # Zero-dependency Paper Trading broker
│   │   └── angelone.py             # Production Angel One SmartAPI adapter
│   │
│   ├── market_data/
│   │   ├── __init__.py
│   │   ├── manager.py              # Multi-channel WebSocket ConnectionManager
│   │   └── simulator.py            # Geometric Brownian Motion price tick generator
│   │
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── trading_engine.py       # StrategyExecutor (SMA 50/200) & TradingEngine
│   │   ├── strategy_evaluator.py   # Multi-indicator rule engine (RSI, SMA, EMA)
│   │   └── risk_manager.py         # Pre-trade circuit breaker & rate limiter
│   │
│   └── api/
│       ├── __init__.py
│       ├── strategies.py           # CRUD endpoints for automated strategies
│       ├── trades.py               # Paginated trade history & stats
│       ├── market_data.py          # REST snapshots for prices & risk status
│       └── websocket.py            # WebSocket endpoints (/ws/market/{sym}, /ws/trades)
│
├── client/                         # React Frontend
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js              # Dev proxy to backend port 8000
│   ├── tailwind.config.js          # Tailwind CSS v3 dark theme configuration
│   ├── postcss.config.js
│   └── src/
│       ├── main.jsx
│       ├── index.css               # Design tokens & glassmorphism utilities
│       ├── App.jsx                 # Routing layout (Dashboard, Strategies, History)
│       ├── hooks/
│       │   ├── useWebSocket.js     # Auto-reconnecting WebSocket hook
│       │   └── useApi.js           # REST API fetch hook with mutations
│       ├── components/
│       │   ├── Sidebar.jsx         # Navigation sidebar
│       │   ├── MarketTicker.jsx    # Real-time price card
│       │   ├── TradeLog.jsx        # Live scrolling execution stream
│       │   ├── StrategyBuilder.jsx # Visual rule builder interface
│       │   ├── StrategyList.jsx    # Strategy status & toggle list
│       │   ├── RiskGauge.jsx       # Risk usage & circuit breaker meter
│       │   └── StatusBadge.jsx     # Status badge pill
│       └── pages/
│           ├── Dashboard.jsx       # Real-time trading desk
│           ├── Strategies.jsx      # Strategy automation page
│           └── TradeHistory.jsx    # Audit logs and performance metrics
│
└── tests/
    └── test_strategy_executor.py   # Unit test for SMA 50/200 StrategyExecutor
```

---

## ⚡ Key Highlights of the Core Engine

### `StrategyExecutor` (SMA 50/200 Crossover)
- Built with `collections.deque(maxlen=300)` for bounded memory usage and $O(1)$ appends.
- Computes **Fast SMA (50)** and **Slow SMA (200)** over recent tick history.
- Automatically generates:
  - **`BUY` (Golden Cross)** when the 50-period SMA crosses above the 200-period SMA.
  - **`SELL` (Death Cross)** when the 50-period SMA crosses below the 200-period SMA.
- Prevents false repeated signals by maintaining a state transition machine (`above` $\leftrightarrow$ `below`).

### Multi-Threaded Execution
- CPU-intensive technical analysis and user rule evaluations are offloaded to Python's `ThreadPoolExecutor`, preventing blocking of the async `uvicorn` event loop.

### Pre-Trade Risk Management (`RiskManager`)
- **Position Sizing Gate**: Blocks orders exceeding maximum exposure per asset.
- **Daily Loss Circuit Breaker**: Halts all automated execution if cumulative daily loss exceeds threshold.
- **Order Rate Limiter**: Prevents flooding the broker with runaway execution loops.

---

## 🚀 Running the Full Stack

### 1. Backend Setup & Run

In your backend terminal:

```bash
cd fastapi-template

# Install dependencies
pip install -r requirements.txt

# Run the FastAPI backend server
python -m uvicorn app.main:app --reload --port 8080
```

The backend starts at:
- **API & Docs**: `http://localhost:8080/docs`
- **Health Check**: `http://localhost:8080/api/health`
- **WebSocket Feeds**: `ws://localhost:8080/ws/market/{SYMBOL}` and `ws://localhost:8080/ws/trades`

### 2. Frontend Setup & Run

In a second terminal:

```bash
cd fastapi-template/client

# Start Vite development server
npm run dev
```

The frontend dashboard will be available at `http://localhost:5173`.
