# Portfolio Monitor

Zero-infrastructure portfolio monitor that runs on **GitHub Actions**, watches
your holdings against four signals, and opens a **GitHub Issue** when any
position needs review.

- Advisory only — never places orders (IBKR-compliant workflow)
- Free market data via **yfinance** — US, HK (`.HK`), Japan (`.T`), Europe (`.L` / `.PA` / `.DE`)
- **Minimum-viable CSV** — just a single `ticker` column
- Consolidated digest alerts with deduplication (no spam)
- Full audit trail: `signals.log` is committed back to the repo on every run

## The CSV — just tickers

```csv
ticker
AAPL
MSFT
NVDA
0700.HK
7203.T
SPY
```

That's it. Extra columns are silently ignored, so you can add `notes`,
`sector`, `entry_date` etc. for your own reference — the code doesn't care.

## Signals

| Signal | Default trigger | What it catches |
|---|---|---|
| **Bearish streak** | 3 consecutive daily closes < opens | Slow bleed |
| **Intraday -10% from today's open** | current price ≥ 10 % below today's open, during market hours | Sharp intraday moves (15–20 min delayed) |
| **EOD -10% from today's open** | today's close ≥ 10 % below today's open | Confirmed crash days |
| **Break below 50-day MA** | close ≥ 1 % below 50-DMA | Trend break |

All thresholds are global and configurable in `config.yaml`.

> ⚠️ **Note on the "intraday" signal.** yfinance quotes on the free tier are
> 15–20 min delayed for US markets, sometimes more for international. So the
> intraday alert fires 15–20 min after the level is actually breached. True
> real-time requires a persistent connection to a broker feed (IBKR etc.) on
> a machine that's always on — which is the trade-off for zero infrastructure.

## Repo layout

```
portfolio_monitor/
├── .github/workflows/monitor.yml   ← GitHub Actions schedule
├── src/
│   ├── monitor.py                  ← entrypoint
│   ├── portfolio.py                ← CSV loader (ticker column only)
│   ├── data.py                     ← yfinance daily + intraday fetcher
│   ├── signals.py                  ← 4 pure signal functions
│   └── alerts.py                   ← GitHub Issues dispatcher
├── tests/test_signals.py           ← 11 unit tests
├── portfolio.csv                   ← YOU EDIT THIS — just tickers
├── config.yaml                     ← thresholds & cron behaviour
├── requirements.txt
└── signals.log                     ← auto-committed audit log
```

## Setup (5 minutes)

### 1. Create a new GitHub repo

Create an **empty private repo** (e.g. `portfolio-monitor`). Private is
recommended so your holdings list stays confidential.

### 2. Push these files

```bash
cd portfolio_monitor
git init
git add .
git commit -m "Initial portfolio monitor"
git branch -M main
git remote add origin git@github.com:<your-user>/portfolio-monitor.git
git push -u origin main
```

### 3. Enable Actions write permissions

`Repo → Settings → Actions → General → Workflow permissions`
→ ✅ **Read and write permissions**

### 4. Edit `portfolio.csv`

Just tickers, one per line. Yahoo Finance ticker conventions:

| Market | Example |
|---|---|
| US | `AAPL`, `SPY`, `MSFT` |
| Hong Kong | `0700.HK`, `9988.HK` |
| Japan | `7203.T`, `6758.T` |
| London | `HSBA.L` |
| Paris | `MC.PA` |
| Frankfurt | `SAP.DE` |

Commit and push — the next scheduled run picks it up.

### 5. Watch for alerts

- **Issues tab** — every triggered digest opens a labeled `portfolio-alert` issue
- **Mobile** — install the GitHub mobile app → notifications on for the repo
- **Email** — GitHub emails you automatically for issues you're subscribed to

## Schedule

Default cron (all UTC):

| Trigger | UTC | Local (HKT) | Purpose |
|---|---|---|---|
| JP intraday + close | 06:30 | 14:30 | Intraday drop during JP session, then EOD sweep after close |
| HK intraday + close | 08:30 | 16:30 | Intraday drop during HK session, then EOD sweep after close |
| US midday pulse | 15:00 | 23:00 | Intraday drop check |
| US afternoon pulse | 18:00 | 02:00 | Intraday drop check |
| US post-close sweep | 21:00 | 05:00 | Evaluates completed US daily candle |

Mon–Fri only. Costs ~200 min/month of GitHub's free 2,000-min tier.

To catch big intraday moves faster, add more cron lines in
`.github/workflows/monitor.yml` — every 15 min during market hours is fine
within the free tier if you want it.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Without GITHUB_TOKEN → alerts fall back to console log
python src/monitor.py --config config.yaml

# With a personal access token → creates real issues
export GITHUB_TOKEN=ghp_xxx
export GITHUB_REPOSITORY=your-user/portfolio-monitor
python src/monitor.py --config config.yaml
```

## Testing

```bash
python -m pytest tests/ -v
```

11 tests cover all four signals plus the integrator.

## Deduplication

If the same `(ticker, signal_key)` pair is already in an OPEN `portfolio-alert`
issue, the next triggered run **comments on that issue** instead of opening a
new one. Close the issue when you've acted — the next occurrence opens a fresh
one.

## Compliance reminder

Every alert body includes:

> ⚠️ **Advisory alert only — no order has been placed.**
> Review each position and manually execute in TWS / IBKR Client Portal.

The workflow has zero broker credentials and cannot place orders.

## Extending

- **More signals** — add a function to `src/signals.py`, wire it into
  `evaluate_position()`, add a label in `alerts.py::_signal_label()`. Done.
- **Different thresholds** — edit `config.yaml`, push, no code changes needed.
- **Slack/Discord** — add a channel class to `alerts.py`, no other changes.
