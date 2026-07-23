"""Unit tests for pure signal-detection logic."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from signals import (  # noqa: E402
    detect_bearish_streak,
    detect_intraday_drop,
    detect_eod_drop,
    detect_ma_break,
    evaluate_position,
)


def _mk_daily(opens, closes, highs=None, lows=None, volumes=None):
    n = len(opens)
    idx = pd.date_range("2026-01-05", periods=n, freq="B")
    return pd.DataFrame({
        "open": opens,
        "high": highs or [max(o, c) + 1 for o, c in zip(opens, closes)],
        "low":  lows  or [min(o, c) - 1 for o, c in zip(opens, closes)],
        "close": closes,
        "volume": volumes or [1_000_000] * n,
    }, index=idx)


# ---------- Bearish streak ----------
def test_bearish_streak_triggers():
    df = _mk_daily([100, 101, 102], [99, 100, 101])
    r = detect_bearish_streak(df, required=3)
    assert r.triggered
    assert r.metrics["streak_length"] == 3


def test_bearish_streak_broken():
    df = _mk_daily([100, 101, 102], [99, 102, 101])
    r = detect_bearish_streak(df, required=3)
    assert not r.triggered


# ---------- Intraday drop (current vs today's open) ----------
def test_intraday_drop_exact_threshold():
    r = detect_intraday_drop(today_open=200.0, current_price=180.0, threshold_pct=10.0)
    assert r.triggered
    assert round(r.metrics["drop_pct"], 2) == 10.0


def test_intraday_drop_below_threshold():
    r = detect_intraday_drop(today_open=200.0, current_price=185.0, threshold_pct=10.0)
    assert not r.triggered


def test_intraday_drop_handles_missing_quote():
    r = detect_intraday_drop(today_open=None, current_price=None, threshold_pct=10.0)
    assert not r.triggered
    assert "no live intraday quote" in r.summary


# ---------- EOD drop (today's close vs today's open) ----------
def test_eod_drop_triggers():
    bar = pd.Series({"open": 200.0, "close": 179.0})
    r = detect_eod_drop(bar, threshold_pct=10.0)
    assert r.triggered
    assert r.metrics["drop_pct"] > 10.0


def test_eod_drop_no_trigger():
    bar = pd.Series({"open": 200.0, "close": 195.0})
    r = detect_eod_drop(bar, threshold_pct=10.0)
    assert not r.triggered


def test_eod_drop_handles_missing_data():
    r = detect_eod_drop(None, threshold_pct=10.0)
    assert not r.triggered


# ---------- 50-DMA break ----------
def test_ma_break_returns_period():
    opens = list(range(100, 160)) + [150]
    closes = list(range(101, 161)) + [130]
    df = _mk_daily(opens, closes)
    r = detect_ma_break(df, period=50, confirmation_pct=1.0)
    assert r.metrics["period"] == 50
    assert "50-DMA" in r.summary


def test_ma_break_no_trigger_when_above():
    opens = list(range(100, 160))
    closes = [o + 2 for o in opens]
    df = _mk_daily(opens, closes)
    r = detect_ma_break(df, period=50, confirmation_pct=1.0)
    assert not r.triggered


# ---------- Position evaluator ----------
def test_evaluate_position_returns_four_signals():
    df = _mk_daily([100, 101, 102, 103], [99, 100, 101, 102])
    cfg = {"bearish_streak_length": 3, "daily_drop_pct": 10.0,
           "ma_period": 50, "ma_confirmation_pct": 1.0}
    results = evaluate_position(df, today_open=100.0, current_price=88.0,
                                cfg_signals=cfg)
    keys = {r.signal_key for r in results}
    assert keys == {"bearish_streak", "intraday_drop", "eod_drop", "ma_break"}
    # intraday_drop should fire (12% drop)
    assert next(r for r in results if r.signal_key == "intraday_drop").triggered
