"""
Portfolio Monitor — main entrypoint.

Reads portfolio.csv (single `ticker` column), pulls daily bars + intraday
snapshot for every ticker via yfinance, evaluates four signals per position,
and dispatches a consolidated GitHub Issues digest if anything triggered.

This monitor is ADVISORY ONLY. It does not place orders.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from portfolio import load_portfolio                    # noqa: E402
from data import fetch_daily_bars, fetch_intraday_snapshot   # noqa: E402
from signals import evaluate_position                   # noqa: E402
from alerts import GitHubIssuesAlerter                  # noqa: E402


def build_logger(log_file: str) -> logging.Logger:
    root = logging.getLogger("monitor")
    if root.handlers:
        return root
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    return root


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(cfg_path: str) -> int:
    cfg = load_config(cfg_path)
    log = build_logger(cfg["alerts"]["log_file"])

    log.info("=" * 70)
    log.info("Portfolio monitor starting — %s", datetime.now(timezone.utc).isoformat())
    log.info("=" * 70)

    positions = load_portfolio(cfg["portfolio"]["csv_path"])
    if not positions:
        log.warning("No valid positions found. Exiting.")
        return 0

    tickers = [p.ticker for p in positions]

    # Historical daily bars (for streak, EOD-drop, MA-break)
    daily_bars = fetch_daily_bars(
        tickers,
        history_days=int(cfg["signals"]["history_days"]),
        max_retries=int(cfg["data"]["max_retries"]),
        backoff=float(cfg["data"]["retry_backoff_seconds"]),
    )

    # Intraday snapshot (for intraday-drop)
    snapshots = fetch_intraday_snapshot(tickers) if cfg["signals"].get("check_intraday", True) else {}

    triggers = []
    healthy = []

    for pos in positions:
        daily = daily_bars.get(pos.ticker)
        today_open, current_price = snapshots.get(pos.ticker, (None, None))

        results = evaluate_position(daily, today_open, current_price, cfg["signals"])
        fired = [r for r in results if r.triggered]

        current = float(daily["close"].iloc[-1]) if daily is not None and not daily.empty else float("nan")
        status = "TRIGGERED" if fired else "ok"
        log.info("  %-10s  %-9s  lastC=%.2f  todayO=%s  now=%s  signals=[%s]",
                 pos.ticker, status, current,
                 f"{today_open:.2f}" if today_open else "  n/a",
                 f"{current_price:.2f}" if current_price else "  n/a",
                 ", ".join(r.signal_key for r in fired) or "-")

        if fired:
            for r in fired:
                triggers.append({
                    "ticker": pos.ticker,
                    "signal_key": r.signal_key,
                    "summary": r.summary,
                    "detail": r.detail,
                    "metrics": r.metrics,
                })
        else:
            healthy.append(pos.ticker)

    log.info("-" * 70)
    log.info("Summary: %d triggered / %d healthy / %d total",
             len({t['ticker'] for t in triggers}), len(healthy), len(positions))

    alerter = GitHubIssuesAlerter(cfg)
    alerter.dispatch(triggers, healthy)
    log.info("Monitor run complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Portfolio Monitor (advisory).")
    parser.add_argument("--config",
                        default=str(Path(__file__).resolve().parents[1] / "config.yaml"))
    args = parser.parse_args()
    try:
        return run(args.config)
    except Exception as exc:
        logging.getLogger("monitor").exception("Fatal: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
