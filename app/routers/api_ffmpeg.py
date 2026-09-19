"""
ffmpeg template API - CRUD plus the two build/parse endpoints that power the
GUI's two-way sync between option fields and the full command text.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select, update

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
                                     extra_option_warnings,
                                     option_warnings, parse_command,
                                     template_command_errors)
from ..services.ffmpeg_validate import run_demo, syntax_check
from ..services.ffmpeg_editor import disabled_parameters, field_errors, set_extra_option

router = APIRouter(prefix="/api/ffmpeg", tags=["ffmpeg"], dependencies=[Depends(require_admin)])

FIELDS = [c for c in FFmpegTemplate.__table__.columns.keys() if c != "id"]


def _row(t: FFmpegTemplate) -> dict:
    return {c: getattr(t, c) for c in FIELDS} | {"id": t.id}


def _template_errors(t: FFmpegTemplate) -> list[str]:
    """Validate the stored command and raw extension fields before persistence."""
    errors = extra_option_warnings(_opts(t))
    if t.command_source == "fields" and (t.command or "").strip() != REDIRECT_COMMAND:
        errors.extend(field_errors(_opts(t)))
    errors.extend(template_command_errors(t.command or ""))
    return errors


def _reject_template(errors: list[str]) -> None:
    if errors:
        raise HTTPException(422, {
            "message": "invalid FFmpeg template",
            "errors": errors,
        })


def _source_from_payload(t: FFmpegTemplate, payload: dict) -> str:
    """Choose the command authority, inferring legacy/GUI requests safely.

    Older clients sent fields and command together but did not always send a
    reliable command_source for a newly-created row. If the supplied text is
    different from what the fields would render, it is necessarily a manual
    command and must not be re-rendered behind the user's back.
    """
    requested = payload.get("command_source")
    if requested is not None and requested not in ("fields", "manual"):
        raise HTTPException(422, "command_source must be fields or manual")
    if requested == "manual":
        return "manual"
    if requested == "fields":
        # An explicit source is authoritative in both directions. The fields
        # editor may send stale command text while a request is being assembled;
        # fields mode must deterministically replace it rather than guessing
        # that the user meant manual mode.
        return "fields"
    if "command" in payload:
        supplied = str(payload.get("command", "") or "").strip()
        expected = (REDIRECT_COMMAND if supplied == REDIRECT_COMMAND
                    else build_command(_opts(t))).strip()
        return "manual" if supplied and supplied != expected else "fields"
    return t.command_source if t.command_source in ("fields", "manual") else "fields"


def _requested_default(payload):
    value = payload.get("is_default")
    if "is_default" in payload and not isinstance(value, bool):
        raise HTTPException(422, "is_default must be true or false")
    return value


async def _choose_default(db, template):
    await db.flush()  # materialize defaults for newly-created templates
    if not template.enabled:
        raise HTTPException(422, "Enable the template before making it the default")
    # A single UPDATE replaces the selection atomically instead of leaving
    # multiple defaults behind when clients select different templates.
    await db.execute(update(FFmpegTemplate).values(
        is_default=(FFmpegTemplate.id == template.id)))


@router.post("/{tid}/default")
async def choose_default(tid: int, db=Depends(get_db)):
    template = await db.get(FFmpegTemplate, tid)
    if not template:
        raise HTTPException(404, "template not found")
    await _choose_default(db, template)
    await db.commit()
    return {"item": _row(template)}


@router.get("")
async def templates_list(db=Depends(get_db)):
    rows = (await db.execute(select(FFmpegTemplate).order_by(FFmpegTemplate.name))).scalars().all()
    return {"items": [_row(r) for r in rows]}


@router.post("")
async def create_template(payload: dict, db=Depends(get_db)):
    make_default = _requested_default(payload)
    t = FFmpegTemplate()
    for f in FIELDS:
        if f in payload and f != "is_default":
            setattr(t, f, payload[f])
    t.command_source = _source_from_payload(t, payload)
    if t.command_source == "fields":
        t.command = ((t.command or "").strip() == REDIRECT_COMMAND
                     and REDIRECT_COMMAND) or build_command(_opts(t))
    _reject_template(_template_errors(t))
    db.add(t)
    if make_default:
        await _choose_default(db, t)
    await db.commit()
    return {"item": _row(t)}


@router.put("/{tid}")
async def update_template(tid: int, payload: dict, db=Depends(get_db)):
    t = await db.get(FFmpegTemplate, tid)
    if not t:
        raise HTTPException(404, "template not found")
    make_default = _requested_default(payload)
    if t.is_default and (make_default is False or payload.get("enabled") is False):
        raise HTTPException(409, "Choose another default before disabling or clearing this default")
    old_command = (t.command or "").strip()
    was_redirect = old_command == REDIRECT_COMMAND
    for f in FIELDS:
        if f in payload and f != "is_default":
            setattr(t, f, payload[f])

    if t.is_default and not t.enabled:
        raise HTTPException(409, "Choose another default before disabling this template")
    source = _source_from_payload(t, payload)
    supplied_command = str(payload.get("command", "") or "").strip()
    if "command" in payload:
        t.command = supplied_command
    t.command_source = source

    # Fields are authoritative only in fields mode. A manual command remains
    # byte-for-byte intact when a script or an older UI sends unrelated field
    # updates without the command text.
    if source == "fields":
        if (t.command or "").strip() == REDIRECT_COMMAND:
            t.command = REDIRECT_COMMAND
        elif "command" not in payload or supplied_command != build_command(_opts(t)):
            t.command = build_command(_opts(t))

    # A redirect row carries structured defaults only for seeding/UI shape. It
    # must not leak stale raw extras into a newly-created FFmpeg command.
    if was_redirect and (t.command or "").strip() != REDIRECT_COMMAND:
        if "extra_input" not in payload:
            t.extra_input = ""
        if "extra_output" not in payload:
            t.extra_output = ""
        if source == "fields":
            t.command = build_command(_opts(t))

    _reject_template(_template_errors(t))
    if make_default:
        await _choose_default(db, t)
    await db.commit()
    return {"item": _row(t)}


@router.delete("/{tid}")
async def delete_template(tid: int, db=Depends(get_db)):
    t = await db.get(FFmpegTemplate, tid)
    if not t:
        raise HTTPException(404, "template not found")
    if t.is_default:
        raise HTTPException(409, "Choose another default before deleting this template")
    await db.delete(t)
    await db.commit()
    return {"ok": True}


@router.post("/build")
async def build(payload: dict):
    """fields -> command (2-way sync, left side of the editor)."""
    opts = FFmpegOptions(**coerce_options(payload))
    _reject_template(field_errors(opts))
    return {"command": build_command(opts), "warnings": option_warnings(opts)}


@router.post("/extra-option")
async def extra_option(payload: dict):
    """Insert/replace an advanced option, preserving quoting and validating ranges."""
    try:
        if "options" in payload:
            opts = FFmpegOptions(**coerce_options(payload["options"]))
            reason = disabled_parameters(opts, "advanced").get(payload.get("flag"))
            if reason:
                raise ValueError(reason)
        return {"extra": set_extra_option(payload.get("raw", ""), payload.get("side"),
                                          payload.get("flag"), payload.get("value", ""))}
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None


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
            # avoid_busy: the demo opens a media connection on the MAC it
            # resolves through. Doing that on a MAC that is streaming right now
            # is what the panel answers with HTTP 456 (single connection slot),
            # so a demo run beside a playing box used to report a bare
            # "rc=8 with no output". Resolve through a free MAC - or say so.
            resolved = await item_info.resolve_playlist_input(db, kind, pid,
                                                              avoid_busy=True)
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
                              "url": resolved["url"],
                              "mac": resolved.get("mac") or "",
                              "portal": resolved.get("portal") or ""}
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
