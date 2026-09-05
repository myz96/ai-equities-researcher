import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.backend.database.connection import get_db
from app.backend.database.models import DeskNote, DeskPersona, DeskState

router = APIRouter(prefix="/desk", tags=["desk"])

DEFAULT_WATCHLIST = ["AAPL", "NVDA", "MSFT", "BHP.AX"]


# ---------- state: watchlist + bench ----------

def _get_state(db: Session, key: str, default):
    row = db.query(DeskState).get(key)
    return row.value if row else default


def _put_state(db: Session, key: str, value):
    row = db.query(DeskState).get(key)
    if row:
        row.value = value
    else:
        db.add(DeskState(key=key, value=value))
    db.commit()


class StateBody(BaseModel):
    value: list


@router.get("/state/{key}")
def get_state(key: str, db: Session = Depends(get_db)):
    if key not in ("watchlist", "bench"):
        raise HTTPException(404)
    default = DEFAULT_WATCHLIST if key == "watchlist" else []
    return {"value": _get_state(db, key, default)}


@router.put("/state/{key}")
def put_state(key: str, body: StateBody, db: Session = Depends(get_db)):
    if key not in ("watchlist", "bench"):
        raise HTTPException(404)
    _put_state(db, key, [str(v)[:64] for v in body.value][:100])
    return {"ok": True}


# ---------- personas ----------

class PersonaBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    epithet: str = Field(default="", max_length=120)
    philosophy: str = Field(min_length=1, max_length=6000)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:48] or "member"


def _persona_dict(p: DeskPersona) -> dict:
    return {"id": p.id, "name": p.name, "epithet": p.epithet or "", "philosophy": p.philosophy}


@router.get("/personas")
def list_personas(db: Session = Depends(get_db)):
    return [_persona_dict(p) for p in db.query(DeskPersona).order_by(DeskPersona.created_at).all()]


@router.post("/personas")
def create_persona(body: PersonaBody, db: Session = Depends(get_db)):
    base = _slug(body.name)
    slug, n = base, 2
    while db.query(DeskPersona).get(slug):
        slug = f"{base}_{n}"
        n += 1
    p = DeskPersona(id=slug, name=body.name, epithet=body.epithet, philosophy=body.philosophy)
    db.add(p)
    db.commit()
    return _persona_dict(p)


@router.put("/personas/{persona_id}")
def update_persona(persona_id: str, body: PersonaBody, db: Session = Depends(get_db)):
    p = db.query(DeskPersona).get(persona_id)
    if not p:
        raise HTTPException(404, "No such member")
    p.name, p.epithet, p.philosophy = body.name, body.epithet, body.philosophy
    db.commit()
    return _persona_dict(p)


@router.delete("/personas/{persona_id}")
def delete_persona(persona_id: str, db: Session = Depends(get_db)):
    p = db.query(DeskPersona).get(persona_id)
    if not p:
        raise HTTPException(404, "No such member")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ---------- track record ----------

@router.get("/track-record")
async def track_record(db: Session = Depends(get_db)):
    import asyncio

    from app.backend.services.track_record import compute_track_records
    return await asyncio.to_thread(compute_track_records, db)


# ---------- notes ----------

@router.get("/notes")
def list_notes(db: Session = Depends(get_db)):
    """Newest-first summaries; the full payload stays behind /notes/{id}."""
    rows = db.query(DeskNote).order_by(DeskNote.created_at.desc()).limit(200).all()
    out = []
    for n in rows:
        decision = (n.data.get("decisions") or {}).get(n.ticker) or n.data.get("decisions") or {}
        out.append({
            "id": n.id,
            "ticker": n.ticker,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "model_name": n.model_name,
            "run_cost": n.run_cost,
            "action": decision.get("action"),
            "confidence": decision.get("confidence"),
            "thesis": decision.get("reasoning"),
        })
    return out


@router.get("/notes/{note_id}")
def get_note(note_id: int, db: Session = Depends(get_db)):
    n = db.query(DeskNote).get(note_id)
    if not n:
        raise HTTPException(404, "No such note")
    return {"id": n.id, "ticker": n.ticker,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "run_cost": n.run_cost, "data": n.data}


class ImportBody(BaseModel):
    """One-time migration of notes saved in the browser before server storage."""
    notes: list[dict] = []
    watchlist: list[str] | None = None


@router.post("/import")
def import_local(body: ImportBody, db: Session = Depends(get_db)):
    imported = 0
    if db.query(DeskNote).count() == 0:
        for item in body.notes[:50]:
            data = item.get("data") or {}
            if not data.get("ticker"):
                continue
            db.add(DeskNote(ticker=data["ticker"], model_name=data.get("model_name"),
                            run_cost=data.get("run_cost"), data=data))
            imported += 1
    if body.watchlist and not db.query(DeskState).get("watchlist"):
        db.add(DeskState(key="watchlist", value=body.watchlist[:100]))
    db.commit()
    return {"imported": imported}


def save_note(db: Session, data: dict) -> int:
    """Called by the run route when a committee session completes."""
    from app.backend.services.track_record import invalidate_cache

    n = DeskNote(ticker=data.get("ticker"), model_name=data.get("model_name"),
                 run_cost=data.get("run_cost"), data=data)
    db.add(n)
    db.commit()
    invalidate_cache()
    return n.id
