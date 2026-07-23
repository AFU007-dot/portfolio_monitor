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
from typing import Dict, List, Optional

import requests

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
                 as_of: Optional[datetime] = None) -> None:
        if not triggers:
            logger.info("No triggered signals — no issue created.")
            return

        if not self.enabled:
            logger.warning("Would have dispatched %d triggers, but alerter disabled.",
                           len(triggers))
            self._log_console(triggers, healthy)
            return

        as_of = as_of or datetime.now(timezone.utc)
        body = self._build_body(triggers, healthy, as_of)
        title = self._build_title(triggers, as_of)

        if self.dedupe:
            existing = self._find_dedupe_target(triggers)
            if existing:
                self._add_comment(existing, body)
                return

        self._create_issue(title, body)

    # ------------------------------------------------------------------
    @staticmethod
    def _build_title(triggers: List[dict], as_of: datetime) -> str:
        tickers_hit = sorted({t["ticker"] for t in triggers})
        return (
            f"Portfolio Alert — {as_of.strftime('%Y-%m-%d %H:%M UTC')} — "
            f"{len(tickers_hit)} position{'s' if len(tickers_hit) != 1 else ''}: "
            f"{', '.join(tickers_hit[:5])}"
            + (" …" if len(tickers_hit) > 5 else "")
        )

    def _build_body(self, triggers: List[dict], healthy: List[str],
                    as_of: datetime) -> str:
        by_ticker: Dict[str, List[dict]] = {}
        for t in triggers:
            by_ticker.setdefault(t["ticker"], []).append(t)

        lines = [
            f"**As of:** {as_of.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            "> ⚠️ **Advisory alert only — no order has been placed.**",
            "> Review each position and manually execute in TWS / IBKR Client Portal.",
            "",
            f"## {len(by_ticker)} position(s) need review",
            "",
        ]

        for ticker in sorted(by_ticker):
            lines.append(f"### {ticker}")
            for sig in by_ticker[ticker]:
                lines.append(f"- **{_signal_label(sig['signal_key'])}** — {sig['summary']}")
                if sig.get("detail"):
                    lines.append("  ```")
                    for row in sig["detail"].splitlines():
                        lines.append(f"  {row}")
                    lines.append("  ```")
            lines.append("")

        if healthy:
            lines.append(f"## ✅ {len(healthy)} healthy position(s)")
            lines.append(", ".join(sorted(healthy)))
            lines.append("")

        signature = {
            "as_of": as_of.isoformat(),
            "triggers": [
                {"ticker": t["ticker"], "signal_key": t["signal_key"]}
                for t in triggers
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
    def _log_console(triggers: List[dict], healthy: List[str]) -> None:
        logger.warning("--- DIGEST (console fallback) ---")
        for t in triggers:
            logger.warning("  [%s] %s — %s", t["ticker"], t["signal_key"], t["summary"])
        if healthy:
            logger.warning("  Healthy: %s", ", ".join(healthy))


def _signal_label(key: str) -> str:
    return {
        "bearish_streak": "🔻 Bearish streak",
        "intraday_drop":  "⚡ Intraday -10% from today's open",
        "eod_drop":       "📉 Daily candle closed -10% from open",
        "ma_break":       "⚠️ Below 50-day moving average",
    }.get(key, key)
