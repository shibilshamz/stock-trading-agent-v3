# Trading Agent

A plugin-based algorithmic trading system for the Indian stock market (NSE), built around one core idea: **the exact same strategy code runs unmodified in backtests, replays, and live/paper trading.** No separate backtest engine with its own copy of the logic — one strategy, four execution modes.

Ships with two strategies (Opening-Range-Breakout + VWAP + Momentum, and a minimal MA Crossover), two NSE data sources (free yfinance and real-time Upstox), a market-aware timeframe selector, ATR-based position sizing, circuit breakers, a FastAPI + vanilla-JS dashboard, Excel reporting, Telegram alerts, and an optional LLM-based signal sanity check.

## Contents

- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Markets, data sources, and timeframes](#markets-data-sources-and-timeframes)
- [Strategies](#strategies)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Dashboard API reference](#dashboard-api-reference)
- [Testing](#testing)
- [Deployment](#deployment)
- [Known limitations and roadmap](#known-limitations-and-roadmap)

## Architecture

Everything is built around four plugin contracts (`core/registry.py` auto-discovers any concrete subclass placed in the right package):

| Contract | Package | What it does |
|---|---|---|
| `MarketAdapter` | `markets/` | Universe selection, OHLCV data, market hours, order execution |
| `StrategyPlugin` | `strategies/` | `on_bar` (entries), `on_position_update` (exits), `on_market_close` (EOD flatten) |
| `DataFeed` | `data_feeds/` | Delivers `Bar` objects to a strategy: live/paper polling, time-paced historical replay, or one-shot batch backtest |
| `RiskEngine` | `risk/` | Approves/rejects/resizes orders before they execute |

**The look-ahead-safety principle:** a strategy's `on_bar` calls `market_adapter.get_ohlcv(...)` to compute its own indicators. In live/paper mode that's genuinely "now." In `historical_replay` and `backtest` modes, the strategy is instead given a point-in-time view that only exposes bars up to the currently-replayed timestamp — so the same strategy code that runs live cannot accidentally see the future when it's being tested.

```
                     ┌─────────────────┐
                     │  strategies/     │   on_bar / on_position_update / on_market_close
                     │  orb_vwap.py     │
                     │  ma_crossover.py │
                     └────────┬─────────┘
                              │ Signal
              ┌───────────────┼────────────────┐
              │               │                │
        risk/position_    risk/circuit_    (approved order)
        sizing.py         breakers.py           │
              └───────────────┴────────────────┘
                              │
              markets/nse_adapter.py  ──►  yfinance
              markets/upstox_adapter.py ──►  Upstox API
                              │
                    data/cache.py (SQLite OHLCV cache, keyed by symbol+timeframe)
```

Four ways a strategy actually runs, all driving the same `Signal`/`Bar` lifecycle:

- **`paper` / `live`** — `data_feeds/paper_feed.py` polls the market adapter on a cron schedule during market hours; orchestrated by `dashboard/api.py`'s `RunManager`. `live` currently behaves identically to `paper` — there's no real broker integration yet (see [Known limitations](#known-limitations-and-roadmap)).
- **`historical_replay`** — `data_feeds/historical_replay.py` replays historical bars to a callback at wall-clock pace scaled by a speed multiplier, in a background thread.
- **`backtest`** — `data_feeds/historical_batch.py` + `core/backtest_runner.py` run a strategy over a date range in one pass and persist metrics/trades/equity curve to SQLite.

Supporting pieces:

- **`dashboard/`** — FastAPI backend (`api.py`) + vanilla HTML/CSS/JS frontend. Owns at most one active run at a time; positions/trades are mirrored into SQLite as they open/close. Static assets are cache-busted on redeploy so a browser never serves a stale UI.
- **`brain/`** — `ai_engine.py` (Groq LLM signal validation, fails open on any error) and `sentiment.py` (Google News RSS + keyword sentiment scoring). Built and tested, but not yet wired into the live decision loop — see limitations.
- **`alerts/`** — `telegram_bot.py`, rate-limited to 1 message/sec, with preformatted signal/trade/EOD/error alerts. Wired into `main.py` for run-lifecycle notifications (started/stopped/error).

## Project structure

```
markets/           MarketAdapter contract + NSE (yfinance) and Upstox adapters + Upstox OAuth
strategies/         StrategyPlugin contract + ORB+VWAP+Momentum and MA Crossover strategies
indicators/          VWAP/ATR/RSI/EMA/Bollinger/ADX/OBV + composite signal scoring
data_feeds/         DataFeed contract: paper polling, historical replay, historical batch
data/                SQLite OHLCV cache (data/cache.py), keyed by symbol + timeframe
risk/                RiskEngine contract: ATR position sizing, circuit breakers
core/                Plugin registry + BacktestRunner
database/           SQLite schema + connection helpers
dashboard/          FastAPI backend, Excel reports, HTML/CSS/JS frontend
brain/                Groq AI signal validation, RSS news sentiment
alerts/              Telegram notifications
scripts/             deploy.sh (VPS systemd deployment)
tests/                pytest unit tests
main.py             CLI entry point
config.yaml          Non-secret runtime configuration
.env.example        Secret template (copy to .env)
```

## Markets, data sources, and timeframes

Two market adapters are registered — both cover NSE, but with different data backends. Pick one via the dashboard's **Market** dropdown or the CLI `--market` flag:

| Market code | Backend | Auth | Notes |
|---|---|---|---|
| `nse` | yfinance | none | Free. Intraday history is capped at the **last ~60 days** (yfinance limit), and 1m only goes back ~7 days — fine for quick checks, limited for intraday backtests. |
| `upstox` | Upstox API | daily OAuth | Real NSE data with **no 60-day limit** — the preferred source for intraday backtests. |

**Timeframe selector.** The candle interval is chosen per run (dashboard **Timeframe** dropdown), and the options are market-aware:

- `upstox`: `1m`, `5m`, `15m`, `30m`, `1h`, `1d`
- `nse`: `1m`, `5m`, `15m`, `1h`, `1d`

Defaults to `15m` when unset. Sub-minute (seconds) candles are **not** available — neither data source provides them; that would require a live tick feed.

**Upstox login.** Upstox access tokens expire daily at 03:30 IST (no refresh token), so connecting is a once-per-trading-day action: open `/api/upstox/login` on the dashboard, authorize, and the token is saved to `data/upstox_token.json`. This requires `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` / `UPSTOX_REDIRECT_URI` in `.env`, and the redirect URI must match the one registered in your Upstox developer app exactly. Because Upstox requires an HTTPS redirect, a deployed instance is typically fronted by a stable HTTPS tunnel/domain (see [Deployment](#deployment)).

## Strategies

| Code | Name | Summary |
|---|---|---|
| `orb_vwap` | ORB + VWAP + Momentum | Composite-score strategy combining opening-range breakout, VWAP position, and EMA/RSI momentum into a single weighted BUY signal, with ATR-based stops/targets. |
| `ma_crossover` | MA Crossover | Minimal fast/slow SMA crossover (default 9/21, configurable). BUY when fast crosses above slow, EXIT when it crosses below. No other filters — deliberately simple, with per-bar MA + crossover logging for manual verification. |

Both are auto-discovered by `core/registry.py`. Adding another is just dropping a new `StrategyPlugin` subclass into `strategies/` — no wiring elsewhere.

## Installation

### Local development

Requires Python 3.10+ (developed against 3.14; 3.12 recommended for parity with the deploy script).

```bash
git clone https://github.com/shibilshamz/stock-trading-agent-v3.git
cd stock-trading-agent-v3
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python database/init_db.py      # creates data/trading_agent.db
cp .env.example .env            # then fill in real values -- see Configuration
```

### VPS (Ubuntu/Debian)

```bash
sudo ./scripts/deploy.sh
```

Installs Python 3.12, Node.js, and SQLite; clones the repo; creates a venv; installs dependencies; initializes the database; creates `.env` from `.env.example`; and sets up a `systemd` service that starts automatically. See [Deployment](#deployment) for details and configuration knobs.

## Configuration

Two separate files, deliberately:

- **`.env`** (git-ignored, never commit this) — secrets only: `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and (for the Upstox market) `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI`. Copy from `.env.example` and fill in real values. `main.py` loads this via `python-dotenv`; nothing in the codebase hardcodes a secret.
- **`config.yaml`** (committed, no secrets) — everything else: market/strategy/mode defaults, risk limits, feature toggles.

```yaml
default:
  market: nse
  strategy: orb_vwap
  mode: paper
  paper_balance: 50000
  max_risk_per_trade_pct: 1.0      # risk engines read this as a fraction (0.01)
  max_daily_loss_pct: 3.0
  stop_loss_atr_mult: 1.5
  take_profit_rr: 2.0
  max_position_size_pct: 10.0
  log_level: INFO
  enable_ai_validation: true
  enable_telegram_alerts: true
  enable_dashboard: true

dashboard:
  host: 0.0.0.0
  port: 8000
  refresh_interval: 30
```

Missing or placeholder credentials don't break anything: `TelegramBot` alerts are skipped with a warning if enabled but unset, `AIEngine` fails open (approves) on any API error, and the `upstox` market simply reports "not connected" until you complete the OAuth login. The free `nse` market needs no credentials at all.

## Usage

### CLI (`main.py`)

```bash
# Backtest a date range
python main.py --mode backtest --market nse --strategy ma_crossover \
  --start-date 2026-06-01 --end-date 2026-07-01

# Backtest specific symbols instead of the auto-selected universe
python main.py --mode backtest --symbols RELIANCE.NS,TCS.NS \
  --start-date 2026-06-01 --end-date 2026-07-01

# Paper trading (continuous; runs until Ctrl+C / SIGTERM)
python main.py --mode paper

# Start the dashboard server
python main.py --mode dashboard
```

All flags override the matching `config.yaml` `default.*` value when given. `--config path/to/other.yaml` points at a different config file. Full flag list: `python main.py --help`. (Per-run timeframe selection is a dashboard feature; the CLI uses the default `15m`.)

### Dashboard

```bash
python main.py --mode dashboard
# or directly:
uvicorn dashboard.api:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. From there: pick **market** (`nse` free / `upstox` real-time), **strategy** (`orb_vwap` / `ma_crossover`), **timeframe**, and **mode**; optionally override symbols/parameters/date range; hit **Run**; watch status/positions/trades update (auto-refreshes every 30s); use **Kill Switch** to stop the active run; download an Excel trade report or browse backtest history from the Reports section. For the `upstox` market, click **Connect Upstox** first to complete the daily OAuth login.

> **Live deployment:** the production instance runs on a VPS behind a **private HTTPS (ngrok) URL** that is intentionally not published here — the dashboard has no authentication, so its address is kept unlisted. Run your own instance locally with the commands above, or deploy your own (see [Deployment](#deployment)).

### Backtesting programmatically

```python
from core.backtest_runner import BacktestRunner

runner = BacktestRunner()
run_id = runner.run(
    "ma_crossover", "upstox", ["RELIANCE.NS", "TCS.NS"],
    "2026-06-01", "2026-07-01", timeframe="15m",
)
results = runner.get_results(run_id)
print(results["total_trades"], results["win_rate"], results["sharpe_ratio"])
runner.generate_report(run_id)  # -> data/reports/<run_id>.xlsx
```

## Dashboard API reference

Base URL: `http://<host>:<port>` (default `0.0.0.0:8000`).

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard UI |
| GET | `/api/markets` | List registered markets — `[{code, name}]` |
| GET | `/api/markets/{code}/timeframes` | Timeframes supported by a market — `["1m", ...]` |
| GET | `/api/strategies` | List registered strategies — `[{code, name}]` |
| GET | `/api/strategies/{code}/parameters` | Default parameters for a strategy |
| GET | `/api/modes` | `["paper", "live", "historical_replay", "backtest"]` |
| GET | `/api/upstox/login` | Begin Upstox OAuth (redirects to Upstox) |
| GET | `/api/upstox/callback` | OAuth redirect target; exchanges the code for a token |
| GET | `/api/upstox/status` | Upstox connection status — `{connected, expires_at}` |
| POST | `/api/run` | Start a run — body: `{market, strategy, mode, timeframe?, parameters?, symbols?, date_range?, replay_speed?}` |
| POST | `/api/stop/{run_id}` | Kill switch — stops the active run |
| GET | `/api/status` | Current run status (or `{"status": "idle"}`) |
| GET | `/api/positions` | Currently open positions |
| GET | `/api/trades` | Closed trades — query: `start_date`, `end_date` |
| GET | `/api/report` | Download an Excel trade report — query: `start_date`, `end_date` |
| GET | `/api/backtest/results` | Backtest history — query: `limit` (default 50) |
| GET | `/api/replay/progress` | Progress of an active `historical_replay` run |

`POST /api/run` returns `409` if a run is already active, `400` for invalid mode/market/strategy, an unsupported timeframe for the chosen market, or missing required fields (e.g. `date_range` for `backtest`/`historical_replay`). `POST /api/stop/{run_id}` returns `404` if `run_id` doesn't match the active run.

## Testing

```bash
pip install -r requirements.txt   # includes pytest
pytest                             # or: pytest -v
```

80 unit tests across `tests/`, covering the NSE and Upstox adapters (network mocked), Upstox OAuth token lifecycle, the ORB+VWAP strategy's signal lifecycle, portfolio persistence (see note below), risk engines (ATR sizing + circuit breakers), historical replay (speed, progress, chronological ordering), and the timeframe selector (per-market lists, validation, and end-to-end plumbing into the feed). Hermetic — no real network calls, no writes to the real database.

> **Note:** `tests/test_portfolio_manager.py` tests `dashboard/api.py`'s `RunManager` — there's no standalone `portfolio_manager.py` module; `RunManager` is where that functionality lives.

## Deployment

`scripts/deploy.sh` targets Ubuntu/Debian and is safe to re-run (pulls latest code, reinstalls dependencies, restarts the service instead of failing on a second run):

```bash
sudo ./scripts/deploy.sh
```

Configurable via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `REPO_URL` | this repo | Git remote to clone |
| `REPO_BRANCH` | `main` | Branch to deploy |
| `INSTALL_DIR` | `/opt/trading-agent` | Install location |
| `SERVICE_USER` | invoking `sudo` user | User the systemd service runs as |
| `SERVICE_NAME` | `trading-agent` | systemd unit name |
| `SERVICE_MODE` | `dashboard` | `--mode` passed to `main.py` in the service |
| `PYTHON_VERSION` | `3.12` | Installed via the deadsnakes PPA if not already present |

After deploying: edit `/opt/trading-agent/.env` with real credentials, then `systemctl restart trading-agent`. Useful commands:

```bash
systemctl status trading-agent
journalctl -u trading-agent -f
systemctl restart trading-agent
```

Routine update of an already-deployed instance (no full re-run needed):

```bash
cd /opt/trading-agent && git pull origin main && systemctl restart trading-agent
```

For the `upstox` market, expose the dashboard over HTTPS (e.g. a reserved ngrok domain pointing at port 8000) so Upstox's OAuth redirect can reach `/api/upstox/callback`, and register that HTTPS callback URL in your Upstox developer app. Since the dashboard has no authentication, keep its public URL private.

## Known limitations and roadmap

Being direct about what's built vs. what's still a stub, so nobody is surprised in production:

- **No dashboard authentication.** Anyone who can reach the dashboard URL can start/stop runs and trigger the Upstox login. Keep the deployed URL private, or put it behind an auth proxy before exposing it.
- **No real broker integration.** `live` mode currently executes through the same paper-fill simulation as `paper` mode (current price ± small random slippage). Wiring a real broker API is the biggest remaining gap before this could place real orders.
- **AI validation and news sentiment aren't wired into the live decision loop.** `brain/ai_engine.py` and `brain/sentiment.py` are fully built and tested standalone, but `RunManager`'s bar-processing loop doesn't call them yet.
- **Per-trade Telegram alerts aren't wired in.** `TelegramBot` is used for run lifecycle events in `main.py`, but not yet on individual signals/fills (those methods exist and are tested but need a call site).
- **Single active run at a time.** `RunManager` doesn't support multiple concurrent strategies/markets.
- **`historical_replay` isn't exposed via the CLI** (`main.py --mode` accepts `backtest`/`paper`/`live`/`dashboard`), though it's fully implemented and reachable via the dashboard API.

### Contributing

The plugin contracts in `markets/base.py`, `strategies/base.py`, `data_feeds/base.py`, and `risk/base.py` are the extension points. A new concrete subclass placed in the matching package is auto-registered by `core/registry.py` — no wiring required elsewhere. Run `pytest` before submitting changes.
