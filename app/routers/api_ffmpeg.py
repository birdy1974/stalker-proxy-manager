"""
ffmpeg template API - CRUD plus the two build/parse endpoints that power the
GUI's two-way sync between option fields and the full command text.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

from ..database import get_db
from ..models import (
    FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource, LocalFile,
    LocalPlaylist, LocalSource, Portal, SeriePlaylist, SerieSource,
    VodPlaylist, VodSource,
)
from ..security import require_admin
from ..services import item_info
from ..services.ffmpeg_templates import (FFmpegOptions, REDIRECT_COMMAND,
                                     build_command, coerce_options,
                                     option_warnings, parse_command)
from ..services.ffmpeg_validate import run_demo, syntax_check

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


@router.get("/demo-sources")
async def demo_sources(kind: str = "live", q: str = "", db=Depends(get_db)):
    """Enabled playlist items the FFmpeg tab can demo a template against."""
    q = (q or "").strip()
    if kind not in item_info.PLAYLIST_KINDS:
        raise HTTPException(400, "kind must be live|vod|series|local")
    like = f"%{q}%" if q else None
    items: list[dict] = []
    if kind == "live":
        stmt = (select(LivePlaylist, LiveSource.original_name, Portal.name)
                .outerjoin(LivePlaylistSource, (LivePlaylistSource.live_playlist_id == LivePlaylist.id)
                           & (LivePlaylistSource.priority == 1))
                .outerjoin(LiveSource, LiveSource.id == LivePlaylistSource.live_source_id)
                .outerjoin(Portal, Portal.id == LiveSource.portal_id)
                .where(LivePlaylist.enabled.is_(True)))
        if like:
            stmt = stmt.where(or_(LivePlaylist.custom_name.ilike(like),
                                  LiveSource.original_name.ilike(like)))
        stmt = stmt.order_by(LivePlaylist.custom_name).limit(80)
        for pl, src_name, portal_name in (await db.execute(stmt)).all():
            items.append({"id": pl.id, "kind": "live", "name": pl.custom_name,
                          "group": pl.group_name, "source": src_name, "portal": portal_name})
    elif kind == "vod":
        stmt = (select(VodPlaylist, VodSource.original_name, Portal.name)
                .outerjoin(VodSource, VodSource.id == VodPlaylist.vod_source_id)
                .outerjoin(Portal, Portal.id == VodSource.portal_id)
                .where(VodPlaylist.enabled.is_(True)))
        if like:
            stmt = stmt.where(or_(VodPlaylist.custom_name.ilike(like),
                                  VodSource.original_name.ilike(like)))
        stmt = stmt.order_by(VodPlaylist.custom_name).limit(80)
        for pl, src_name, portal_name in (await db.execute(stmt)).all():
            items.append({"id": pl.id, "kind": "vod", "name": pl.custom_name,
                          "group": pl.group_name, "source": src_name, "portal": portal_name})
    elif kind == "series":
        stmt = (select(SeriePlaylist, SerieSource.original_name, Portal.name)
                .outerjoin(SerieSource, SerieSource.id == SeriePlaylist.serie_source_id)
                .outerjoin(Portal, Portal.id == SerieSource.portal_id)
                .where(SeriePlaylist.enabled.is_(True)))
        if like:
            stmt = stmt.where(or_(SeriePlaylist.custom_name.ilike(like),
                                  SerieSource.original_name.ilike(like)))
        stmt = stmt.order_by(SeriePlaylist.custom_name).limit(80)
        for pl, src_name, portal_name in (await db.execute(stmt)).all():
            items.append({"id": pl.id, "kind": "series", "name": pl.custom_name,
                          "group": pl.group_name, "source": src_name, "portal": portal_name})
    else:
        stmt = (select(LocalPlaylist, LocalFile.filename, LocalSource.directory)
                .outerjoin(LocalFile, LocalFile.id == LocalPlaylist.local_file_id)
                .outerjoin(LocalSource, LocalSource.id == LocalFile.local_source_id)
                .where(LocalPlaylist.enabled.is_(True)))
        if like:
            stmt = stmt.where(or_(LocalPlaylist.custom_name.ilike(like),
                                  LocalFile.filename.ilike(like)))
        stmt = stmt.order_by(LocalPlaylist.custom_name).limit(80)
        for pl, filename, directory in (await db.execute(stmt)).all():
            items.append({"id": pl.id, "kind": "local", "name": pl.custom_name,
                          "group": pl.group_name, "source": filename, "portal": directory})
    return {"items": items, "kind": kind, "query": q}


@router.post("/demo")
async def demo(payload: dict, db=Depends(get_db)):
    """Run the template command against a short test input (~2 s).

    mode: 'lavfi' (synthetic testsrc2), 'url' (HTTP clip), or 'playlist'
    (an enabled playlist item identified by kind + id).
    """
    command = payload.get("command", "")
    mode = payload.get("mode") or "lavfi"
    if mode == "playlist" or (payload.get("kind") and payload.get("id") is not None
                              and payload.get("id") != ""):
        kind = payload.get("kind") or "live"
        try:
            pid = int(payload.get("id"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "id must be an integer") from exc
        try:
            resolved = await item_info.resolve_playlist_input(db, kind, pid)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        label = resolved["name"]
        if resolved.get("source"):
            label = f"{label} · {resolved['source']}"
        result = await run_demo(
            command=command, mode="playlist", url=resolved["url"],
            source_label=f"{kind} #{resolved['id']} {label}",
        )
        result["playlist"] = {"kind": kind, "id": resolved["id"],
                              "name": resolved["name"],
                              "source": resolved.get("source"),
                              "url": resolved["url"]}
        return result
    return await run_demo(
        command=command,
        mode=mode,
        url=payload.get("url"),
    )


def _opts(t: FFmpegTemplate) -> FFmpegOptions:
    """The row's structured columns as options - coerced, because a JSON import
    can put anything into a text column and re-rendering a template must not
    crash on it."""
    return FFmpegOptions(**coerce_options(
        {f: getattr(t, f) for f in FFmpegOptions.__dataclass_fields__ if hasattr(t, f)}))
