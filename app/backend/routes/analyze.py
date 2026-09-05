import asyncio
import os
from datetime import datetime

import requests
from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
    id: str
    name: str
    epithet: str = ""
    philosophy: str


class AnalyzeRequest(BaseModel):
    ticker: str
    model_name: str | None = None
    model_provider: str | None = None
    analysts: list[str] | None = None  # builtin keys; None = all
    custom_analysts: list[CustomPersona] | None = None


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
    model_name = request_data.model_name or os.environ.get("MODEL_NAME", "deepseek/deepseek-chat-v3.1")
    model_provider = request_data.model_provider or os.environ.get("MODEL_PROVIDER", "OpenRouter")

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

        def progress_handler(agent_name, ticker_, status, analysis, timestamp):
            event = ProgressUpdateEvent(agent=agent_name, ticker=ticker_, status=status, timestamp=timestamp, analysis=analysis)
            progress_queue.put_nowait(event)

        progress.register_handler(progress_handler)

        try:
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

            yield StartEvent().to_sse()

            while not run_task.done():
                if disconnect_task.done():
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    return
                try:
                    event = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                    yield event.to_sse()
                except asyncio.TimeoutError:
                    pass

            try:
                result = await run_task
            except asyncio.CancelledError:
                return
            except Exception as e:
                yield ErrorEvent(message=f"Analysis failed: {e}").to_sse()
                return

            if not result or not result.get("messages"):
                yield ErrorEvent(message="Failed to generate analysis").to_sse()
                return

            usage_after = await asyncio.to_thread(_openrouter_usage)
            run_cost = None
            if usage_before and usage_after and usage_before.get("used") is not None and usage_after.get("used") is not None:
                run_cost = max(0.0, usage_after["used"] - usage_before["used"])

            note_data = {
                "ticker": ticker,
                "start_date": start_date,
                "end_date": end_date,
                "model_name": model_name,
                "run_cost": run_cost,
                "usage": usage_after,
                "decisions": parse_hedge_fund_response(result.get("messages", [])[-1].content),
                "analyst_signals": result.get("data", {}).get("analyst_signals", {}),
            }
            try:
                db = SessionLocal()
                try:
                    note_data["note_id"] = save_note(db, note_data)
                finally:
                    db.close()
            except Exception as e:
                print(f"Failed to persist note: {e}")

            yield CompleteEvent(data=note_data).to_sse()

        except asyncio.CancelledError:
            return
        finally:
            progress.unregister_handler(progress_handler)
            if run_task and not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except asyncio.CancelledError:
                    pass
            if disconnect_task and not disconnect_task.done():
                disconnect_task.cancel()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
