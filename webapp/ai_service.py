"""Anthropic AI Service for Gold Trading Terminal.

Initializes the Anthropic client using the user's provided pattern:
    client = Anthropic(
        base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.openapis.online/anthropic"),
        api_key=os.getenv("ANTHROPIC_API_KEY", "admin"),
    )

Provides context building from live market data, indicators, risk settings, and streaming text generation.
"""

from __future__ import annotations

import os
from typing import Generator, Any
import pandas as pd
import numpy as np

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

from webapp.routers.market import _raw, TF_MAP
from gold_bot.data import resample
from gold_bot.indicators import compute_all_indicators
from webapp import db, biquote_client


def get_anthropic_client() -> Anthropic:
    if Anthropic is None:
        raise RuntimeError("anthropic package is not installed. Please install anthropic>=0.18.0")
    
    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.openapis.online/anthropic").rstrip("/")
    api_key = os.getenv("ANTHROPIC_API_KEY", "admin")
    return Anthropic(base_url=base_url, api_key=api_key)


def get_default_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")


def build_market_context(timeframe: str = "15min") -> dict[str, Any]:
    """Gather complete current market context for Claude reasoning."""
    df, meta = _raw()
    alias, minutes = TF_MAP.get(timeframe, ("15min", 15))
    base_minutes = meta.get("base_minutes", 15)
    
    if minutes == base_minutes:
        bar_df = df.copy()
    else:
        bar_df = resample(df, alias)

    if not bar_df.empty:
        ind_df = compute_all_indicators(bar_df.copy())
        last_row = ind_df.iloc[-1]
        prev_row = ind_df.iloc[-2] if len(ind_df) > 1 else last_row
        
        latest_price = float(last_row.get("close", 0.0))
        rsi = float(last_row.get("rsi", 50.0)) if pd.notna(last_row.get("rsi")) else None
        macd = float(last_row.get("macd", 0.0)) if pd.notna(last_row.get("macd")) else None
        macd_signal = float(last_row.get("macd_signal", 0.0)) if pd.notna(last_row.get("macd_signal")) else None
        atr = float(last_row.get("atr", 0.0)) if pd.notna(last_row.get("atr")) else None
        ema_20 = float(last_row.get("ema_20", 0.0)) if pd.notna(last_row.get("ema_20")) else None
        ema_50 = float(last_row.get("ema_50", 0.0)) if pd.notna(last_row.get("ema_50")) else None
        ema_200 = float(last_row.get("ema_200", 0.0)) if pd.notna(last_row.get("ema_200")) else None
        
        # Calculate 24h high/low and change
        trailing_24h = ind_df.tail(96)  # ~96 15m bars in 24h
        h24 = float(trailing_24h["high"].max())
        l24 = float(trailing_24h["low"].min())
        change_24h = round(latest_price - float(ind_df.iloc[-96]["close"]), 2) if len(ind_df) >= 96 else 0.0
    else:
        latest_price = 0.0
        rsi = macd = macd_signal = atr = ema_20 = ema_50 = ema_200 = None
        h24 = l24 = change_24h = 0.0

    # Live tick from biquote
    tick = biquote_client.get_tick()
    
    # Active risk settings & DB saved strategies
    conn = db.get_conn()
    try:
        risk_row = conn.execute("SELECT * FROM risk_settings WHERE id = 1").fetchone()
        risk_settings = dict(risk_row) if risk_row else {}
        
        open_positions = [dict(r) for r in conn.execute("SELECT * FROM paper_positions").fetchall()]
        saved_strategies = [dict(r) for r in conn.execute("SELECT id, name, strategy_class FROM strategies").fetchall()]
    finally:
        conn.close()

    return {
        "symbol": "XAU/USD (Gold)",
        "timeframe": timeframe,
        "latest_price": latest_price,
        "tick": tick,
        "change_24h": change_24h,
        "high_24h": h24,
        "low_24h": l24,
        "indicators": {
            "rsi_14": round(rsi, 2) if rsi is not None else None,
            "macd": round(macd, 3) if macd is not None else None,
            "macd_signal": round(macd_signal, 3) if macd_signal is not None else None,
            "atr_14": round(atr, 2) if atr is not None else None,
            "ema_20": round(ema_20, 2) if ema_20 is not None else None,
            "ema_50": round(ema_50, 2) if ema_50 is not None else None,
            "ema_200": round(ema_200, 2) if ema_200 is not None else None,
        },
        "risk_settings": risk_settings,
        "open_positions_count": len(open_positions),
        "open_positions": open_positions[:5],
        "saved_strategies_count": len(saved_strategies),
        "saved_strategies": saved_strategies,
    }


