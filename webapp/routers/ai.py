"""FastAPI router for Anthropic Claude AI integration in Gold Trading Terminal.

Routes:
- POST /api/ai/chat/stream : Streaming AI response
- POST /api/ai/auto-quant   : Autonomous AI Quant Pipeline (Chart analysis, 1-yr backtests, demo trade start, report)
- POST /api/ai/analyze-market : Fast 1-click market structure analysis
- GET  /api/ai/reports      : History of generated audit reports
- GET  /api/ai/status       : Anthropic API connection status
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from webapp.ai_service import (
    build_market_context,
    get_anthropic_client,
    get_default_model,
    stream_ai_chat,
)
from webapp.ai_quant_engine import AI_REPORTS_CACHE, run_auto_quant_pipeline

router = APIRouter()


class ChatRequest(BaseModel):
    prompt: str
    history: list[dict[str, str]] = []
    timeframe: str = "15min"
    model: str | None = None


class AutoQuantRequest(BaseModel):
    timeframe: str = "15min"
    min_return_pct: float = 0.0
    max_dd_pct: float = 25.0
    auto_start_demo: bool = True
    model: str | None = None


@router.get("/status")
def get_ai_status():
    """Check Anthropic client configuration and service health."""
    try:
        client = get_anthropic_client()
        default_model = get_default_model()
        return {
            "status": "ready",
            "base_url": str(client.base_url),
            "model": default_model,
            "reports_count": len(AI_REPORTS_CACHE),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


@router.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """Stream AI response from Anthropic Claude."""
    try:
        generator = stream_ai_chat(
            prompt=req.prompt,
            history=req.history,
            timeframe=req.timeframe,
            model=req.model,
        )
        return StreamingResponse(generator, media_type="text/plain; charset=utf-8")
    except Exception as exc:
        raise HTTPException(500, f"AI streaming failed: {exc}")


@router.post("/auto-quant")
def trigger_auto_quant(req: AutoQuantRequest):
    """Trigger the complete autonomous quant pipeline."""
    try:
        report = run_auto_quant_pipeline(
            timeframe=req.timeframe,
            min_return_pct=req.min_return_pct,
            max_dd_pct=req.max_dd_pct,
            auto_start_demo=req.auto_start_demo,
            model=req.model,
        )
        return report
    except Exception as exc:
        raise HTTPException(500, f"Autonomous AI Quant pipeline failed: {exc}")


@router.post("/analyze-market")
def analyze_market(req: ChatRequest):
    """Fast 1-click market structure scan."""
    prompt = (
        "Perform a high-level quantitative market structure breakdown for XAU/USD gold. "
        "Detail:\n"
        "1. Current Market Trend & Momentum (RSI / MACD / EMAs)\n"
        "2. Key Support & Resistance Levels derived from current price\n"
        "3. Recommended Trading Bias (Long / Short / Neutral)\n"
        "4. Optimal Risk Management / SL distance based on current ATR"
    )
    req.prompt = prompt
    return chat_stream(req)


@router.get("/reports")
def list_reports():
    """List history of generated AI quant reports."""
    return AI_REPORTS_CACHE
