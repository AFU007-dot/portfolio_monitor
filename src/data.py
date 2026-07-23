"""
Market-data layer using yfinance.

Provides two things per ticker:
    - Historical daily OHLCV (completed bars only, today excluded)
    - Today's intraday snapshot: (today_open, current_price) — for the
      intraday-drop signal. Both None if the market is closed or no data.

Note on the free-tier delay:
    yfinance intraday quotes are 15-20 min delayed for US markets, more for
    some international exchanges. The intraday-drop signal will therefore
    fire 15-20 min after the level is actually breached. That's the price
    of the "zero infrastructure" architecture.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("monitor.data")


# ---------------------------------------------------------------------------
# Historical daily bars
# ---------------------------------------------------------------------------
def fetch_daily_bars(tickers: List[str], history_days: int,
                     max_retries: int = 3, backoff: float = 2.0) -> Dict[str, pd.DataFrame]:
    """
    Returns {ticker: DataFrame} with columns [open, high, low, close, volume],
    date-indexed, ascending, completed bars only.
    """
    import yfinance as yf

    if not tickers:
        return {}

    period_days = max(history_days + 10, 60)
    start = (datetime.now(timezone.utc) - timedelta(days=period_days * 1.6)).strftime("%Y-%m-%d")

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Downloading daily bars for %d tickers (attempt %d)...",
                        len(tickers), attempt)
            raw = yf.download(
                tickers=" ".join(tickers),
                start=start,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
            return _split_multiticker(raw, tickers)
        except Exception as exc:
            last_err = exc
            logger.warning("yfinance daily fetch failed (attempt %d/%d): %s",
                           attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(backoff * attempt)

    logger.error("yfinance daily fetch permanently failed: %s", last_err)
    return {}


def _split_multiticker(raw: pd.DataFrame, tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """Normalise yfinance's MultiIndex response to per-ticker frames."""
    out: Dict[str, pd.DataFrame] = {}
    today = pd.Timestamp(datetime.now(timezone.utc).date())

    for t in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            elif isinstance(raw.columns, pd.MultiIndex):
                if t not in raw.columns.get_level_values(0):
                    logger.warning("No daily data returned for %s", t)
                    continue
                df = raw[t].copy()
            else:
                continue

            df.columns = [c.lower() for c in df.columns]
            required = {"open", "high", "low", "close", "volume"}
            if not required.issubset(df.columns):
                logger.warning("%s missing OHLCV columns; got %s", t, list(df.columns))
                continue

            df = df.dropna(subset=["open", "close"]).sort_index()
            df = df[df.index < today]  # completed bars only
            if df.empty:
                logger.warning("%s: no completed bars after filtering", t)
                continue
            out[t] = df
        except Exception as exc:
            logger.warning("Failed to normalise %s: %s", t, exc)

    logger.info("Normalised bars for %d/%d tickers", len(out), len(tickers))
    return out


# ---------------------------------------------------------------------------
# Intraday snapshot
# ---------------------------------------------------------------------------
def fetch_intraday_snapshot(tickers: List[str]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """
    Returns {ticker: (today_open, current_price)}.
    Either value may be None if that ticker's market is closed or yfinance
    returned no intraday data.

    Uses 1-day / 5-minute bars — the smallest interval yfinance offers on
    the free tier without extra flags.
    """
    import yfinance as yf

    out: Dict[str, Tuple[Optional[float], Optional[float]]] = {t: (None, None) for t in tickers}
    if not tickers:
        return out

    try:
        raw = yf.download(
            tickers=" ".join(tickers),
            period="1d",
            interval="5m",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.warning("yfinance intraday fetch failed: %s", exc)
        return out

    if raw is None or raw.empty:
        logger.info("No intraday data available (markets likely all closed).")
        return out

    for t in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            elif isinstance(raw.columns, pd.MultiIndex):
                if t not in raw.columns.get_level_values(0):
                    continue
                df = raw[t].copy()
            else:
                continue

            df.columns = [c.lower() for c in df.columns]
            df = df.dropna(subset=["open", "close"])
            if df.empty:
                continue

            today_open = float(df["open"].iloc[0])
            current_price = float(df["close"].iloc[-1])
            out[t] = (today_open, current_price)
        except Exception as exc:
            logger.debug("Intraday snapshot for %s failed: %s", t, exc)

    populated = sum(1 for v in out.values() if v[0] is not None)
    logger.info("Intraday snapshots populated for %d/%d tickers", populated, len(tickers))
    return out
