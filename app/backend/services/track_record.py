"""Grade every committee member's past calls against actual price moves.

Rules (v1):
- A call is a member's signal on a note (bullish / bearish; neutral abstains).
- Final grade after the 30-day horizon: bullish hits if the return beats +2%,
  bearish hits if it beats -2% downward. Inside the band, the market is flat
  and the call is a miss for both sides.
- Younger calls stay "open" and carry a live aligned return (bearish calls
  flip the sign), so the desk shows how a call is doing before it is graded.
- The committee's own decision (buy/short/...) is graded the same way under
  the key "committee".
"""

import time
from datetime import date, datetime, timedelta

from app.backend.database.models import DeskNote
from src.tools import yfinance_api

HORIZON_DAYS = 30
FLAT_BAND = 0.02
EXCLUDED = ("risk_management", "debate_room", "portfolio_manager")

_cache: dict = {"at": 0.0, "data": None}
_TTL_SECONDS = 1800


def invalidate_cache():
    _cache["data"] = None


def _price_series(ticker: str, start: str) -> list:
    return yfinance_api.get_prices(ticker, start, date.today().isoformat())


def _close_on_or_before(prices: list, day: date) -> float | None:
    best = None
    for p in prices:
        if date.fromisoformat(p.time) <= day:
            best = p.close
        else:
            break
    return best


def _signal_calls(note_data: dict, ticker: str):
    """Yield (member_key, direction) for one note. direction: +1 bull, -1 bear."""
    for agent_key, by_ticker in (note_data.get("analyst_signals") or {}).items():
        if agent_key.startswith(EXCLUDED):
            continue
        entry = (by_ticker or {}).get(ticker) or {}
        signal = str(entry.get("signal", "")).lower()
        if signal == "bullish":
            yield agent_key.removesuffix("_agent"), 1
        elif signal == "bearish":
            yield agent_key.removesuffix("_agent"), -1

    decision = (note_data.get("decisions") or {}).get(ticker) or {}
    action = str(decision.get("action", "")).lower()
    if action in ("buy", "cover"):
        yield "committee", 1
    elif action in ("sell", "short"):
        yield "committee", -1


def compute_track_records(db) -> dict:
    """Per-member records across all notes. Cached; call from a thread."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < _TTL_SECONDS:
        return _cache["data"]

    notes = db.query(DeskNote).order_by(DeskNote.created_at).all()
    series_cache: dict[str, list] = {}
    records: dict[str, dict] = {}

    for note in notes:
        ticker = note.ticker
        note_day = (note.created_at or datetime.utcnow()).date()
        if ticker not in series_cache:
            start = (min(note_day, date.today()) - timedelta(days=HORIZON_DAYS + 20)).isoformat()
            series_cache[ticker] = _price_series(ticker, start)
        prices = series_cache[ticker]
        if not prices:
            continue

        entry_price = _close_on_or_before(prices, note_day)
        if not entry_price:
            continue
        horizon_day = note_day + timedelta(days=HORIZON_DAYS)
        graded = date.today() >= horizon_day
        exit_price = _close_on_or_before(prices, horizon_day) if graded else prices[-1].close
        if not exit_price:
            continue
        ret = (exit_price - entry_price) / entry_price

        for member, direction in _signal_calls(note.data or {}, ticker):
            rec = records.setdefault(member, {
                "calls": 0, "graded": 0, "hits": 0, "open": 0, "open_aligned_return": 0.0,
            })
            rec["calls"] += 1
            if graded:
                rec["graded"] += 1
                hit = ret > FLAT_BAND if direction > 0 else ret < -FLAT_BAND
                rec["hits"] += 1 if hit else 0
            else:
                rec["open"] += 1
                rec["open_aligned_return"] += direction * ret

    for rec in records.values():
        rec["hit_rate"] = round(rec["hits"] / rec["graded"], 3) if rec["graded"] else None
        rec["open_aligned_return"] = round(
            rec["open_aligned_return"] / rec["open"], 4) if rec["open"] else None

    _cache["at"] = now
    _cache["data"] = records
    return records


def records_for_prompt(records: dict) -> dict:
    """Compact per-member strings for the LLM prompts. Only proven members."""
    out = {}
    for member, rec in records.items():
        if rec["graded"]:
            out[member] = f"{rec['hits']}/{rec['graded']} calls right over {HORIZON_DAYS}d"
    return out