def stream_ai_chat(
    prompt: str,
    history: list[dict[str, str]] | None = None,
    timeframe: str = "15min",
    model: str | None = None,
    max_tokens: int = 1024,
) -> Generator[str, None, None]:
    """Stream AI response using Anthropic client streaming pattern."""
    client = get_anthropic_client()
    target_model = model or get_default_model()
    context = build_market_context(timeframe=timeframe)

    system_prompt = (
        "You are Claude, an expert Quantitative Gold (XAU/USD) Trader, Financial Analyst, "
        "and AI Strategy Developer for the Gold Trading Terminal.\n"
        "You provide actionable, mathematically sound quantitative insights, market chart analyses, "
        "risk evaluations, and strategy optimization suggestions.\n\n"
        "Current Market Context:\n"
        f"- Symbol: {context['symbol']} (Timeframe: {context['timeframe']})\n"
        f"- Spot Price: ${context['latest_price']:.2f} (24h Change: {context['change_24h']:+.2f}, 24h High: ${context['high_24h']:.2f}, Low: ${context['low_24h']:.2f})\n"
        f"- Key Technical Indicators: RSI(14)={context['indicators']['rsi_14']}, MACD={context['indicators']['macd']}, "
        f"ATR(14)={context['indicators']['atr_14']}, EMA20={context['indicators']['ema_20']}, EMA50={context['indicators']['ema_50']}, EMA200={context['indicators']['ema_200']}\n"
        f"- Account Open Positions: {context['open_positions_count']} open positions\n"
        f"- Saved Strategies Count: {context['saved_strategies_count']}\n\n"
        "Format your answer with concise, well-structured GitHub-flavored Markdown. "
        "Include clear bullet points, bulleted recommendations, or code snippets when appropriate."
    )

    messages = []
    if history:
        for msg in history:
            role = "user" if msg.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": msg.get("content", "")})
    
    messages.append({"role": "user", "content": prompt})

    try:
        with client.messages.stream(
            model=target_model,
            max_tokens=max_tokens,
            messages=messages,
            system=system_prompt,
        ) as stream:
            for text in stream.text_stream:
                yield text
    except Exception as exc:
        err_msg = str(exc)
        if "503" in err_msg or "maintenance" in err_msg.lower():
            # Fallback local quant analysis engine
            yield from _generate_fallback_quant_analysis(prompt, context)
        else:
            yield f"\n\n> [!WARNING]\n> **AI Assistant Exception**: {exc}"


def _generate_fallback_quant_analysis(prompt: str, context: dict[str, Any]) -> Generator[str, None, None]:
    """Generate intelligent quantitative gold market insights locally when API endpoint is in maintenance."""
    price = context['latest_price']
    rsi_val = context['indicators']['rsi_14'] or 50.0
    atr_val = context['indicators']['atr_14'] or 5.0
    ema20 = context['indicators']['ema_20'] or price
    ema50 = context['indicators']['ema_50'] or price
    ema200 = context['indicators']['ema_200'] or price
    change_24h = context['change_24h']
    
    # Determine trend bias
    if price > ema20 > ema50:
        bias = "Strong Bullish Uptrend 🚀"
        action = "Look for buy pullbacks near EMA 20 support."
    elif price < ema20 < ema50:
        bias = "Bearish Downtrend 📉"
        action = "Look for sell retests near EMA 20 resistance."
    else:
        bias = "Range-Bound / Neutral Consolidation ⚡"
        action = "Trade range boundaries or wait for a confirmed breakout."

    # RSI condition
    if rsi_val >= 70:
        rsi_state = "Overbought (Caution on long entries)"
    elif rsi_val <= 30:
        rsi_state = "Oversold (Watch for reversal buy signals)"
    else:
        rsi_state = f"Neutral ({rsi_val:.1f})"

    analysis = [
        f"### 📊 XAU/USD Quantitative Market Breakdown\n\n",
        f"- **Current Spot Price**: `${price:.2f}` (24h Change: `{change_24h:+.2f}`)\n",
        f"- **Market Structure Bias**: **{bias}**\n",
        f"- **Actionable Guidance**: {action}\n\n",
        f"#### 🔍 Key Indicators & Risk Metrics\n",
        f"1. **RSI (14)**: `{rsi_state}`\n",
        f"2. **ATR (14 Volatility)**: `${atr_val:.2f}` (Suggested SL distance: `${atr_val * 1.5:.2f}` to `${atr_val * 2.0:.2f}`)\n",
        f"3. **Moving Averages**: EMA20 = `${ema20:.2f}`, EMA50 = `${ema50:.2f}`, EMA200 = `${ema200:.2f}`\n\n",
        f"#### 🛡️ Strategy & Risk Parameter Tuning\n",
        f"- **Recommended Stop Loss**: Place SL at least `{1.5 * atr_val:.2f}` points from entry based on current ATR.\n",
        f"- **Target Risk:Reward**: Minimum 1:2.0 Risk to Reward ratio.\n",
        f"- **Auto-Trading Recommendation**: Run the **Second FVG Retest** or **Order Block Sweep** strategy for high win-rate liquidity setups.\n\n",
        f"> [!NOTE]\n",
        f"> *Note: The primary Anthropic proxy endpoint is currently under 503 maintenance; this quantitative analysis was synthesized locally using live XAU/USD indicators.*"
    ]

    import time
    for chunk in analysis:
        yield chunk
        time.sleep(0.05)


