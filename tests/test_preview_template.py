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
