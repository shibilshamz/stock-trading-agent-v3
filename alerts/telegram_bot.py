"""Telegram alerting: rate-limited notifications for signals, trades, and errors."""

import asyncio
import re
import time
from typing import Any, Dict, Optional

from telegram import Bot

from strategies.base import Signal


class TelegramBot:
    """Thin wrapper around python-telegram-bot's async Bot, with a fixed
    max-1-message-per-second rate limit and pre-formatted trading alerts."""

    MIN_INTERVAL_SECONDS = 1.0
    _TOKEN_PATTERN = re.compile(r"^\d+:[\w-]+$")

    def __init__(self, token: str, chat_id: str):
        if not token or not self._TOKEN_PATTERN.match(token):
            raise ValueError("Invalid Telegram bot token")
        if not chat_id:
            raise ValueError("chat_id is required")

        self.token = token
        self.chat_id = chat_id
        self._bot = Bot(token=token)
        self._last_sent = 0.0
        self._lock = asyncio.Lock()

    async def send_message(self, text: str) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last_sent
            if elapsed < self.MIN_INTERVAL_SECONDS:
                await asyncio.sleep(self.MIN_INTERVAL_SECONDS - elapsed)
            await self._bot.send_message(chat_id=self.chat_id, text=text)
            self._last_sent = time.monotonic()

    async def send_signal(self, signal: Signal, price: float) -> None:
        emoji = "🟢" if signal.action == "BUY" else "🔴"
        sl = f"{signal.suggested_stop:.2f}" if signal.suggested_stop is not None else "N/A"
        tp = f"{signal.suggested_target:.2f}" if signal.suggested_target is not None else "N/A"
        text = (
            f"{emoji} {signal.action} {signal.symbol} @ {price:.2f} | "
            f"Confidence: {signal.confidence:.2f} | SL: {sl} | TP: {tp}"
        )
        await self.send_message(text)

    async def send_trade_opened(self, trade: Dict[str, Any]) -> None:
        side = trade.get("side", "BUY")
        text = (
            f"📈 OPENED: {side} {trade['symbol']} {trade['quantity']} @ {trade['entry_price']:.2f}"
        )
        await self.send_message(text)

    async def send_trade_closed(self, trade: Dict[str, Any]) -> None:
        side = trade.get("side", "SELL")
        pnl_str = self._format_pnl(trade["pnl"])
        text = (
            f"📉 CLOSED: {side} {trade['symbol']} {trade['quantity']} "
            f"@ {trade['exit_price']:.2f} | P&L: {pnl_str}"
        )
        await self.send_message(text)

    async def send_eod_summary(self, summary: Dict[str, Any]) -> None:
        pnl_str = self._format_pnl(summary["pnl"])
        text = (
            f"📊 EOD Summary | Trades: {summary['trades']} | "
            f"Win Rate: {summary['win_rate']:.0f}% | P&L: {pnl_str}"
        )
        await self.send_message(text)

    async def send_error(self, error: str) -> None:
        await self.send_message(f"🔴 ERROR: {error}")

    async def send_kill_switch(self, run_id: str) -> None:
        await self.send_message(f"🛑 Kill Switch activated for run {run_id}")

    @staticmethod
    def _format_pnl(value: float) -> str:
        sign = "+" if value >= 0 else "-"
        return f"{sign}₹{abs(value):.0f}"
