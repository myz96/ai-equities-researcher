import asyncio
import time
from datetime import date, timedelta

from fastapi import APIRouter, Query

from src.tools import yfinance_api

router = APIRouter(prefix="/market", tags=["market"])

# In-process cache: quotes and price series change slowly enough for a desk.
_cache: dict[str, tuple[float, object]] = {}
_TTL_SECONDS = 300


def _cached(key: str, build):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    value = build()
    _cache[key] = (now, value)
    return value


def _quote(ticker: str) -> dict | None:
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=45)).isoformat()
    prices = yfinance_api.get_prices(ticker, start, end)
    if len(prices) < 2:
        return None
    closes = [p.close for p in prices][-31:]
    last, prev = closes[-1], closes[-2]
    return {
        "ticker": ticker,
        "last": last,
        "change_pct": (last - prev) / prev * 100 if prev else 0.0,
        "spark": closes,
    }


@router.get("/quotes")
async def quotes(tickers: str = Query(..., description="Comma-separated tickers")):
    """Last price, day change, and a 30-day close series per ticker."""
    wanted = [t.strip().upper() for t in tickers.split(",") if t.strip()][:20]

    def build_one(ticker):
        return _cached(f"q_{ticker}", lambda: _quote(ticker))

    results = await asyncio.gather(*(asyncio.to_thread(build_one, t) for t in wanted))
    return {"quotes": [r for r in results if r]}


@router.get("/prices")
async def prices(ticker: str, months: int = 3):
    """Daily closes for the report chart."""
    ticker = ticker.strip().upper()
    months = max(1, min(months, 24))

    def build():
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=months * 31)).isoformat()
        series = yfinance_api.get_prices(ticker, start, end)
        return {
            "ticker": ticker,
            "prices": [{"date": p.time, "close": p.close} for p in series],
        }

    return await asyncio.to_thread(lambda: _cached(f"p_{ticker}_{months}", build))
