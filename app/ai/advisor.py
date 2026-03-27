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
2. Identify which strategy + timeframe + parameters work best RIGHT NOW based on the backtest data
3. Explain WHY in simple terms (2-3 sentences)
4. Give a CONFIDENCE score (0-100) — use low values (10-30) if data is sparse or unavailable
5. List top 3 RISKS
6. Provide a SUMMARY recommendation

███ CRITICAL — ALGORITHM KEYS ███
The "recommended_algorithm" field and all "algorithm" fields in "top_3_strategies"
MUST be EXACTLY one of these values (case-sensitive):
  "ma_crossover"  — Moving Average Crossover
  "rsi"           — RSI Oscillator
  "macd"          — MACD
  "supertrend"    — SuperTrend
  "bb_bounce"     — Bollinger Band Bounce

Do NOT invent algorithm names like "mean_reversion", "trend_following", "momentum",
"breakout_strategy" or anything else. Use ONLY the exact keys listed above.
Any other value is a system error.
██████████████████████████████████

RESPOND ONLY in valid JSON with this exact structure:
{
  "market_regime": "trending_up|trending_down|ranging|high_volatility|breakout",
  "regime_explanation": "Brief description of current market conditions",
  "recommended_algorithm": "<one of the 5 keys above>",
  "recommended_timeframe": "5m|15m|1h|4h|1d",
  "recommended_params": {},
  "confidence": 0-100,
  "reasoning": "2-3 sentence explanation of why this strategy fits current conditions",
  "risks": ["risk1", "risk2", "risk3"],
  "top_3_strategies": [
    {"algorithm": "<one of the 5 keys>", "timeframe": "tf", "score": 0, "note": "brief note"},
    {"algorithm": "<one of the 5 keys>", "timeframe": "tf", "score": 0, "note": "brief note"},
    {"algorithm": "<one of the 5 keys>", "timeframe": "tf", "score": 0, "note": "brief note"}
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


# ── Algorithm key validation ───────────────────────────────────────────────

VALID_ALGOS = {"ma_crossover", "rsi", "macd", "supertrend", "bb_bounce"}

_DEFAULT_PARAMS_FALLBACK: dict[str, dict] = {
    "ma_crossover": {"fast_ma": 7,  "slow_ma": 25, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "rsi":          {"rsi_period": 14, "oversold": 30, "overbought": 70, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "macd":         {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "supertrend":   {"st_period": 10, "st_multiplier": 3.0, "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
    "bb_bounce":    {"bb_period": 20, "bb_std": 2.0, "bb_exit": "middle", "stop_loss_pct": 2.0, "take_profit_pct": 4.0},
}


def _validate_and_fix_algorithms(result: dict, scan_data: dict) -> None:
    """
    Ensure Claude used only valid algorithm keys.
    If not, replace with the best combo from scanner data or a safe default.
    Mutates result in place.
    """
    best_combos = scan_data.get("best_combinations", [])

    def _best_valid_algo() -> tuple[str, str, dict]:
        """Return (algorithm, timeframe, params) from top scanner combos."""
        for combo in best_combos:
            if combo.get("algorithm") in VALID_ALGOS:
                return (
                    combo["algorithm"],
                    combo.get("timeframe", "1h"),
                    combo.get("params", _DEFAULT_PARAMS_FALLBACK.get(combo["algorithm"], {})),
                )
        return "ma_crossover", "1h", _DEFAULT_PARAMS_FALLBACK["ma_crossover"]

    # Fix recommended_algorithm
    if result.get("recommended_algorithm") not in VALID_ALGOS:
        bad_name = result.get("recommended_algorithm", "?")
        algo, tf, params = _best_valid_algo()
        logger.warning(
            "Claude hallucinated algorithm '%s' — corrected to '%s'", bad_name, algo
        )
        result["recommended_algorithm"] = algo
        if not result.get("recommended_timeframe") or result["recommended_timeframe"] not in ("5m", "15m", "1h", "4h", "1d"):
            result["recommended_timeframe"] = tf
        if not result.get("recommended_params"):
            result["recommended_params"] = params

    # Fix top_3_strategies
    valid_top3 = [s for s in result.get("top_3_strategies", []) if s.get("algorithm") in VALID_ALGOS]
    if not valid_top3:
        for combo in best_combos[:3]:
            if combo.get("algorithm") in VALID_ALGOS:
                valid_top3.append({
                    "algorithm": combo["algorithm"],
                    "timeframe": combo.get("timeframe", "1h"),
                    "score": round(combo.get("score", 0), 1),
                    "note": (
                        f"Backtest: win_rate={combo.get('win_rate', 0)}%, "
                        f"return={combo.get('return_pct', 0)}%"
                    ),
                })
    result["top_3_strategies"] = valid_top3[:3]


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

        # ── Validate algorithm keys to prevent Claude hallucination ──────────
        _validate_and_fix_algorithms(result, scan_data)

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
