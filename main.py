#!/usr/bin/env python
"""CLI entry point: run a backtest, a paper/live session, or the dashboard server."""

import argparse
import asyncio
import logging
import os
import signal as signal_module
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from core.backtest_runner import BacktestRunner
from core.registry import market_registry, strategy_registry

logger = logging.getLogger("trading_agent")

_shutdown_requested = False


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trading agent CLI")
    parser.add_argument("--mode", choices=["backtest", "paper", "live", "dashboard"], help="Run mode (overrides config.yaml default.mode)")
    parser.add_argument("--market", help="Market code (overrides config.yaml default.market)")
    parser.add_argument("--strategy", help="Strategy code (overrides config.yaml default.strategy)")
    parser.add_argument("--symbols", help="Comma-separated symbol list (default: strategy's auto universe)")
    parser.add_argument("--start-date", help="Backtest start date, YYYY-MM-DD (required for --mode backtest)")
    parser.add_argument("--end-date", help="Backtest end date, YYYY-MM-DD (required for --mode backtest)")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration and exit without starting a run")
    return parser.parse_args(argv)


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load config.yaml and merge in secrets from the environment (populated
    from .env if present). Secrets never live in config.yaml or in code."""
    load_dotenv()

    config: Dict[str, Any] = {}
    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        logger.warning("Config file %s not found; using built-in defaults only.", config_path)

    config.setdefault("default", {})
    config.setdefault("dashboard", {})

    config["secrets"] = {
        "groq_api_key": os.environ.get("GROQ_API_KEY"),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    }
    return config


def signal_handler(signum: int, frame: Any) -> None:
    global _shutdown_requested
    logger.info("Received signal %s, shutting down gracefully...", signum)
    _shutdown_requested = True


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    defaults = config["default"]

    _configure_logging(defaults.get("log_level", "INFO"))

    mode = args.mode or defaults.get("mode", "paper")
    market = args.market or defaults.get("market", "nse")
    strategy = args.strategy or defaults.get("strategy", "orb_vwap")
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None

    validation_error = _validate_run(mode, market, strategy, args)
    if validation_error:
        logger.error(validation_error)
        return 1

    if args.dry_run:
        logger.info("Dry run OK: mode=%s market=%s strategy=%s symbols=%s", mode, market, strategy, symbols or "(auto universe)")
        return 0

    signal_module.signal(signal_module.SIGINT, signal_handler)
    signal_module.signal(signal_module.SIGTERM, signal_handler)

    telegram_bot = _build_telegram_bot(config["secrets"], defaults.get("enable_telegram_alerts", False))

    try:
        if mode == "backtest":
            _run_backtest(config, market, strategy, symbols, args)
        elif mode in ("paper", "live"):
            _run_continuous(config, market, strategy, mode, symbols, telegram_bot)
        elif mode == "dashboard":
            _run_dashboard(config)
    except KeyboardInterrupt:
        logger.info("Interrupted by user, shutting down gracefully.")
        return 0
    except Exception as exc:
        logger.error("Fatal error: %s", exc)
        if telegram_bot is not None:
            _notify(telegram_bot.send_error(str(exc)))
        return 1

    return 0


# -- mode handlers -------------------------------------------------------


def _run_backtest(config: Dict[str, Any], market: str, strategy: str, symbols: Optional[List[str]], args: argparse.Namespace) -> None:
    defaults = config["default"]
    runner = BacktestRunner(db_path=defaults.get("db_path", "data/trading_agent.db"))

    resolved_symbols = symbols
    if resolved_symbols is None:
        from core.registry import get_market

        resolved_symbols = get_market(market).get_universe(top_n=defaults.get("universe_size", 10))

    parameters = _strategy_parameters_from_config(defaults)
    run_id = runner.run(strategy, market, resolved_symbols, args.start_date, args.end_date, parameters=parameters)

    results = runner.get_results(run_id) or {}
    logger.info(
        "Backtest complete: run_id=%s | trades=%s | win_rate=%.2f%% | net_pnl=%.2f | sharpe=%s",
        run_id, results.get("total_trades"), results.get("win_rate", 0.0),
        results.get("net_pnl", 0.0), results.get("sharpe_ratio"),
    )


def _run_continuous(
    config: Dict[str, Any], market: str, strategy: str, mode: str, symbols: Optional[List[str]], telegram_bot: Any
) -> None:
    from dashboard.api import RunManager, RunRequest

    defaults = config["default"]
    manager = RunManager(
        db_path=defaults.get("db_path", "data/trading_agent.db"),
        config={"paper_balance": defaults.get("paper_balance", 50000)},
        risk_config=_risk_config_from_defaults(defaults),
    )

    parameters = _strategy_parameters_from_config(defaults)
    request = RunRequest(market=market, strategy=strategy, mode=mode, symbols=symbols, parameters=parameters)
    state = manager.start_run(request)
    logger.info("Started %s run: %s", mode, state["run_id"])
    if telegram_bot is not None:
        _notify(telegram_bot.send_message(f"Started {mode} run: {state['run_id']}"))

    global _shutdown_requested
    try:
        while not _shutdown_requested:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if manager.get_status().get("status") == "running":
            manager.stop_run(state["run_id"])
            logger.info("Run %s stopped.", state["run_id"])
            if telegram_bot is not None:
                _notify(telegram_bot.send_kill_switch(state["run_id"]))


def _run_dashboard(config: Dict[str, Any]) -> None:
    import uvicorn

    dashboard_config = config.get("dashboard", {})
    uvicorn.run(
        "dashboard.api:app",
        host=dashboard_config.get("host", "0.0.0.0"),
        port=dashboard_config.get("port", 8000),
    )


# -- config mapping -------------------------------------------------------


def _strategy_parameters_from_config(defaults: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if "stop_loss_atr_mult" in defaults:
        params["stop_loss_atr_mult"] = defaults["stop_loss_atr_mult"]
    if "take_profit_rr" in defaults:
        params["take_profit_rr"] = defaults["take_profit_rr"]
    if "enable_ai_validation" in defaults:
        params["use_ai_validation"] = defaults["enable_ai_validation"]
    return params


def _risk_config_from_defaults(defaults: Dict[str, Any]) -> Dict[str, Any]:
    """config.yaml stores *_pct fields as percentages (e.g. 1.0 = 1%); the risk
    engines' own config expects fractions (0.01), matching Phase 1's
    .env.example convention (MAX_RISK_PER_TRADE_PCT=1.0)."""
    risk_config: Dict[str, Any] = {}
    if "max_risk_per_trade_pct" in defaults:
        risk_config["max_risk_per_trade_pct"] = defaults["max_risk_per_trade_pct"] / 100
    if "max_daily_loss_pct" in defaults:
        risk_config["max_daily_loss_pct"] = defaults["max_daily_loss_pct"] / 100
    if "max_position_size_pct" in defaults:
        risk_config["max_position_size_pct"] = defaults["max_position_size_pct"] / 100
    if "stop_loss_atr_mult" in defaults:
        risk_config["stop_loss_atr_mult"] = defaults["stop_loss_atr_mult"]
    return risk_config


def _validate_run(mode: str, market: str, strategy: str, args: argparse.Namespace) -> Optional[str]:
    if market not in market_registry.list_available():
        return f"Unknown market: {market!r}. Available: {market_registry.list_available()}"
    if strategy not in strategy_registry.list_available():
        return f"Unknown strategy: {strategy!r}. Available: {strategy_registry.list_available()}"
    if mode == "backtest" and (not args.start_date or not args.end_date):
        return "backtest mode requires --start-date and --end-date"
    return None


def _build_telegram_bot(secrets: Dict[str, Any], enabled: bool) -> Any:
    if not enabled:
        return None
    token, chat_id = secrets.get("telegram_bot_token"), secrets.get("telegram_chat_id")
    if not token or not chat_id:
        logger.warning("enable_telegram_alerts is true but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not set; alerts disabled.")
        return None
    try:
        from alerts.telegram_bot import TelegramBot

        return TelegramBot(token=token, chat_id=chat_id)
    except ValueError as exc:
        logger.warning("Could not initialize Telegram bot: %s", exc)
        return None


_notify_loop: Optional[asyncio.AbstractEventLoop] = None


def _notify(coro: Any) -> None:
    """Fire-and-forget a TelegramBot coroutine from synchronous code; alert
    failures are logged, never allowed to crash the run.

    Reuses one event loop across calls rather than asyncio.run()'s
    create-a-loop-then-close-it-every-time: TelegramBot's underlying HTTP
    client lazily binds internal resources to whichever loop first runs a
    request, so a second asyncio.run() call (a new, different loop) breaks
    with "Event loop is closed" on the very next notification.
    """
    global _notify_loop
    try:
        if _notify_loop is None or _notify_loop.is_closed():
            _notify_loop = asyncio.new_event_loop()
        _notify_loop.run_until_complete(coro)
    except Exception as exc:
        logger.warning("Telegram notification failed: %s", exc)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


if __name__ == "__main__":
    sys.exit(main())
