"""Lightweight news sentiment: Google News RSS + keyword-based scoring."""

from typing import Any, Dict, List
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import feedparser


class NewsSentiment:
    """Fetches headlines per query from a Google-News-style RSS endpoint and
    scores them with simple positive/negative keyword counting."""

    POSITIVE_WORDS = ["surge", "rally", "gain", "profit", "growth", "bull", "up", "rise"]
    NEGATIVE_WORDS = ["crash", "fall", "loss", "bear", "down", "decline", "sell-off", "plunge"]

    def __init__(self, rss_url: str = "https://news.google.com/rss/search?q=NSE+stock+market"):
        self.rss_url = rss_url

    def get_headlines(self, query: str, max_items: int = 5) -> List[Dict[str, str]]:
        feed = feedparser.parse(self._build_query_url(query))
        return [
            {
                "title": entry.get("title", ""),
                "published": entry.get("published", ""),
                "source": self._extract_source(entry),
            }
            for entry in feed.entries[:max_items]
        ]

    def analyze_sentiment(self, headlines: List[str]) -> Dict[str, Any]:
        positive_count = 0
        negative_count = 0
        keywords_found: List[str] = []

        for headline in headlines:
            lower = headline.lower()
            for word in self.POSITIVE_WORDS:
                if word in lower:
                    positive_count += 1
                    keywords_found.append(word)
            for word in self.NEGATIVE_WORDS:
                if word in lower:
                    negative_count += 1
                    keywords_found.append(word)

        total = positive_count + negative_count
        score = (positive_count - negative_count) / total if total > 0 else 0.0

        if score > 0:
            label = "positive"
        elif score < 0:
            label = "negative"
        else:
            label = "neutral"

        return {"score": round(score, 4), "label": label, "keywords_found": keywords_found}

    def get_symbol_sentiment(self, symbol: str) -> Dict[str, Any]:
        headlines = self.get_headlines(symbol)
        titles = [h["title"] for h in headlines]
        sentiment = self.analyze_sentiment(titles)
        return {"symbol": symbol, "headline_count": len(headlines), **sentiment}

    def _build_query_url(self, query: str) -> str:
        parsed = urlparse(self.rss_url)
        params = parse_qs(parsed.query)
        params["q"] = [query]
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    @staticmethod
    def _extract_source(entry: Any) -> str:
        source = entry.get("source")
        if source is None:
            return ""
        if hasattr(source, "get"):
            return source.get("title", "") or ""
        return str(source)
