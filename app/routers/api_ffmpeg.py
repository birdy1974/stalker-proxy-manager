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
from ..services.ffmpeg_templates import (FFmpegOptions, REDIRECT_COMMAND,
                                     build_command, coerce_options,
                                     option_warnings, parse_command)
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
    touched_opts = False
    for f in FIELDS:
        if f in payload:
            setattr(t, f, payload[f])
            touched_opts = touched_opts or f in FFmpegOptions.__dataclass_fields__
    # `command` is derived state while command_source says it was rendered from
    # the fields, so a caller that edits those fields without resending the text
    # (a script, an import, a PATCH-style UI widget) must not be left with a
    # command that contradicts the row - it is what the stream path runs. A
    # payload that carries its own command wins as sent, and a manual command is
    # the user's text and stays byte-for-byte theirs. The redirect preset is not
    # a command at all: rendering one would quietly turn the 302 marker back into
    # an ffmpeg invocation.
    if (touched_opts and "command" not in payload and t.command_source == "fields"
            and (t.command or "").strip() != REDIRECT_COMMAND):
        t.command = build_command(_opts(t))
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
    opts = FFmpegOptions(**coerce_options(payload))
    return {"command": build_command(opts), "warnings": option_warnings(opts)}


@router.post("/parse")
async def parse(payload: dict):
    """command -> fields (right side of the editor).

    `base` is the editor's current field state: what the command text cannot
    say (a rate-control mode it never mentions, a bitrate CQP does not render)
    is kept from there instead of being reset to the shipped defaults.
    """
    return parse_command(payload.get("command", ""), base=payload.get("base"))


@router.post("/validate")
async def validate(payload: dict):
    """Syntax-only check: tokens, placeholder, balanced quotes."""
    return syntax_check(payload.get("command", ""))


@router.post("/demo")
async def demo(payload: dict):
    """Run the template command against a short test input (~2 s).
    mode: 'lavfi' (synthetic testsrc2, no network) or 'url' (real HTTP clip)."""
    return await run_demo(
        command=payload.get("command", ""),
        mode=payload.get("mode", "lavfi"),
    )


def _opts(t: FFmpegTemplate) -> FFmpegOptions:
    """The row's structured columns as options - coerced, because a JSON import
    can put anything into a text column and re-rendering a template must not
    crash on it."""
    return FFmpegOptions(**coerce_options(
        {f: getattr(t, f) for f in FFmpegOptions.__dataclass_fields__ if hasattr(t, f)}))
