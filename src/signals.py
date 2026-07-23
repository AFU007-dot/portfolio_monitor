"""
Pure signal-detection functions. No I/O; fully unit-testable.

Signals:
    1. Consecutive bearish daily closes (close < open)
    2. 10% intraday drop  — current_price vs today's open (during market hours)
    3. 10% end-of-day drop — today's close vs today's open (post-close)
    4. Break below N-day moving average with confirmation buffer
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd


@dataclass
class SignalResult:
    signal_key: str
    triggered: bool
    summary: str
    detail: str = ""
    metrics: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1) Bearish streak (completed daily bars)
# ---------------------------------------------------------------------------
def detect_bearish_streak(daily: pd.DataFrame, required: int) -> SignalResult:
    if daily is None or len(daily) < required:
        return SignalResult(
            "bearish_streak", False,
            summary=f"insufficient history ({0 if daily is None else len(daily)} bars)",
        )

    tail = daily.tail(required)
    all_bearish = bool((tail["close"] < tail["open"]).all())
    streak = _trailing_streak(daily)

    rows = [
        f"  {str(d)[:10]} O={o:>10.2f}  C={c:>10.2f}  Δ={c - o:+.2f}"
        for d, o, c in zip(tail.index, tail["open"], tail["close"])
    ]
    return SignalResult(
        "bearish_streak",
        triggered=all_bearish,
        summary=f"{streak} consecutive bearish daily closes (need {required})",
        detail="\n".join(rows),
        metrics={"streak_length": streak, "required": required},
    )


def _trailing_streak(daily: pd.DataFrame) -> int:
    count = 0
    for _, row in daily.iloc[::-1].iterrows():
        if row["close"] < row["open"]:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# 2) Intraday drop — current price vs today's open (during market hours)
# ---------------------------------------------------------------------------
def detect_intraday_drop(today_open: Optional[float], current_price: Optional[float],
                        threshold_pct: float) -> SignalResult:
    """
    Fires when a live intraday quote is >= threshold_pct below today's OPEN.
    yfinance intraday quotes are delayed 15-20 min on the free tier, so this
    alert will fire ~15-20 min after the level is actually breached.
    """
    if (today_open is None or today_open <= 0 or
            current_price is None or current_price <= 0):
        return SignalResult("intraday_drop", False, "no live intraday quote")

    drop_pct = (today_open - current_price) / today_open * 100.0
    return SignalResult(
        "intraday_drop",
        triggered=drop_pct >= threshold_pct,
        summary=(
            f"Today's open ${today_open:.2f} → now ${current_price:.2f} "
            f"({-drop_pct:+.2f}%)"
        ),
        detail=f"Threshold: -{threshold_pct:.1f}% | Live drop: -{drop_pct:.2f}%",
        metrics={
            "today_open": today_open, "current_price": current_price,
            "drop_pct": drop_pct,
        },
    )


# ---------------------------------------------------------------------------
# 3) End-of-day drop — today's close vs today's open (completed candle)
# ---------------------------------------------------------------------------
def detect_eod_drop(daily_bar: Optional[pd.Series], threshold_pct: float) -> SignalResult:
    """
    Fires when the most recent COMPLETED daily candle closed >= threshold_pct
    below its own open. Use this on the post-close cron run.
    `daily_bar` is a pandas Series with 'open' and 'close' keys.
    """
    if daily_bar is None or pd.isna(daily_bar.get("open")) or pd.isna(daily_bar.get("close")):
        return SignalResult("eod_drop", False, "no completed daily bar")

    o = float(daily_bar["open"])
    c = float(daily_bar["close"])
    if o <= 0:
        return SignalResult("eod_drop", False, "invalid open price")

    drop_pct = (o - c) / o * 100.0
    return SignalResult(
        "eod_drop",
        triggered=drop_pct >= threshold_pct,
        summary=f"Daily candle: Open ${o:.2f} → Close ${c:.2f} ({-drop_pct:+.2f}%)",
        detail=f"Threshold: -{threshold_pct:.1f}% | Actual: -{drop_pct:.2f}%",
        metrics={"open": o, "close": c, "drop_pct": drop_pct},
    )


# ---------------------------------------------------------------------------
# 4) Break below N-day moving average
# ---------------------------------------------------------------------------
def detect_ma_break(daily: pd.DataFrame, period: int,
                    confirmation_pct: float = 1.0) -> SignalResult:
    if daily is None or len(daily) < period + 1:
        return SignalResult("ma_break", False,
                            summary=f"insufficient history for {period}-DMA")

    closes = daily["close"]
    ma = closes.rolling(period).mean()
    last_close = float(closes.iloc[-1])
    last_ma = float(ma.iloc[-1])
    if pd.isna(last_ma):
        return SignalResult("ma_break", False, summary=f"{period}-DMA not yet available")

    delta_pct = (last_close - last_ma) / last_ma * 100.0
    triggered = delta_pct <= -confirmation_pct

    prev_close = float(closes.iloc[-2])
    prev_ma = float(ma.iloc[-2]) if not pd.isna(ma.iloc[-2]) else last_ma
    fresh_break = prev_close >= prev_ma and last_close < last_ma

    summary = (
        f"Close ${last_close:.2f} vs {period}-DMA ${last_ma:.2f} "
        f"({delta_pct:+.2f}%)" + ("  ⚡ fresh break" if fresh_break and triggered else "")
    )
    return SignalResult(
        "ma_break",
        triggered=triggered,
        summary=summary,
        detail=f"Confirmation threshold: -{confirmation_pct:.1f}%",
        metrics={
            "close": last_close, "ma": last_ma, "delta_pct": delta_pct,
            "period": period, "fresh_break": fresh_break,
        },
    )


# ---------------------------------------------------------------------------
# Per-position evaluator — runs all four signals
# ---------------------------------------------------------------------------
def evaluate_position(daily: pd.DataFrame,
                      today_open: Optional[float],
                      current_price: Optional[float],
                      cfg_signals: dict) -> List[SignalResult]:
    """
    daily          — completed daily OHLCV bars (today's forming bar excluded)
    today_open     — today's opening print (from intraday feed), None if market closed / no data
    current_price  — latest intraday quote, None if market closed / no data
    """
    if daily is None or daily.empty:
        return [SignalResult("data_missing", False, "no market data")]

    last_completed_bar = daily.iloc[-1]

    return [
        detect_bearish_streak(daily, int(cfg_signals["bearish_streak_length"])),
        detect_intraday_drop(today_open, current_price,
                            float(cfg_signals["daily_drop_pct"])),
        detect_eod_drop(last_completed_bar, float(cfg_signals["daily_drop_pct"])),
        detect_ma_break(daily, int(cfg_signals["ma_period"]),
                        float(cfg_signals.get("ma_confirmation_pct", 1.0))),
    ]
