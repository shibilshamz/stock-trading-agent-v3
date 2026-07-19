# Trading Agent

A plugin-based algorithmic trading system for the Indian stock market (NSE), built around one core idea: **the exact same strategy code runs unmodified in backtests, replays, and live/paper trading.** No separate backtest engine with its own copy of the logic — one strategy, four execution modes.

Ships with an Opening-Range-Breakout + VWAP + Momentum strategy, ATR-based position sizing, circuit breakers, a FastAPI + vanilla-JS dashboard, Excel reporting, Telegram alerts, and an optional LLM-based signal sanity check.

## Contents

- [Architecture](#architecture)
- [Project structure](#project-structure)
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
                     └────────┬─────────┘
                              │ Signal
              ┌───────────────┼────────────────┐
              │               │                │
        risk/position_    risk/circuit_    (approved order)
        sizing.py         breakers.py           │
              └───────────────┴────────────────┘
                              │
                    markets/nse_adapter.py  ──►  yfinance
                              │
                    data/cache.py (SQLite OHLCV cache)
```

Four ways a strategy actually runs, all driving the same `Signal`/`Bar` lifecycle:

- **`paper` / `live`** — `data_feeds/paper_feed.py` polls the market adapter on a cron schedule (every 15 min during market hours); orchestrated by `dashboard/api.py`'s `RunManager`. `live` currently behaves identically to `paper` — there's no real broker integration yet (see [Known limitations](#known-limitations-and-roadmap)).
- **`historical_replay`** — `data_feeds/historical_replay.py` replays historical bars to a callback at wall-clock pace scaled by a speed multiplier, in a background thread.
- **`backtest`** — `data_feeds/historical_batch.py` + `core/backtest_runner.py` run a strategy over a date range in one pass and persist metrics/trades/equity curve to SQLite.

Supporting pieces:

- **`dashboard/`** — FastAPI backend (`api.py`) + vanilla HTML/CSS/JS frontend. Owns at most one active run at a time; positions/trades are mirrored into SQLite as they open/close.
- **`brain/`** — `ai_engine.py` (Groq LLM signal validation, fails open on any error) and `sentiment.py` (Google News RSS + keyword sentiment scoring). Built and tested, but not yet wired into the live decision loop — see limitations.
- **`alerts/`** — `telegram_bot.py`, rate-limited to 1 message/sec, with preformatted signal/trade/EOD/error alerts. Wired into `main.py` for run-lifecycle notifications (started/stopped/error).

## Project structure

```
markets/           MarketAdapter contract + NSE adapter (yfinance-backed)
strategies/         StrategyPlugin contract + ORB+VWAP+Momentum strategy
indicators/          VWAP/ATR/RSI/EMA/Bollinger/ADX/OBV + composite signal scoring
data_feeds/         DataFeed contract: paper polling, historical replay, historical batch
data/                SQLite OHLCV cache (data/cache.py)
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

- **`.env`** (git-ignored, never commit this) — secrets only: `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Copy from `.env.example` and fill in real values. `main.py` loads this via `python-dotenv`; nothing in the codebase hardcodes a secret.
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

`.env.example` also documents a few additional variables (`DB_PATH`, `PAPER_BALANCE`, `MAX_RISK_PER_TRADE_PCT`, etc.) that mirror `config.yaml`'s settings for reference — currently only the three secrets above are actually read from the environment by `main.py`; `config.yaml` is authoritative for everything else. If you want those env vars to override `config.yaml`, that's a small, well-scoped addition to `main.py`'s `load_config()`.

Missing or placeholder Telegram/Groq credentials don't break anything: `TelegramBot` alerts are skipped with a warning if `enable_telegram_alerts` is on but credentials aren't set, and `AIEngine` fails open (approves) on any API error.

## Usage

### CLI (`main.py`)

```bash
# Backtest a date range
python main.py --mode backtest --market nse --strategy orb_vwap \
  --start-date 2026-06-01 --end-date 2026-07-01

# Backtest specific symbols instead of the auto-selected universe
python main.py --mode backtest --symbols RELIANCE.NS,TCS.NS \
  --start-date 2026-06-01 --end-date 2026-07-01

# Paper trading (continuous; runs until Ctrl+C / SIGTERM)
python main.py --mode paper

# Validate config and exit without starting anything
python main.py --mode backtest --start-date 2026-06-01 --end-date 2026-07-01 --dry-run

# Start the dashboard server
python main.py --mode dashboard
```

All flags override the matching `config.yaml` `default.*` value when given; otherwise the config value is used. `--config path/to/other.yaml` points at a different config file. Full flag list: `python main.py --help`.

`paper`/`live` mode blocks in the foreground and shuts down gracefully on `SIGINT`/`SIGTERM` — the active run is stopped and finalized in the database before the process exits, not just killed.

### Dashboard

```bash
python main.py --mode dashboard
# or directly:
uvicorn dashboard.api:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. From there: pick market/strategy/mode, optionally override symbols/parameters/date range, hit **Run**; watch status/positions/trades update (auto-refreshes every 30s); use **Kill Switch** to stop the active run; download an Excel trade report or browse backtest history from the Reports section.

### Backtesting programmatically

```python
from core.backtest_runner import BacktestRunner

runner = BacktestRunner()
run_id = runner.run("orb_vwap", "nse", ["RELIANCE.NS", "TCS.NS"], "2026-06-01", "2026-07-01")
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
| GET | `/api/strategies` | List registered strategies — `[{code, name}]` |
| GET | `/api/modes` | `["paper", "live", "historical_replay", "backtest"]` |
| GET | `/api/strategies/{code}/parameters` | Default parameters for a strategy |
| POST | `/api/run` | Start a run — body: `{market, strategy, mode, parameters?, symbols?, date_range?, replay_speed?}` |
| POST | `/api/stop/{run_id}` | Kill switch — stops the active run |
| GET | `/api/status` | Current run status (or `{"status": "idle"}`) |
| GET | `/api/positions` | Currently open positions |
| GET | `/api/trades` | Closed trades — query: `start_date`, `end_date` |
| GET | `/api/report` | Download an Excel trade report — query: `start_date`, `end_date` |
| GET | `/api/backtest/results` | Backtest history — query: `limit` (default 50) |
| GET | `/api/replay/progress` | Progress of an active `historical_replay` run |

`POST /api/run` returns `409` if a run is already active, `400` for invalid mode/market/strategy or missing required fields (e.g. `date_range` for `backtest`/`historical_replay`). `POST /api/stop/{run_id}` returns `404` if `run_id` doesn't match the active run.

## Testing

```bash
pip install -r requirements.txt   # includes pytest
pytest                             # or: pytest -v
```

37 unit tests across `tests/`, covering the NSE adapter (yfinance mocked), the ORB+VWAP strategy's signal lifecycle, portfolio persistence (open/close/positions — see note below), risk engines (ATR sizing + circuit breakers), and historical replay (speed, progress, chronological bar ordering across symbols). All hermetic — no real network calls, no writes to the real database, ~2.5s total runtime.

> **Note:** `tests/test_portfolio_manager.py` tests `dashboard/api.py`'s `RunManager` — there's no standalone `portfolio_manager.py` module in this codebase; `RunManager` is where that functionality actually lives.

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
| `SERVICE_MODE` | `dashboard` | `--mode` passed to `main.py` in the service (`paper`/`live` for a continuous trading service instead) |
| `PYTHON_VERSION` | `3.12` | Installed via the deadsnakes PPA if not already present |

After deploying: edit `/opt/trading-agent/.env` with real credentials, then `systemctl restart trading-agent`. Useful commands:

```bash
systemctl status trading-agent
journalctl -u trading-agent -f
systemctl restart trading-agent
```

## Known limitations and roadmap

Being direct about what's built vs. what's still a stub, so nobody is surprised in production:

- **No real broker integration.** `live` mode currently executes through the same paper-fill simulation as `paper` mode (current price ± small random slippage). Wiring a real broker API is the biggest remaining gap before this could place real orders.
- **AI validation and news sentiment aren't wired into the live decision loop.** `brain/ai_engine.py` and `brain/sentiment.py` are fully built and tested standalone, but `RunManager`'s bar-processing loop doesn't call them yet — `enable_ai_validation` in `config.yaml` currently only toggles the strategy's own `use_ai_validation` flag (a placeholder that always approves), not a real `AIEngine.validate_signal` call.
- **Per-trade Telegram alerts aren't wired in.** `TelegramBot` is used for run lifecycle events (started/stopped/error) in `main.py`, but not yet called on individual signals/fills — `send_signal`/`send_trade_opened`/`send_trade_closed`/`send_eod_summary` exist and are tested but need a call site in `RunManager`.
- **Single active run at a time.** `RunManager` doesn't support running multiple strategies or markets concurrently.
- **`historical_replay` isn't exposed via the CLI** (`main.py --mode` only accepts `backtest`/`paper`/`live`/`dashboard`), though it's fully implemented and reachable via the dashboard API.
- **One strategy today** (ORB + VWAP + Momentum). The plugin architecture supports more — drop a new `StrategyPlugin` subclass in `strategies/` and it's auto-discovered.

### Contributing

The plugin contracts in `markets/base.py`, `strategies/base.py`, `data_feeds/base.py`, and `risk/base.py` are the extension points. A new concrete subclass placed in the matching package is auto-registered by `core/registry.py` — no wiring required elsewhere. Run `pytest` before submitting changes.
