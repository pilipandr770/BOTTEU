"""
Level 2 — AI Advisor (Anthropic Claude).

Takes scanner data and produces market regime analysis,
strategy recommendations with confidence scores, and explanations.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _get_client():
    """Lazy-init Anthropic client."""
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=api_key)


SYSTEM_PROMPT = """\
You are an expert quantitative trading analyst. You analyze cryptocurrency markets \
using technical indicators and backtest data to recommend optimal trading strategies.

You will receive structured data containing:
1. Multi-timeframe signals (BUY/SELL/HOLD) from 5 algorithms across 5 timeframes
2. Backtest results (trades, win_rate, return_pct, max_drawdown, sharpe) for each combination
3. Market indicators (ATR%, RSI, SMA crossovers, Bollinger Band Width, price changes)
4. Top 10 ranked strategy combinations

Your job:
1. Determine the current MARKET REGIME (one of: trending_up, trending_down, ranging, high_volatility, breakout)
2. Identify which strategy + timeframe + parameters work best RIGHT NOW
3. Explain WHY in simple terms (2-3 sentences)
4. Give a CONFIDENCE score (0-100)
5. List top 3 RISKS
6. Provide a SUMMARY recommendation

RESPOND ONLY in valid JSON with this exact structure:
{
  "market_regime": "trending_up|trending_down|ranging|high_volatility|breakout",
  "regime_explanation": "Brief description of current market conditions",
  "recommended_algorithm": "algorithm_key",
  "recommended_timeframe": "timeframe",
  "recommended_params": {},
  "confidence": 0-100,
  "reasoning": "2-3 sentence explanation of why this strategy fits current conditions",
  "risks": ["risk1", "risk2", "risk3"],
  "top_3_strategies": [
    {"algorithm": "key", "timeframe": "tf", "score": 0, "note": "brief note"},
    {"algorithm": "key", "timeframe": "tf", "score": 0, "note": "brief note"},
    {"algorithm": "key", "timeframe": "tf", "score": 0, "note": "brief note"}
  ],
  "market_summary": "1-2 paragraph overall market analysis"
}
"""


def _build_user_prompt(scan_data: dict, lang: str = "en") -> str:
    """Build a compact prompt from scanner results to save tokens."""
    lines = [f"Symbol: {scan_data['symbol']}\n"]

    # Signal matrix
    lines.append("=== SIGNAL MATRIX (algorithm × timeframe) ===")
    for tf, tf_data in scan_data.get("timeframes", {}).items():
        if "error" in tf_data:
            lines.append(f"  {tf}: insufficient data")
            continue
        signals = tf_data.get("signals", {})
        market = tf_data.get("market", {})
        sig_str = ", ".join(f"{a}={s}" for a, s in signals.items())
        lines.append(f"  {tf}: {sig_str}")
        if market:
            lines.append(
                f"    price={market.get('price')}, RSI={market.get('rsi')}, "
                f"ATR%={market.get('atr_pct')}, BBW%={market.get('bbw_pct')}, "
                f"above_SMA20={market.get('price_above_sma20')}, "
                f"above_SMA50={market.get('price_above_sma50')}, "
                f"SMA20>SMA50={market.get('sma20_above_sma50')}, "
                f"1d_change={market.get('pct_change_1d')}%, "
                f"7d_change={market.get('pct_change_7d')}%"
            )

    # Best combinations
    lines.append("\n=== TOP 10 STRATEGY COMBINATIONS (by composite score) ===")
    for i, combo in enumerate(scan_data.get("best_combinations", [])[:10]):
        lines.append(
            f"  #{i+1}: {combo['algorithm']} on {combo['timeframe']} "
            f"(variant={combo.get('variant', 'N/A')}): "
            f"trades={combo.get('trades', 0)}, "
            f"win_rate={combo.get('win_rate', 0)}%, "
            f"return={combo.get('return_pct', 0)}%, "
            f"max_dd={combo.get('max_drawdown_pct', 0)}%, "
            f"sharpe={combo.get('sharpe', 0)}, "
            f"score={combo.get('score', 0):.1f}"
        )
        lines.append(f"    params: {json.dumps(combo.get('params', {}))}")

    # Backtest details for top timeframes
    lines.append("\n=== BACKTEST DETAILS (per TF) ===")
    for tf, tf_data in scan_data.get("timeframes", {}).items():
        if "error" in tf_data:
            continue
        bt = tf_data.get("backtests", {})
        for algo_key, variants in bt.items():
            if isinstance(variants, dict) and "default" in variants:
                d = variants["default"]
                lines.append(
                    f"  {tf}/{algo_key}/default: "
                    f"trades={d.get('trades', 0)}, win={d.get('win_rate', 0)}%, "
                    f"ret={d.get('return_pct', 0)}%, dd={d.get('max_drawdown_pct', 0)}%"
                )

    lang_instruction = ""
    if lang == "de":
        lang_instruction = "\n\nIMPORTANT: Write all text fields (regime_explanation, reasoning, risks, market_summary, notes) in German."
    elif lang == "ru":
        lang_instruction = "\n\nIMPORTANT: Write all text fields (regime_explanation, reasoning, risks, market_summary, notes) in Russian."

    lines.append(lang_instruction)

    return "\n".join(lines)


def analyze(scan_data: dict, lang: str = "en") -> dict[str, Any]:
    """
    Send scanner data to Claude and get structured analysis.
    Returns parsed JSON dict with recommendations.
    """
    client = _get_client()
    user_prompt = _build_user_prompt(scan_data, lang)

    logger.info("AI Advisor: sending %d chars to Claude for %s", len(user_prompt), scan_data.get("symbol"))

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[
                {"role": "user", "content": user_prompt}
            ],
            system=SYSTEM_PROMPT,
        )

        response_text = message.content[0].text

        # Parse JSON from response
        # Claude may wrap in ```json ... ```, strip that
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        result = json.loads(text)

        # Add token usage info
        result["_usage"] = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }

        return result

    except json.JSONDecodeError as exc:
        logger.error("AI Advisor: failed to parse Claude response: %s", exc)
        return {
            "error": "Failed to parse AI response",
            "raw_response": response_text[:500] if 'response_text' in dir() else "",
        }
    except Exception as exc:
        logger.error("AI Advisor: Anthropic API error: %s", exc)
        return {"error": str(exc)}
