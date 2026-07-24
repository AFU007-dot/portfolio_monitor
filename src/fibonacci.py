"""
Fibonacci retracement context computation.

Given a ticker's daily bars and the current price, this module identifies
the 52-week swing high and swing low, auto-detects trend direction, and
returns the nearest Fib level above and below the current price.

Purely informational — never triggers alerts on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

import pandas as pd


@dataclass
class FibLevel:
    """A single Fibonacci retracement level."""
    pct: float           # e.g. 23.6, 50.0, 61.8
    price: float         # absolute price at this level


@dataclass
class FibContext:
    """Full Fibonacci retracement context for a single ticker."""
    ok: bool                                    # False if insufficient history
    reason: str = ""                            # why ok=False, if applicable
    trend: str = ""                             # "uptrend" | "downtrend"
    swing_high: float = float("nan")
    swing_high_date: str = ""
    swing_low: float = float("nan")
    swing_low_date: str = ""
    lookback_days: int = 0
    current_price: float = float("nan")
    levels: List[FibLevel] = field(default_factory=list)
    nearest_above: Optional[FibLevel] = None
    nearest_below: Optional[FibLevel] = None
    pct_to_above: Optional[float] = None        # +% distance to nearest level above
    pct_to_below: Optional[float] = None        # -% distance to nearest level below
    at_level: Optional[FibLevel] = None         # if current is within proximity_pct of any level
    at_level_distance_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "reason": self.reason,
            "trend": self.trend,
            "swing_high": self.swing_high, "swing_high_date": self.swing_high_date,
            "swing_low": self.swing_low, "swing_low_date": self.swing_low_date,
            "lookback_days": self.lookback_days,
            "current_price": self.current_price,
            "levels": [{"pct": l.pct, "price": l.price} for l in self.levels],
            "nearest_above": None if self.nearest_above is None else
                {"pct": self.nearest_above.pct, "price": self.nearest_above.price},
            "nearest_below": None if self.nearest_below is None else
                {"pct": self.nearest_below.pct, "price": self.nearest_below.price},
            "pct_to_above": self.pct_to_above,
            "pct_to_below": self.pct_to_below,
            "at_level": None if self.at_level is None else
                {"pct": self.at_level.pct, "price": self.at_level.price},
            "at_level_distance_pct": self.at_level_distance_pct,
        }


def compute_fib_context(
    daily: pd.DataFrame,
    current_price: Optional[float],
    lookback_trading_days: int = 252,
    levels_pct: Optional[List[float]] = None,
    at_level_proximity_pct: float = 0.5,
) -> FibContext:
    """
    Compute Fibonacci retracement context for a ticker.

    Uses the highest high and lowest low over the last N trading days
    (default 252 = ~52 weeks). Auto-detects trend based on which pivot
    occurred later:
      - If swing high came AFTER swing low  -> uptrend
        (levels drawn from high downward; retracement pulls back from rally)
      - If swing low came AFTER swing high  -> downtrend
        (levels drawn from low upward; retracement bounces off decline)

    `current_price` should be the live/latest known price. If missing,
    falls back to the last close in `daily`.
    """
    if levels_pct is None:
        levels_pct = [0.0, 23.6, 38.2, 50.0, 61.8, 78.6, 100.0]

    if daily is None or daily.empty:
        return FibContext(ok=False, reason="no daily bars")

    # Take the last N trading days (or the full history if shorter)
    window = daily.tail(lookback_trading_days)
    if len(window) < 20:
        return FibContext(ok=False,
                          reason=f"insufficient history ({len(window)} bars, need ≥20)")

    # 52-week high/low anchored on completed daily bars
    high_idx = window["high"].idxmax()
    low_idx = window["low"].idxmin()
    swing_high = float(window.loc[high_idx, "high"])
    swing_low = float(window.loc[low_idx, "low"])

    if swing_high <= swing_low:
        return FibContext(ok=False, reason="swing high <= swing low (flat/degenerate range)")

    # Auto-detect trend
    trend = "uptrend" if high_idx >= low_idx else "downtrend"

    # Build level list
    price_range = swing_high - swing_low
    levels: List[FibLevel] = []
    for pct in sorted(levels_pct):
        # For uptrend: retracement measured DOWN from swing high
        #   0%   = swing_high
        #   100% = swing_low
        # For downtrend: retracement measured UP from swing low
        #   0%   = swing_low
        #   100% = swing_high
        if trend == "uptrend":
            price = swing_high - price_range * (pct / 100.0)
        else:  # downtrend
            price = swing_low + price_range * (pct / 100.0)
        levels.append(FibLevel(pct=pct, price=price))

    # Fall back to last close if no live price
    if current_price is None or (isinstance(current_price, float) and pd.isna(current_price)):
        current_price = float(daily["close"].iloc[-1])
    current_price = float(current_price)

    # Find nearest level above and below (based on absolute price, not %)
    above_candidates = [l for l in levels if l.price > current_price]
    below_candidates = [l for l in levels if l.price < current_price]

    # Sort by absolute price distance so "nearest" is unambiguous
    nearest_above = min(above_candidates, key=lambda l: l.price - current_price) \
        if above_candidates else None
    nearest_below = max(below_candidates, key=lambda l: l.price) \
        if below_candidates else None

    pct_to_above = (nearest_above.price - current_price) / current_price * 100.0 \
        if nearest_above else None
    pct_to_below = (nearest_below.price - current_price) / current_price * 100.0 \
        if nearest_below else None  # will be negative

    # "At a Fib level" check — within proximity_pct of any level
    at_level: Optional[FibLevel] = None
    at_level_distance_pct: Optional[float] = None
    for lvl in levels:
        dist_pct = abs(lvl.price - current_price) / current_price * 100.0
        if dist_pct <= at_level_proximity_pct:
            if at_level is None or dist_pct < abs(at_level_distance_pct or 0):
                at_level = lvl
                # Signed distance: positive means current is BELOW the level, negative means ABOVE
                at_level_distance_pct = (lvl.price - current_price) / current_price * 100.0

    return FibContext(
        ok=True,
        trend=trend,
        swing_high=swing_high,
        swing_high_date=str(high_idx)[:10],
        swing_low=swing_low,
        swing_low_date=str(low_idx)[:10],
        lookback_days=len(window),
        current_price=current_price,
        levels=levels,
        nearest_above=nearest_above,
        nearest_below=nearest_below,
        pct_to_above=pct_to_above,
        pct_to_below=pct_to_below,
        at_level=at_level,
        at_level_distance_pct=at_level_distance_pct,
    )


def format_fib_context_lines(ctx: FibContext) -> List[str]:
    """Render the Fib context as markdown lines suitable for a GitHub Issue body."""
    if not ctx.ok:
        return [f"  📐 Fib context unavailable — {ctx.reason}"]

    header = (
        f"  📐 Fib context (52wk, {ctx.trend}, "
        f"H ${ctx.swing_high:.2f} on {ctx.swing_high_date} → "
        f"L ${ctx.swing_low:.2f} on {ctx.swing_low_date}):"
    )
    lines = [header]

    if ctx.nearest_above is not None:
        lines.append(
            f"     Above: {ctx.nearest_above.pct:>5.1f}% @ ${ctx.nearest_above.price:.2f} "
            f"({ctx.pct_to_above:+.2f}% away)"
        )
    else:
        lines.append("     Above: (price is above 100% level — no upside Fib in range)")

    if ctx.nearest_below is not None:
        lines.append(
            f"     Below: {ctx.nearest_below.pct:>5.1f}% @ ${ctx.nearest_below.price:.2f} "
            f"({ctx.pct_to_below:+.2f}% away)"
        )
    else:
        lines.append("     Below: (price is below 0% level — no downside Fib in range)")

    return lines
