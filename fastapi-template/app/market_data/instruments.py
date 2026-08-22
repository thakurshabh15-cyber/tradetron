"""Comprehensive Instrument Master and Scrip Search Registry.

Supports all NSE/BSE Cash Equities, F&O Derivatives (Nifty, BankNifty, Stock Futures/Options),
MCX Commodities (Gold, Silver, Crude Oil, Natural Gas), Forex, and Crypto.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional
import httpx
from app.core.logging import get_logger

logger = get_logger("market.instruments")


@dataclass
class InstrumentItem:
    symbol: str
    name: str
    exchange: str  # NSE, BSE, NFO, MCX, BINANCE, CDS
    segment: str   # EQUITY, FNO, COMMODITY, CRYPTO, FOREX
    instrument_type: str  # EQ, FUT, CE, PE, COM, CRYPTO
    lot_size: int = 1
    tick_size: float = 0.05
    strike: Optional[float] = None
    expiry: Optional[str] = None
    base_price: float = 1000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "segment": self.segment,
            "instrument_type": self.instrument_type,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "strike": self.strike,
            "expiry": self.expiry,
            "base_price": self.base_price,
        }


# ── Comprehensive Multi-Asset Master Catalogue ────────────────────────────────

_INSTRUMENT_CATALOGUE: list[InstrumentItem] = [
    # ── Top NSE & BSE Equities ────────────────────────────────────────────────
    InstrumentItem("RELIANCE", "Reliance Industries Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=2985.40),
    InstrumentItem("TCS", "Tata Consultancy Services Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=3940.60),
    InstrumentItem("HDFCBANK", "HDFC Bank Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1685.10),
    InstrumentItem("ICICIBANK", "ICICI Bank Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1195.80),
    InstrumentItem("INFY", "Infosys Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1620.30),
    InstrumentItem("BHARTIARTL", "Bharti Airtel Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1450.20),
    InstrumentItem("SBIN", "State Bank of India", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=845.50),
    InstrumentItem("ITC", "ITC Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=485.60),
    InstrumentItem("LICI", "Life Insurance Corporation of India", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1045.00),
    InstrumentItem("HINDUNILVR", "Hindustan Unilever Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=2680.00),
    InstrumentItem("LT", "Larsen & Toubro Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=3620.00),
    InstrumentItem("BAJFINANCE", "Bajaj Finance Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=6980.00),
    InstrumentItem("MARUTI", "Maruti Suzuki India Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=12450.00),
    InstrumentItem("HCLTECH", "HCL Technologies Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1640.00),
    InstrumentItem("SUNPHARMA", "Sun Pharmaceutical Industries Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1750.00),
    InstrumentItem("TATAMOTORS", "Tata Motors Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=985.20),
    InstrumentItem("ONGC", "Oil & Natural Gas Corporation Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=315.40),
    InstrumentItem("NTPC", "NTPC Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=410.00),
    InstrumentItem("AXISBANK", "Axis Bank Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1180.00),
    InstrumentItem("KOTAKBANK", "Kotak Mahindra Bank Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1810.00),
    InstrumentItem("TITAN", "Titan Company Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=3520.00),
    InstrumentItem("ADANIENT", "Adani Enterprises Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=3020.00),
    InstrumentItem("ADANIPORTS", "Adani Ports and Special Economic Zone Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1480.00),
    InstrumentItem("POWERGRID", "Power Grid Corporation of India Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=335.00),
    InstrumentItem("COALINDIA", "Coal India Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=515.00),
    InstrumentItem("ULTRACEMCO", "UltraTech Cement Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=11200.00),
    InstrumentItem("BAJAJFINSV", "Bajaj Finserv Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1620.00),
    InstrumentItem("ASIANPAINT", "Asian Paints Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=2980.00),
    InstrumentItem("M&M", "Mahindra & Mahindra Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=2780.00),
    InstrumentItem("TATASTEEL", "Tata Steel Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=158.00),
    InstrumentItem("JSWSTEEL", "JSW Steel Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=940.00),
    InstrumentItem("GRASIM", "Grasim Industries Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=2650.00),
    InstrumentItem("WIPRO", "Wipro Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=510.00),
    InstrumentItem("CIPLA", "Cipla Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1520.00),
    InstrumentItem("DRREDDY", "Dr. Reddy's Laboratories Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=6840.00),
    InstrumentItem("DIVISLAB", "Divi's Laboratories Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=4780.00),
    InstrumentItem("EICHERMOT", "Eicher Motors Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=4820.00),
    InstrumentItem("BAJAJ-AUTO", "Bajaj Auto Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=9850.00),
    InstrumentItem("HEROMOTOCO", "Hero MotoCorp Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=5320.00),
    InstrumentItem("BRITANNIA", "Britannia Industries Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=5750.00),
    InstrumentItem("NESTLEIND", "Nestle India Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=2510.00),
    InstrumentItem("BPCL", "Bharat Petroleum Corporation Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=345.00),
    InstrumentItem("IOC", "Indian Oil Corporation Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=175.00),
    InstrumentItem("GAIL", "GAIL (India) Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=230.00),
    InstrumentItem("TATAPOWER", "Tata Power Company Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=435.00),
    InstrumentItem("ZOMATO", "Zomato Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=245.00),
    InstrumentItem("PAYTM", "One97 Communications Ltd. (Paytm)", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=565.00),
    InstrumentItem("JIOFIN", "Jio Financial Services Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=325.00),
    InstrumentItem("HAL", "Hindustan Aeronautics Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=4850.00),
    InstrumentItem("BEL", "Bharat Electronics Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=295.00),
    InstrumentItem("TRENT", "Trent Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=6480.00),
    InstrumentItem("VEDL", "Vedanta Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=445.00),
    InstrumentItem("IRCTC", "Indian Railway Catering and Tourism Corp Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=940.00),
    InstrumentItem("IRFC", "Indian Railway Finance Corporation Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=180.00),
    InstrumentItem("RVNL", "Rail Vikas Nigam Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=565.00),
    InstrumentItem("PFC", "Power Finance Corporation Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=520.00),
    InstrumentItem("RECLTD", "REC Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=615.00),
    InstrumentItem("SUZLON", "Suzlon Energy Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=78.50),
    InstrumentItem("YESBANK", "Yes Bank Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=24.20),
    InstrumentItem("IDEA", "Vodafone Idea Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=15.80),
    InstrumentItem("POLYCAB", "Polycab India Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=6750.00),
    InstrumentItem("KPITTECH", "KPIT Technologies Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=1780.00),
    InstrumentItem("TATAELXSI", "Tata Elxsi Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=6980.00),
    InstrumentItem("COFORGE", "Coforge Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=6240.00),
    InstrumentItem("DMART", "Avenue Supermarts Ltd. (DMart)", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=4850.00),
    InstrumentItem("NAUKRI", "Info Edge (India) Ltd.", "NSE", "EQUITY", "EQ", 1, 0.05, base_price=6950.00),

    # ── Major Indices & Index Derivatives (NIFTY 50, BANKNIFTY, FINNIFTY) ────
    InstrumentItem("NIFTY50", "NIFTY 50 Benchmark Index", "NSE", "EQUITY", "INDEX", 25, 0.05, base_price=24850.50),
    InstrumentItem("BANKNIFTY", "NIFTY Bank Index", "NSE", "EQUITY", "INDEX", 15, 0.05, base_price=51200.75),
    InstrumentItem("FINNIFTY", "NIFTY Financial Services Index", "NSE", "EQUITY", "INDEX", 25, 0.05, base_price=23450.20),
    InstrumentItem("MIDCPNIFTY", "NIFTY Midcap Select Index", "NSE", "EQUITY", "INDEX", 50, 0.05, base_price=12850.00),
    InstrumentItem("SENSEX", "BSE SENSEX 30 Index", "BSE", "EQUITY", "INDEX", 10, 0.05, base_price=81500.00),

    # NIFTY Options & Futures Strikes
    InstrumentItem("NIFTY FUT", "NIFTY 50 Monthly Future", "NFO", "FNO", "FUT", 25, 0.05, base_price=24875.00, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24800 CE", "NIFTY 24800 Call Option", "NFO", "FNO", "CE", 25, 0.05, strike=24800.0, base_price=145.50, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24800 PE", "NIFTY 24800 Put Option", "NFO", "FNO", "PE", 25, 0.05, strike=24800.0, base_price=95.20, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24900 CE", "NIFTY 24900 Call Option", "NFO", "FNO", "CE", 25, 0.05, strike=24900.0, base_price=88.40, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24900 PE", "NIFTY 24900 Put Option", "NFO", "FNO", "PE", 25, 0.05, strike=24900.0, base_price=138.00, expiry="2024-08-29"),
    InstrumentItem("NIFTY 25000 CE", "NIFTY 25000 Call Option", "NFO", "FNO", "CE", 25, 0.05, strike=25000.0, base_price=48.20, expiry="2024-08-29"),
    InstrumentItem("NIFTY 25000 PE", "NIFTY 25000 Put Option", "NFO", "FNO", "PE", 25, 0.05, strike=25000.0, base_price=198.50, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24700 CE", "NIFTY 24700 Call Option", "NFO", "FNO", "CE", 25, 0.05, strike=24700.0, base_price=215.00, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24700 PE", "NIFTY 24700 Put Option", "NFO", "FNO", "PE", 25, 0.05, strike=24700.0, base_price=64.00, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24600 CE", "NIFTY 24600 Call Option", "NFO", "FNO", "CE", 25, 0.05, strike=24600.0, base_price=295.00, expiry="2024-08-29"),
    InstrumentItem("NIFTY 24600 PE", "NIFTY 24600 Put Option", "NFO", "FNO", "PE", 25, 0.05, strike=24600.0, base_price=42.00, expiry="2024-08-29"),

    # BANKNIFTY Options & Futures Strikes
    InstrumentItem("BANKNIFTY FUT", "BANKNIFTY Monthly Future", "NFO", "FNO", "FUT", 15, 0.05, base_price=51280.00, expiry="2024-08-29"),
    InstrumentItem("BANKNIFTY 51200 CE", "BANKNIFTY 51200 Call Option", "NFO", "FNO", "CE", 15, 0.05, strike=51200.0, base_price=280.00, expiry="2024-08-29"),
    InstrumentItem("BANKNIFTY 51200 PE", "BANKNIFTY 51200 Put Option", "NFO", "FNO", "PE", 15, 0.05, strike=51200.0, base_price=260.00, expiry="2024-08-29"),
    InstrumentItem("BANKNIFTY 51500 CE", "BANKNIFTY 51500 Call Option", "NFO", "FNO", "CE", 15, 0.05, strike=51500.0, base_price=165.00, expiry="2024-08-29"),
    InstrumentItem("BANKNIFTY 51500 PE", "BANKNIFTY 51500 Put Option", "NFO", "FNO", "PE", 15, 0.05, strike=51500.0, base_price=420.00, expiry="2024-08-29"),
    InstrumentItem("BANKNIFTY 51000 CE", "BANKNIFTY 51000 Call Option", "NFO", "FNO", "CE", 15, 0.05, strike=51000.0, base_price=395.00, expiry="2024-08-29"),
    InstrumentItem("BANKNIFTY 51000 PE", "BANKNIFTY 51000 Put Option", "NFO", "FNO", "PE", 15, 0.05, strike=51000.0, base_price=175.00, expiry="2024-08-29"),

    # Single Stock F&O
    InstrumentItem("RELIANCE FUT", "Reliance Industries Monthly Future", "NFO", "FNO", "FUT", 250, 0.05, base_price=2995.00, expiry="2024-08-29"),
    InstrumentItem("RELIANCE 3000 CE", "Reliance 3000 Call Option", "NFO", "FNO", "CE", 250, 0.05, strike=3000.0, base_price=38.50, expiry="2024-08-29"),
    InstrumentItem("RELIANCE 3000 PE", "Reliance 3000 Put Option", "NFO", "FNO", "PE", 250, 0.05, strike=3000.0, base_price=52.00, expiry="2024-08-29"),
    InstrumentItem("TCS FUT", "TCS Monthly Future", "NFO", "FNO", "FUT", 175, 0.05, base_price=3955.00, expiry="2024-08-29"),
    InstrumentItem("TCS 4000 CE", "TCS 4000 Call Option", "NFO", "FNO", "CE", 175, 0.05, strike=4000.0, base_price=42.00, expiry="2024-08-29"),
    InstrumentItem("HDFCBANK FUT", "HDFC Bank Monthly Future", "NFO", "FNO", "FUT", 550, 0.05, base_price=1690.00, expiry="2024-08-29"),
    InstrumentItem("HDFCBANK 1700 CE", "HDFC Bank 1700 Call Option", "NFO", "FNO", "CE", 550, 0.05, strike=1700.0, base_price=18.40, expiry="2024-08-29"),

    # ── MCX Commodity Contracts ───────────────────────────────────────────────
    InstrumentItem("CRUDEOIL", "MCX Crude Oil Future", "MCX", "COMMODITY", "FUT", 100, 1.0, base_price=6450.00, expiry="2024-09-19"),
    InstrumentItem("GOLD", "MCX Gold 1kg Future", "MCX", "COMMODITY", "FUT", 1, 1.0, base_price=71800.00, expiry="2024-10-05"),
    InstrumentItem("GOLDM", "MCX Gold Mini 100g Future", "MCX", "COMMODITY", "FUT", 10, 1.0, base_price=7180.00, expiry="2024-10-05"),
    InstrumentItem("SILVER", "MCX Silver 30kg Future", "MCX", "COMMODITY", "FUT", 30, 1.0, base_price=84500.00, expiry="2024-09-05"),
    InstrumentItem("SILVERM", "MCX Silver Mini 5kg Future", "MCX", "COMMODITY", "FUT", 5, 1.0, base_price=8450.00, expiry="2024-09-05"),
    InstrumentItem("NATURALGAS", "MCX Natural Gas Future", "MCX", "COMMODITY", "FUT", 1250, 0.1, base_price=185.20, expiry="2024-08-27"),
    InstrumentItem("COPPER", "MCX Copper Future", "MCX", "COMMODITY", "FUT", 2500, 0.05, base_price=798.50, expiry="2024-08-30"),
    InstrumentItem("ZINC", "MCX Zinc Future", "MCX", "COMMODITY", "FUT", 5000, 0.05, base_price=265.40, expiry="2024-08-30"),
    InstrumentItem("ALUMINIUM", "MCX Aluminium Future", "MCX", "COMMODITY", "FUT", 5000, 0.05, base_price=215.80, expiry="2024-08-30"),

    # ── Currency & Forex Pairs ────────────────────────────────────────────────
    InstrumentItem("USDINR", "USD/INR Currency Pair", "CDS", "FOREX", "FUT", 1000, 0.0025, base_price=83.95),
    InstrumentItem("EURINR", "EUR/INR Currency Pair", "CDS", "FOREX", "FUT", 1000, 0.0025, base_price=91.40),
    InstrumentItem("GBPINR", "GBP/INR Currency Pair", "CDS", "FOREX", "FUT", 1000, 0.0025, base_price=108.20),
    InstrumentItem("JPYINR", "JPY/INR Currency Pair", "CDS", "FOREX", "FUT", 1000, 0.0025, base_price=57.10),
    InstrumentItem("EURUSD", "Euro / US Dollar Spot", "FOREX", "FOREX", "SPOT", 1, 0.0001, base_price=1.0885),
    InstrumentItem("GBPUSD", "British Pound / US Dollar Spot", "FOREX", "FOREX", "SPOT", 1, 0.0001, base_price=1.2940),
    InstrumentItem("USDJPY", "US Dollar / Japanese Yen Spot", "FOREX", "FOREX", "SPOT", 1, 0.01, base_price=147.20),

    # ── Crypto Pairs ──────────────────────────────────────────────────────────
    InstrumentItem("BTCUSDT", "Bitcoin / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.01, base_price=61250.00),
    InstrumentItem("ETHUSDT", "Ethereum / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.01, base_price=2680.50),
    InstrumentItem("SOLUSDT", "Solana / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.01, base_price=148.20),
    InstrumentItem("BNBUSDT", "BNB / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.01, base_price=575.00),
    InstrumentItem("XRPUSDT", "XRP / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.0001, base_price=0.5850),
    InstrumentItem("DOGEUSDT", "Dogecoin / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.00001, base_price=0.1045),
    InstrumentItem("ADAUSDT", "Cardano / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.0001, base_price=0.3650),
    InstrumentItem("AVAXUSDT", "Avalanche / Tether Spot", "BINANCE", "CRYPTO", "CRYPTO", 1, 0.01, base_price=24.80),
]


class InstrumentMasterService:
    """Singleton service for fast search, scrip resolution, and live instrument updates."""

    def __init__(self) -> None:
        self._instruments: list[InstrumentItem] = list(_INSTRUMENT_CATALOGUE)
        self._lookup: dict[str, InstrumentItem] = {i.symbol.upper(): i for i in self._instruments}
        self._synced_external: bool = False

    def get_instrument(self, symbol: str) -> Optional[InstrumentItem]:
        """Find instrument by exact symbol ticker."""
        clean = symbol.strip().upper()
        if clean in self._lookup:
            return self._lookup[clean]

        # Dynamic fallback parser for synthetic option/future strikes
        if any(keyword in clean for keyword in ("CE", "PE", "FUT")):
            return InstrumentItem(
                symbol=clean,
                name=f"{clean} Contract",
                exchange="NFO",
                segment="FNO",
                instrument_type="CE" if "CE" in clean else "PE" if "PE" in clean else "FUT",
                lot_size=25 if "NIFTY" in clean else 100,
                base_price=150.0,
            )

        # Default equity fallback
        return InstrumentItem(
            symbol=clean,
            name=f"{clean} Listed Equity",
            exchange="NSE",
            segment="EQUITY",
            instrument_type="EQ",
            lot_size=1,
            base_price=1000.0,
        )

    def search(
        self,
        query: str,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search instruments by symbol, company name, strike, or derivative type."""
        q = query.strip().upper() if query else ""
        results = []

        for inst in self._instruments:
            # Segment filter
            if segment and segment.upper() not in ("ALL", "") and inst.segment.upper() != segment.upper():
                continue

            # Exchange filter
            if exchange and exchange.upper() not in ("ALL", "") and inst.exchange.upper() != exchange.upper():
                continue

            # Search query matching
            if q:
                sym = inst.symbol.upper()
                name = inst.name.upper()
                if q not in sym and q not in name:
                    continue

            results.append(inst.to_dict())
            if len(results) >= limit:
                break

        # If user searched an unknown symbol specifically, synthesize an NSE result
        if not results and q and len(q) >= 2:
            syn = self.get_instrument(q)
            if syn:
                results.append(syn.to_dict())

        return results

    async def sync_from_broker_if_needed(self) -> None:
        """Fetch live instrument scrip master from Angel One or Zerodha API in background."""
        if self._synced_external:
            return
        try:
            url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    added = 0
                    for row in data[:3000]:  # Index top 3,000 active scrips
                        sym = str(row.get("symbol", "")).strip().upper()
                        if not sym or sym in self._lookup:
                            continue
                        exch = str(row.get("exch_seg", "NSE")).strip().upper()
                        name = str(row.get("name", sym)).strip()
                        inst_type = str(row.get("instrumenttype", "EQ")).strip().upper()
                        strike = float(row.get("strike", 0.0)) if row.get("strike") else None
                        expiry = str(row.get("expiry", "")).strip() or None
                        lot_size = int(row.get("lotsize", 1)) if row.get("lotsize") else 1

                        segment = "EQUITY"
                        if exch in ("NFO", "BFO"):
                            segment = "FNO"
                        elif exch == "MCX":
                            segment = "COMMODITY"
                        elif exch == "CDS":
                            segment = "FOREX"

                        item = InstrumentItem(
                            symbol=sym,
                            name=name,
                            exchange=exch,
                            segment=segment,
                            instrument_type=inst_type,
                            lot_size=lot_size,
                            strike=strike,
                            expiry=expiry,
                            base_price=1000.0,
                        )
                        self._instruments.append(item)
                        self._lookup[sym] = item
                        added += 1

                    self._synced_external = True
                    logger.info("Successfully synced %d additional live market scrips into InstrumentMaster", added)
        except Exception as exc:
            logger.info("External scrip master sync bypassed: %s (Standard catalogue active with %d instruments)", exc, len(self._instruments))


# Singleton instance
instrument_master = InstrumentMasterService()
