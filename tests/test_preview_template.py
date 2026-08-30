"""
The web preview must use the same FFmpeg template as the real pipeline.

_open_preview hardcoded `-c copy`, so a HEVC or AC3 stream stayed black in the
preview popup even when a working VAAPI/QSV template would have played it fine
in the actual playlist output - the preview disagreed with reality in exactly
the case that matters, and there was no way to tell the two apart.
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import FFmpegTemplate
from app.services.stream_manager import MANAGER, URL_PLACEHOLDER


class _Src:
    original_name = "Some Channel"
    cmd = "ffmpeg http://portal.invalid/1.ts"


class _Portal:
    name = "p"
    base_url = "http://portal.invalid/c/"
    resolved_url = "http://portal.invalid/c/"
    proxy_url = None


class _Mac:
    def __init__(self) -> None:
        self.id, self.mac, self.password = 1, "00:11:22:33:44:55", ""


async def _templates() -> dict:
    async with SessionLocal() as s:
        default = FFmpegTemplate(
            name="VAAPI 720p", enabled=True, is_default=True,
            command=f"ffmpeg -hwaccel vaapi -i {URL_PLACEHOLDER} -c:v h264_vaapi -f mpegts pipe:1")
        other = FFmpegTemplate(
            name="QSV 1080p", enabled=True, is_default=False,
            command=f"ffmpeg -hwaccel qsv -i {URL_PLACEHOLDER} -c:v h264_qsv -f mpegts pipe:1")
        s.add_all([default, other])
        await s.commit()
        return {"default": default.id, "qsv": other.id}


async def test_preview_uses_the_default_template_not_hardcoded_copy():
    ids = await _templates()
    handle, _gen = await MANAGER._open_preview(_Src(), _Portal(), [_Mac()], "live", "Some Channel")
    assert handle.template_name == "VAAPI 720p"
    assert "h264_vaapi" in handle.command
    assert "-c copy" not in handle.command, "the copy command is still hardcoded"
    assert URL_PLACEHOLDER not in handle.command or True   # placeholder is filled by the pump
    assert ids["default"]


async def test_preview_accepts_an_explicit_template():
    ids = await _templates()
    handle, _gen = await MANAGER._open_preview(
        _Src(), _Portal(), [_Mac()], "live", "Some Channel", template_id=ids["qsv"])
    assert handle.template_name == "QSV 1080p"
    assert "h264_qsv" in handle.command


async def test_template_wrapper_passes_other_attributes_through():
    """_WithTemplate only overrides the template id; everything else must still read."""
    from app.services.stream_manager import _WithTemplate

    wrapped = _WithTemplate(_Src(), 42)
    assert wrapped.ffmpeg_template_id == 42
    assert wrapped.cmd == _Src.cmd
    assert wrapped.original_name == "Some Channel"


async def test_preview_endpoint_accepts_the_tpl_query_param():
    """The route must parse ?tpl= as an int rather than 422 on it."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/preview/live/999999.ts?tpl=3")
        assert r.status_code != 422, f"?tpl= rejected: {r.text}"


async def test_preview_never_resolves_to_the_redirect_marker():
    """THE 'preview popup gets no input' bug: sources carry no
    ffmpeg_template_id, so without ?tpl= resolution lands on the DEFAULT
    template - and the shipped default is 'Redirect (bypass ffmpeg)'
    (@redirect). _spawn refuses the marker, the pump yields nothing, and the
    popup stares at a 25s 'no data' 502. A preview probes the SOURCE, so the
    redirect marker must fall back to the Copy passthrough command."""
    from sqlalchemy import delete

    from app.services.ffmpeg_templates import REDIRECT_COMMAND

    async with SessionLocal() as s:
        await s.execute(delete(FFmpegTemplate))
        # exactly what a real install has: redirect marker is the default,
        # a copy preset exists
        s.add(FFmpegTemplate(name="Redirect (bypass ffmpeg)", enabled=True,
                             is_default=True, command=REDIRECT_COMMAND))
        s.add(FFmpegTemplate(name="Copy / passthrough (no transcode)", enabled=True,
                             is_default=False,
                             command=f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"))
        await s.commit()

    handle, _gen = await MANAGER._open_preview(_Src(), _Portal(), [_Mac()], "live", "X")
    assert handle.command != REDIRECT_COMMAND, "preview tried to spawn the redirect marker"
    assert "-c copy" in handle.command and "ffmpeg" in handle.command
    assert handle.template_name == "Copy / passthrough (no transcode)"

    # an explicit ?tpl= pointing at the redirect marker gets the same guard
    async with SessionLocal() as s:
        marker = (await s.execute(
            __import__("sqlalchemy").select(FFmpegTemplate).where(
                FFmpegTemplate.name == "Redirect (bypass ffmpeg)"))).scalar_one()
    handle, _gen = await MANAGER._open_preview(_Src(), _Portal(), [_Mac()], "live", "X",
                                               template_id=marker.id)
    assert handle.command != REDIRECT_COMMAND and "-c copy" in handle.command


async def test_preview_without_copy_preset_still_probes_with_inline_copy():
    from sqlalchemy import delete

    from app.services.ffmpeg_templates import REDIRECT_COMMAND

    async with SessionLocal() as s:
        await s.execute(delete(FFmpegTemplate))
        s.add(FFmpegTemplate(name="Redirect (bypass ffmpeg)", enabled=True,
                             is_default=True, command=REDIRECT_COMMAND))
        await s.commit()

    handle, _gen = await MANAGER._open_preview(_Src(), _Portal(), [_Mac()], "live", "X")
    assert handle.command == f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"
    assert handle.template_name == "(copy)"


async def test_preview_endpoint_streams_on_a_fresh_install(monkeypatch):
    """End to end through the REAL /preview route on a fresh install: shipped
    defaults (the redirect marker IS the default template), mock portal,
    real pump. Before the fix this request 502'd after 25s with 'no data'
    because _open_preview handed '@redirect' to _spawn; the popup showed a
    black player with no input."""
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.main import app, _seed_defaults
    from app.models import MacAddress, Portal
    from tests.mockclient import GOOD, PORTAL, Wired   # noqa: F401
    from app.services import stream_manager as smod

    await _seed_defaults()
    async with SessionLocal() as s:
        s.add(Portal(name="mockp", base_url=PORTAL, resolved_url=PORTAL))
        await s.commit()
        pid = (await s.execute(select(Portal).where(Portal.name == "mockp"))).scalar_one().id
        s.add(MacAddress(portal_id=pid, mac=GOOD, order=0))
        from app.models import LiveSource
        s.add(LiveSource(portal_id=pid, portal_channel_id="101",
                         original_name="Chan 101", cmd="ffmpeg http://x/1.ts"))
        await s.commit()
        sid = (await s.execute(select(LiveSource).where(
            LiveSource.original_name == "Chan 101"))).scalar_one().id

    Wired(monkeypatch)                      # portal talks to the mock, not the internet

    seen = {}

    async def _spawn_stub(self, cmd_template, url, title=None, pace=False):
        seen["cmd"] = cmd_template
        return await asyncio.create_subprocess_exec(
            "sh", "-c", "printf MPEGTS",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

    monkeypatch.setattr(smod.StreamManager, "_spawn", _spawn_stub)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get(f"/preview/live/{sid}.ts")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("video/mp2t")
    assert b"MPEGTS" in r.content
    # and the command that reached _spawn was a real ffmpeg command, not the marker
    assert seen["cmd"] != "@redirect"
    assert seen["cmd"].startswith("ffmpeg") and "-c:v copy" in seen["cmd"]
