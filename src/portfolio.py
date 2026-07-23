"""
Portfolio CSV loader.

Minimum viable CSV — one column:

    ticker
    AAPL
    MSFT
    0700.HK

Extra columns (notes, sector, whatever) are silently ignored, so you can
enrich the file for your own reference without touching the code.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger("monitor.portfolio")


@dataclass
class Position:
    ticker: str

    @property
    def is_valid(self) -> bool:
        return bool(self.ticker) and not self.ticker.startswith("#")


def load_portfolio(csv_path: str) -> List[Position]:
    """
    Parse the portfolio CSV. Only the `ticker` column is required.
    Blank rows and rows starting with '#' are skipped.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Portfolio CSV not found: {csv_path}")

    positions: List[Position] = []
    seen: set[str] = set()

    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "ticker" not in [c.lower() for c in reader.fieldnames]:
            raise ValueError(
                "Portfolio CSV must have a header row containing a 'ticker' column."
            )

        # Normalise column names to lowercase for lookup
        ticker_col = next(c for c in reader.fieldnames if c.lower() == "ticker")

        for i, row in enumerate(reader, start=2):
            raw = (row.get(ticker_col) or "").strip().upper()
            if not raw or raw.startswith("#"):
                continue
            if raw in seen:
                logger.debug("Row %d duplicate ticker %s — skipped.", i, raw)
                continue
            seen.add(raw)
            positions.append(Position(ticker=raw))

    logger.info("Loaded %d unique tickers from %s", len(positions), csv_path)
    return positions
