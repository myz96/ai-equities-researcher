import asyncio
import os
import re
from datetime import datetime

import requests
from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.backend.database.connection import SessionLocal
from app.backend.models.events import CompleteEvent, ErrorEvent, ProgressUpdateEvent, StartEvent
from app.backend.routes.desk import save_note
from app.backend.services.graph import parse_hedge_fund_response, run_graph_async
from app.backend.services.portfolio import create_portfolio
from src.llm.models import AVAILABLE_MODELS
from src.main import create_workflow
from src.utils.analysts import ANALYST_CONFIG
from src.utils.progress import progress

router = APIRouter(prefix="/analyze", tags=["analyze"])

# One session at a time: keeps SSE progress streams, the shared OpenRouter
# usage delta (per-run cost), and the free dyno honest.
_run_lock = asyncio.Lock()


def _openrouter_usage() -> dict | None:
    """Spend on the OpenRouter key, in USD: {"used": float, "limit": float|None}."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        response = requests.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5,
        )
        if response.status_code != 200:
            return None
        data = response.json().get("data", {})
        return {"used": data.get("usage"), "limit": data.get("limit")}
    except Exception:
        return None


@router.get("/models")
def list_models():
    """Models the report UI offers, plus the configured default."""
    return {
        "default": os.environ.get("MODEL_NAME", "deepseek/deepseek-chat-v3.1"),
        "models": [
            {
                "model_name": m.model_name,
                "display_name": m.display_name.replace("[OpenRouter] ", ""),
            }
            for m in AVAILABLE_MODELS
            if m.provider.value == "OpenRouter"
        ],
    }


@router.get("/usage")
async def usage():
    return await asyncio.to_thread(_openrouter_usage) or {"used": None, "limit": None}


class CustomPersona(BaseModel):
    id: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=80)
    epithet: str = Field(default="", max_length=120)
    philosophy: str = Field(min_length=1, max_length=6000)


class AnalyzeRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=12)
    model_name: str | None = None
    model_provider: str | None = None
    analysts: list[str] | None = None  # builtin keys; None = all
    custom_analysts: list[CustomPersona] | None = Field(default=None, max_length=10)


@router.get("/analysts")
def list_analysts():
    """Metadata for every analyst, used by the report UI to render cards."""
    return [
        {
            "key": key,
            "display_name": config["display_name"],
            "description": config["description"],
            "investing_style": config["investing_style"],
            "type": config["type"],
            "order": config["order"],
        }
        for key, config in ANALYST_CONFIG.items()
    ]


@router.post("/run")
async def analyze(request_data: AnalyzeRequest, request: Request):
    """Run the full committee (all analysts + debate room) on one ticker.

    Streams SSE progress events, then a complete event with all signals.
    """
    ticker = request_data.ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", ticker) or ticker.startswith("^"):
        raise HTTPException(400, "That does not look like an equity ticker.")

    model_name = request_data.model_name or os.environ.get("MODEL_NAME", "deepseek/deepseek-chat-v3.1")
    model_provider = request_data.model_provider or os.environ.get("MODEL_PROVIDER", "OpenRouter")
    allowed_models = {m.model_name for m in AVAILABLE_MODELS}
    if model_name not in allowed_models:
        raise HTTPException(400, "Unknown model.")

    # Spend ceiling: refuse new sessions when the key is nearly exhausted.
    usage_now = await asyncio.to_thread(_openrouter_usage)
    if usage_now and usage_now.get("limit") is not None and usage_now.get("used") is not None:
        if usage_now["used"] >= usage_now["limit"] - 0.50:
            raise HTTPException(402, "The committee's budget is exhausted.")

    if _run_lock.locked():
        raise HTTPException(429, "A session is already in progress. One at a time.")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - relativedelta(months=3)).strftime("%Y-%m-%d")

    selected = None
    if request_data.analysts is not None:
        selected = [k for k in request_data.analysts if k in ANALYST_CONFIG]
    customs = [p.model_dump() for p in (request_data.custom_analysts or [])][:10]
    for p in customs:
        p["id"] = "".join(c for c in p["id"] if c.isalnum() or c == "_")[:48] or "member"
    if selected == [] and not customs:
        selected = None  # never run an empty committee

    portfolio = create_portfolio(100000.0, 0.0, [ticker])
    graph = create_workflow(selected, custom_personas=customs).compile()

    async def wait_for_disconnect():
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return True

    async def event_generator():
        progress_queue = asyncio.Queue()
        run_task = None
        disconnect_task = None
        owns_lock = False
        handed_off = False

        def progress_handler(agent_name, ticker_, status, analysis, timestamp):
            event = ProgressUpdateEvent(agent=agent_name, ticker=ticker_, status=status, timestamp=timestamp, analysis=analysis)
            progress_queue.put_nowait(event)

        # Check-and-acquire with no await in between: atomic within one event-
        # loop step, so two racing generators cannot both start a paid run.
        if _run_lock.locked():
            yield ErrorEvent(message="A session is already in progress. One at a time.").to_sse()
            return
        await _run_lock.acquire()
        owns_lock = True
        progress.register_handler(progress_handler)

        try:
            # First byte immediately — proxies time out on silent streams.
            yield StartEvent().to_sse()

            usage_before = await asyncio.to_thread(_openrouter_usage)

            def load_track_records():
                from app.backend.services.track_record import compute_track_records, records_for_prompt
                db = SessionLocal()
                try:
                    return records_for_prompt(compute_track_records(db))
                except Exception:
                    return {}
                finally:
                    db.close()

            track_records = await asyncio.to_thread(load_track_records)

            run_task = asyncio.create_task(
                run_graph_async(
                    graph=graph,
                    portfolio=portfolio,
                    tickers=[ticker],
                    start_date=start_date,
                    end_date=end_date,
                    model_name=model_name,
                    model_provider=model_provider,
                    extra_data={"track_records": track_records},
                )
            )
            disconnect_task = asyncio.create_task(wait_for_disconnect())

            def build_note_data(result, usage_after, run_cost):
                return {
                    "ticker": ticker,
                    "start_date": start_date,
                    "end_date": end_date,
                    "model_name": model_name,
                    "run_cost": run_cost,
                    "usage": usage_after,
                    "custom_roster": {f"custom_{p['id']}": {"name": p["name"], "epithet": p.get("epithet", "")}
                                      for p in customs},
                    "decisions": parse_hedge_fund_response(result.get("messages", [])[-1].content),
                    "analyst_signals": result.get("data", {}).get("analyst_signals", {}),
                }

            def hand_off_orphan():
                # The executor thread cannot be cancelled: it keeps calling the
                # LLM after a disconnect. Keep the lock and the progress handler
                # until it truly ends — otherwise the zombie leaks its events
                # and its spend into the next session — and save the note, so
                # the money buys something even when nobody is watching.
                nonlocal handed_off
                handed_off = True

                async def finish():
                    try:
                        result = await run_task
                        if result and result.get("messages"):
                            usage_after = await asyncio.to_thread(_openrouter_usage)
                            run_cost = None
                            if usage_before and usage_after and usage_before.get("used") is not None and usage_after.get("used") is not None:
                                run_cost = max(0.0, usage_after["used"] - usage_before["used"])
                            db = SessionLocal()
                            try:
                                save_note(db, build_note_data(result, usage_after, run_cost))
                            finally:
                                db.close()
                            print(f"Orphaned session for {ticker} finished and saved.")
                    except Exception as e:
                        print(f"Orphaned session for {ticker} failed: {e!r}")
                    finally:
                        _run_lock.release()
                        progress.unregister_handler(progress_handler)

                asyncio.create_task(finish())

            quiet_ticks = 0
            while not run_task.done():
                if disconnect_task.done():
                    hand_off_orphan()
                    return
                try:
                    event = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                    quiet_ticks = 0
                    yield event.to_sse()
                except asyncio.TimeoutError:
                    quiet_ticks += 1
                    if quiet_ticks >= 10:
                        # SSE comment heartbeat: keeps proxies from timing out
                        # during long LLM calls (Cloudflare cuts idle streams).
                        quiet_ticks = 0
                        yield ": keepalive\n\n"

            try:
                result = await run_task
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"Analysis run failed: {e!r}")
                yield ErrorEvent(message=f"Analysis failed ({type(e).__name__}). Try again.").to_sse()
                return

            if not result or not result.get("messages"):
                yield ErrorEvent(message="Failed to generate analysis").to_sse()
                return

            usage_after = await asyncio.to_thread(_openrouter_usage)
            run_cost = None
            if usage_before and usage_after and usage_before.get("used") is not None and usage_after.get("used") is not None:
                run_cost = max(0.0, usage_after["used"] - usage_before["used"])

            note_data = build_note_data(result, usage_after, run_cost)
            try:
                db = SessionLocal()
                try:
                    note_data["note_id"] = save_note(db, note_data)
                finally:
                    db.close()
            except Exception as e:
                print(f"Failed to persist note: {e!r}")
                note_data["persist_error"] = True

            yield CompleteEvent(data=note_data).to_sse()

        except asyncio.CancelledError:
            # Starlette killed the generator mid-yield (client vanished): the
            # run itself must still finish, spend accounted, note saved.
            if run_task is not None and not run_task.done():
                hand_off_orphan()
            return
        finally:
            if owns_lock and not handed_off:
                _run_lock.release()
                progress.unregister_handler(progress_handler)
            if disconnect_task and not disconnect_task.done():
                disconnect_task.cancel()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
