"""
ffmpeg template API - CRUD plus the two build/parse endpoints that power the
GUI's two-way sync between option fields and the full command text.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from ..database import get_db
from ..models import FFmpegTemplate
from ..security import require_admin
from ..services.ffmpeg_templates import FFmpegOptions, build_command, parse_command
from ..services.ffmpeg_validate import TEST_VIDEO_URL, run_demo, syntax_check

router = APIRouter(prefix="/api/ffmpeg", tags=["ffmpeg"], dependencies=[Depends(require_admin)])

FIELDS = [c for c in FFmpegTemplate.__table__.columns.keys() if c != "id"]


def _row(t: FFmpegTemplate) -> dict:
    return {c: getattr(t, c) for c in FIELDS} | {"id": t.id}


@router.get("")
async def templates_list(db=Depends(get_db)):
    rows = (await db.execute(select(FFmpegTemplate).order_by(FFmpegTemplate.name))).scalars().all()
    return {"items": [_row(r) for r in rows]}


@router.post("")
async def create_template(payload: dict, db=Depends(get_db)):
    t = FFmpegTemplate()
    for f in FIELDS:
        if f in payload:
            setattr(t, f, payload[f])
    t.command = t.command or build_command(_opts(t))
    db.add(t)
    await db.commit()
    return {"item": _row(t)}


@router.put("/{tid}")
async def update_template(tid: int, payload: dict, db=Depends(get_db)):
    t = await db.get(FFmpegTemplate, tid)
    if not t:
        raise HTTPException(404, "template not found")
    for f in FIELDS:
        if f in payload:
            setattr(t, f, payload[f])
    await db.commit()
    return {"item": _row(t)}


@router.delete("/{tid}")
async def delete_template(tid: int, db=Depends(get_db)):
    t = await db.get(FFmpegTemplate, tid)
    if not t:
        raise HTTPException(404, "template not found")
    await db.delete(t)
    await db.commit()
    return {"ok": True}


@router.post("/build")
async def build(payload: dict):
    """fields -> command (2-way sync, left side of the editor)."""
    opts = {k: v for k, v in payload.items() if hasattr(FFmpegOptions, k)}
    return {"command": build_command(FFmpegOptions(**opts))}


@router.post("/parse")
async def parse(payload: dict):
    """command -> fields (right side of the editor)."""
    return parse_command(payload.get("command", ""))


def _opts(t: FFmpegTemplate) -> FFmpegOptions:
    return FFmpegOptions(
        **{f: getattr(t, f) for f in FFmpegOptions.__dataclass_fields__ if hasattr(t, f)})
