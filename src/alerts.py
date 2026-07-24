"""
GitHub Issues alert dispatcher.

For each run, produces at most ONE consolidated digest:
    - If any position has a triggered signal, opens a new Issue titled
      "Portfolio Alert — YYYY-MM-DD HH:MM UTC — N positions" with a detailed body.
    - Deduplication: if `dedupe_by_signal` is enabled, we scan currently OPEN
      issues with our label; when the same (ticker, signal_key) pair is already
      represented in an open issue's body, we add a comment there instead of
      opening a new issue.
    - When no signals fire, no issue is created (silent successful run).

Auth: uses GITHUB_TOKEN provided by GitHub Actions automatically.
      For local testing, set env var GITHUB_TOKEN + GITHUB_REPOSITORY.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from fibonacci import format_fib_context_lines, FibContext

logger = logging.getLogger("monitor.alerts")

GITHUB_API = "https://api.github.com"


class GitHubIssuesAlerter:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("alerts", {}).get("github_issues", {})
        self.token = os.environ.get("GITHUB_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.enabled = bool(self.cfg.get("enabled") and self.token and self.repo)
        self.labels = self.cfg.get("labels", ["portfolio-alert"])
        self.dedupe = bool(self.cfg.get("dedupe_by_signal", True))

        if not self.enabled:
            logger.warning(
                "GitHub Issues alerter disabled (enabled=%s, token=%s, repo=%s)",
                self.cfg.get("enabled"), bool(self.token), bool(self.repo),
            )

    # ------------------------------------------------------------------
    def dispatch(self, triggers: List[dict], healthy: List[str],
                 as_of: Optional[datetime] = None,
                 index_triggers: Optional[List[dict]] = None,
                 index_healthy: Optional[List[str]] = None,
                 fib_by_ticker: Optional[Dict[str, Any]] = None) -> None:
        index_triggers = index_triggers or []
        index_healthy = index_healthy or []
        fib_by_ticker = fib_by_ticker or {}

        if not triggers and not index_triggers:
            logger.info("No triggered signals — no issue created.")
            return

        if not self.enabled:
            logger.warning("Would have dispatched %d portfolio + %d index triggers, but alerter disabled.",
                           len(triggers), len(index_triggers))
            self._log_console(triggers, healthy, index_triggers, index_healthy)
            return

        as_of = as_of or datetime.now(timezone.utc)
        body = self._build_body(triggers, healthy, as_of,
                                 index_triggers, index_healthy,
                                 fib_by_ticker)
        title = self._build_title(triggers, as_of, index_triggers)

        if self.dedupe:
            existing = self._find_dedupe_target(triggers + index_triggers)
            if existing:
                self._add_comment(existing, body)
                return

        self._create_issue(title, body)

    # ------------------------------------------------------------------
    @staticmethod
    def _build_title(triggers: List[dict], as_of: datetime,
                     index_triggers: Optional[List[dict]] = None) -> str:
        index_triggers = index_triggers or []
        all_tickers = sorted({t["ticker"] for t in triggers + index_triggers})
        n_pos = len({t["ticker"] for t in triggers})
        n_idx = len({t["ticker"] for t in index_triggers})

        parts = []
        if n_pos:
            parts.append(f"{n_pos} position{'s' if n_pos != 1 else ''}")
        if n_idx:
            parts.append(f"{n_idx} index/ETF")

        return (
            f"Portfolio Alert — {as_of.strftime('%Y-%m-%d %H:%M UTC')} — "
            f"{' + '.join(parts)}: "
            f"{', '.join(all_tickers[:5])}"
            + (" …" if len(all_tickers) > 5 else "")
        )

    def _build_body(self, triggers: List[dict], healthy: List[str],
                    as_of: datetime,
                    index_triggers: Optional[List[dict]] = None,
                    index_healthy: Optional[List[str]] = None,
                    fib_by_ticker: Optional[Dict[str, Any]] = None) -> str:
        index_triggers = index_triggers or []
        index_healthy = index_healthy or []
        fib_by_ticker = fib_by_ticker or {}

        lines = [
            f"**As of:** {as_of.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            "> ⚠️ **Advisory alert only — no order has been placed.**",
            "> Review each position and manually execute in TWS / IBKR Client Portal.",
            "",
        ]

        # ---- Index / market-wide section (always first for macro context) ----
        if index_triggers:
            by_idx: Dict[str, List[dict]] = {}
            for t in index_triggers:
                by_idx.setdefault(t["ticker"], []).append(t)
            lines.append(f"## 🌐 Market indices — {len(by_idx)} triggered")
            lines.append("")
            for ticker in sorted(by_idx):
                first = by_idx[ticker][0]
                label = first.get("label") or ticker
                header = f"{ticker}" if label == ticker else f"{ticker} — {label}"
                lines.append(f"### {header}")
                for sig in by_idx[ticker]:
                    lines.append(f"- **{_signal_label(sig['signal_key'])}** — {sig['summary']}")
                    if sig.get("detail"):
                        lines.append("  ```")
                        for row in sig["detail"].splitlines():
                            lines.append(f"  {row}")
                        lines.append("  ```")
                lines.extend(_render_fib_block(fib_by_ticker.get(ticker)))
                lines.append("")

        # ---- Portfolio positions section ----
        by_ticker: Dict[str, List[dict]] = {}
        for t in triggers:
            by_ticker.setdefault(t["ticker"], []).append(t)

        if by_ticker:
            lines.append(f"## 💼 Portfolio positions — {len(by_ticker)} triggered")
            lines.append("")
            for ticker in sorted(by_ticker):
                lines.append(f"### {ticker}")
                for sig in by_ticker[ticker]:
                    lines.append(f"- **{_signal_label(sig['signal_key'])}** — {sig['summary']}")
                    if sig.get("detail"):
                        lines.append("  ```")
                        for row in sig["detail"].splitlines():
                            lines.append(f"  {row}")
                        lines.append("  ```")
                lines.extend(_render_fib_block(fib_by_ticker.get(ticker)))
                lines.append("")

        # ---- "At a Fib level" bonus section (healthy tickers within proximity) ----
        triggered_tickers = set(by_ticker.keys()) | {t["ticker"] for t in index_triggers}
        at_fib_hits = []
        for tk, ctx in (fib_by_ticker or {}).items():
            if tk in triggered_tickers:
                continue  # already reported in main sections
            if ctx is None or not getattr(ctx, "ok", False):
                continue
            if getattr(ctx, "at_level", None) is not None:
                at_fib_hits.append((tk, ctx))
        if at_fib_hits:
            lines.append(f"## 🎯 At a Fib level — {len(at_fib_hits)} healthy ticker(s) near a retracement")
            lines.append("")
            for tk, ctx in sorted(at_fib_hits):
                lvl = ctx.at_level
                dist = ctx.at_level_distance_pct or 0.0
                direction = "below" if dist > 0 else "above"
                lines.append(
                    f"- **{tk}** — ${ctx.current_price:.2f} is {abs(dist):.2f}% {direction} "
                    f"the {lvl.pct:.1f}% Fib @ ${lvl.price:.2f} "
                    f"({ctx.trend}, 52wk range ${ctx.swing_low:.2f}–${ctx.swing_high:.2f})"
                )
            lines.append("")

        # ---- Healthy section — full Fib context per ticker ----
        # Index healthy list may contain labels in "SYMBOL (Label)" format; extract just symbols
        def _extract_symbol(entry: str) -> str:
            return entry.split(" (", 1)[0]

        healthy_symbols = sorted(healthy)
        healthy_idx_symbols = [_extract_symbol(e) for e in sorted(index_healthy)]

        if healthy_symbols or healthy_idx_symbols:
            lines.append(
                f"## ✅ Healthy — {len(healthy_symbols)} position(s), "
                f"{len(healthy_idx_symbols)} index/ETF"
            )
            lines.append("")

            # Healthy indices first (macro context before individual holdings)
            for tk in healthy_idx_symbols:
                ctx = fib_by_ticker.get(tk)
                cp_str = f" — ${ctx.current_price:.2f}" if ctx and ctx.ok else ""
                lines.append(f"### {tk}{cp_str}")
                lines.extend(_render_fib_block(ctx))
                lines.append("")

            # Healthy portfolio positions
            for tk in healthy_symbols:
                ctx = fib_by_ticker.get(tk)
                cp_str = f" — ${ctx.current_price:.2f}" if ctx and ctx.ok else ""
                lines.append(f"### {tk}{cp_str}")
                lines.extend(_render_fib_block(ctx))
                lines.append("")

        signature = {
            "as_of": as_of.isoformat(),
            "triggers": [
                {"ticker": t["ticker"], "signal_key": t["signal_key"]}
                for t in (triggers + index_triggers)
            ],
        }
        lines.append("<!-- monitor-signature: " + json.dumps(signature) + " -->")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _create_issue(self, title: str, body: str) -> None:
        url = f"{GITHUB_API}/repos/{self.repo}/issues"
        r = requests.post(url, headers=self._headers(),
                          json={"title": title, "body": body, "labels": self.labels},
                          timeout=20)
        if r.status_code >= 300:
            logger.error("Create issue failed [%s]: %s", r.status_code, r.text[:300])
            return
        logger.info("Opened issue #%s", r.json().get("number"))

    def _add_comment(self, issue_number: int, body: str) -> None:
        url = f"{GITHUB_API}/repos/{self.repo}/issues/{issue_number}/comments"
        r = requests.post(url, headers=self._headers(),
                          json={"body": body}, timeout=20)
        if r.status_code >= 300:
            logger.error("Comment on #%s failed [%s]: %s",
                         issue_number, r.status_code, r.text[:300])
            return
        logger.info("Added comment to existing issue #%s (dedupe hit).", issue_number)

    def _find_dedupe_target(self, triggers: List[dict]) -> Optional[int]:
        url = f"{GITHUB_API}/repos/{self.repo}/issues"
        params = {"state": "open", "labels": ",".join(self.labels), "per_page": 20}
        try:
            r = requests.get(url, headers=self._headers(), params=params, timeout=20)
            r.raise_for_status()
            open_issues = r.json()
        except Exception as exc:
            logger.warning("Dedupe lookup failed (%s); will create new issue.", exc)
            return None

        current_sig = {(t["ticker"], t["signal_key"]) for t in triggers}
        for issue in open_issues:
            body = issue.get("body") or ""
            marker = "<!-- monitor-signature:"
            if marker not in body:
                continue
            try:
                json_part = body.split(marker, 1)[1].split("-->", 1)[0].strip()
                sig = json.loads(json_part)
                existing = {(t["ticker"], t["signal_key"]) for t in sig.get("triggers", [])}
            except Exception:
                continue
            if current_sig.issubset(existing):
                return int(issue["number"])
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _log_console(triggers: List[dict], healthy: List[str],
                     index_triggers: Optional[List[dict]] = None,
                     index_healthy: Optional[List[str]] = None) -> None:
        index_triggers = index_triggers or []
        index_healthy = index_healthy or []
        logger.warning("--- DIGEST (console fallback) ---")
        for t in index_triggers:
            logger.warning("  [IDX %s] %s — %s", t["ticker"], t["signal_key"], t["summary"])
        for t in triggers:
            logger.warning("  [%s] %s — %s", t["ticker"], t["signal_key"], t["summary"])
        if healthy:
            logger.warning("  Healthy positions: %s", ", ".join(healthy))
        if index_healthy:
            logger.warning("  Healthy indices:   %s", ", ".join(index_healthy))


def _render_fib_block(ctx) -> List[str]:
    """Render Fibonacci context under a triggered ticker, or an empty list if none."""
    if ctx is None:
        return []
    if not isinstance(ctx, FibContext):
        return []
    return format_fib_context_lines(ctx)


def _signal_label(key: str) -> str:
    return {
        "bearish_streak": "🔻 Bearish streak (2 red candles, lower closes)",
        "intraday_drop":  "⚡ Intraday drop from today's open",
        "eod_drop":       "📉 Daily candle closed below today's open",
        "ma_break":       "⚠️ Below 50-day moving average",
        "ma_surge":       "🚀 Surge above 50-day moving average",
    }.get(key, key)
