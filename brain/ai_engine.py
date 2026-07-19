"""Groq-backed AI second opinion on strategy signals."""

import json
import re
from typing import Any, Dict

from groq import Groq

from strategies.base import Signal


class AIEngine:
    """Asks an LLM to sanity-check a trade signal against market context.
    Fails open (approves) on any API or parsing error, since a validation
    outage shouldn't be able to silently block every trade."""

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self.api_key = api_key
        self.model = model
        self._client = Groq(api_key=api_key)

    def validate_signal(self, signal: Signal, symbol: str, market_context: Dict[str, Any]) -> Dict[str, Any]:
        try:
            prompt = self._build_prompt(signal, symbol, market_context)
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=150,
            )
            parsed = self._extract_json(response.choices[0].message.content)
            return {
                "approved": bool(parsed["approved"]),
                "ai_confidence": float(parsed["confidence"]),
                "ai_reason": str(parsed["reason"]),
            }
        except Exception:
            return {"approved": True, "ai_confidence": 0.5, "ai_reason": "API error, defaulting to approve"}

    def get_market_sentiment(self, symbol: str) -> str:
        return "neutral"

    def _build_prompt(self, signal: Signal, symbol: str, market_context: Dict[str, Any]) -> str:
        price = market_context.get("price", "N/A")
        nifty_change = market_context.get("nifty_change_pct")
        if nifty_change is None:
            nifty_str = "Nifty 50 change unknown"
        else:
            direction = "up" if nifty_change >= 0 else "down"
            nifty_str = f"Nifty 50 {direction} {abs(nifty_change):.1f}%"
        sector = market_context.get("sector", "unknown")
        news = market_context.get("news_sentiment", "no recent news")

        return (
            f"You are a trading risk manager. Review this signal: "
            f"{signal.action} {symbol} @ {price}, reason: {signal.reason}, confidence: {signal.confidence:.2f}. "
            f"Market context: {nifty_str}, sector: {sector}, recent news: {news}. "
            'Should we proceed? Respond with JSON: {"approved": bool, "confidence": float 0-1, "reason": str}'
        )

    @staticmethod
    def _extract_json(content: str) -> Dict[str, Any]:
        if not content:
            raise ValueError("Empty AI response")
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in AI response")
        return json.loads(match.group(0))
