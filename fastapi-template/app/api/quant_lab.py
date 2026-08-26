"""TradeThrone AI Quant Lab API.

Endpoints
---------
POST /api/quant-lab/parse     — English text -> native strategy JSON
POST /api/quant-lab/diagnose  — strategy JSON -> Health Report (score 0-100)
POST /api/quant-lab/analyze   — combined parse + diagnose pipeline
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.quant import diagnose_strategy, parse_strategy_text

router = APIRouter(prefix="/api/quant-lab", tags=["quant-lab"])


class ParseRequest(BaseModel):
    text: str = Field(..., min_length=4, max_length=2000,
                      description="Plain-English strategy description")


class DiagnoseRequest(BaseModel):
    strategy: dict = Field(..., description="Native strategy JSON "
                           "(symbols/conditions/action/risk)")
    days: int = Field(90, ge=10, le=365)
    timeframe: str | None = Field(None)
    capital: float = Field(100_000.0, gt=0)
    product_type: str = Field("INTRADAY")
    slippage_pct: float | None = Field(None, gt=0, le=5)


class AnalyzeRequest(ParseRequest):
    days: int = Field(90, ge=10, le=365)
    timeframe: str | None = Field(None)
    capital: float = Field(100_000.0, gt=0)
    product_type: str = Field("INTRADAY")
    slippage_pct: float | None = Field(None, gt=0, le=5)


@router.post("/parse", summary="Natural language -> strategy JSON")
async def parse_endpoint(req: ParseRequest) -> dict:
    try:
        return parse_strategy_text(req.text)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))


@router.post("/diagnose", summary="AI Strategy Doctor health report")
async def diagnose_endpoint(req: DiagnoseRequest) -> dict:
    try:
        return diagnose_strategy(
            req.strategy,
            days=req.days,
            timeframe=req.timeframe,
            capital=req.capital,
            product_type=req.product_type,
            slippage_pct=req.slippage_pct,
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))


@router.post("/analyze", summary="Parse + Diagnose in one call")
async def analyze_endpoint(req: AnalyzeRequest) -> dict:
    try:
        parsed = parse_strategy_text(req.text)
        report = diagnose_strategy(
            parsed,
            days=req.days,
            timeframe=req.timeframe or parsed.get("timeframe"),
            capital=req.capital,
            product_type=req.product_type,
            slippage_pct=req.slippage_pct,
        )
        return {"parsed": parsed, "health_report": report}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))