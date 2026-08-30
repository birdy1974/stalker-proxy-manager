"""
The per-channel "Redirect (bypass ffmpeg)" template.

The old global proxy/redirect switch (general settings) is gone. Redirect is
now a built-in FFmpeg template that can be assigned per channel: assigning it
makes the stream path 302 the player straight to the panel's CDN instead of
proxying through ffmpeg. It is ALSO the built-in default template, so an item
without an explicit template assignment redirects as well.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app, _seed_defaults
from app.models import (
    FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource, Portal, User,
)
from app.services.ffmpeg_templates import (
    REDIRECT_COMMAND, REDIRECT_PRESET_NAME, REFERENCE_PRESET_NAME,
    FFmpegOptions, build_command,
)
from app.services.stream_manager import MANAGER

BASE = "http://testserver"


async def _seed_and_get_ids() -> tuple[int, int]:
    """(redirect template id, a real transcode template id)."""
    await _seed_defaults()
    async with SessionLocal() as s:
        rid = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == REDIRECT_PRESET_NAME))).scalar_one().id
        proxy = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == REFERENCE_PRESET_NAME))).scalar_one().id
        return rid, proxy


async def test_redirect_preset_is_seeded_as_a_builtin_and_is_the_default():
    rid, _proxy = await _seed_and_get_ids()
    async with SessionLocal() as s:
        row = await s.get(FFmpegTemplate, rid)
        assert row.is_builtin is True
        assert row.enabled is True
        assert row.command == REDIRECT_COMMAND
        assert row.is_default is True
        # exactly one default, and it is the redirect preset
        defaults = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.is_default.is_(True)))).scalars().all()
        assert len(defaults) == 1
        assert defaults[0].name == REDIRECT_PRESET_NAME


async def _live_channel(template_id: int | None = None) -> int:
    """A live playlist item whose source portal has no MAC (nothing resolves)."""
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        src = LiveSource(portal_id=portal.id, portal_channel_id="1",
                         original_name="Ch", cmd="ffmpeg http://x/1.ts",
                         enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Ch", enabled=True,
                          ffmpeg_template_id=template_id)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id


async def test_uses_redirect_reflects_the_assigned_template():
    rid, proxy = await _seed_and_get_ids()
    proxied = await _live_channel(proxy)        # explicit transcode template
    redir = await _live_channel(rid)            # explicit redirect template
    defaulted = await _live_channel(None)       # no template -> default = redirect
    assert await MANAGER.uses_redirect("live", proxied) is False
    assert await MANAGER.uses_redirect("live", redir) is True
    assert await MANAGER.uses_redirect("live", defaulted) is True


async def test_play_endpoint_bypasses_ffmpeg_when_channel_has_the_template():
    rid, proxy = await _seed_and_get_ids()
    plain = await _live_channel(proxy)          # explicit proxy template -> ffmpeg
    redir = await _live_channel(rid)
    async with SessionLocal() as s:
        user = User(name="u1", password="pw", enabled=True, m3u_enabled=True)
        s.add(user)
        await s.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        # proxy channel -> ffmpeg path -> no usable source -> 404
        p = await c.get(f"/play/live/{plain}.ts?u=u1&p=pw")
        assert p.status_code == 404, f"expected 404 proxy, got {p.status_code}: {p.text}"
        # redirect channel -> same dead portal, but now it must 502 "no link"
        r = await c.get(f"/play/live/{redir}.ts?u=u1&p=pw")
        assert r.status_code == 502, f"expected 502 redirect, got {r.status_code}: {r.text}"
        assert "no source produced a link" in r.text


async def test_mode_param_still_overrides_the_template():
    """`?mode=proxy` must force the proxy path even when the channel carries
    the redirect template (per-URL override keeps working)."""
    rid, _proxy = await _seed_and_get_ids()
    redir = await _live_channel(rid)
    async with SessionLocal() as s:
        user = User(name="u2", password="pw", enabled=True, m3u_enabled=True)
        s.add(user)
        await s.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get(f"/play/live/{redir}.ts?u=u2&p=pw&mode=proxy")
        assert r.status_code == 404, f"expected 404 proxy, got {r.status_code}: {r.text}"


async def test_boot_seed_moves_builtins_to_the_shipped_rate_control():
    """The built-in templates live in the database, so the shipped default for
    `-rc_mode` only reaches an existing install through this re-seed. A template
    the user created themselves is left exactly as they set it - same rule the
    other VAAPI fields follow."""
    await _seed_defaults()
    async with SessionLocal() as s:
        s.add(FFmpegTemplate(name="My VAAPI 720p", video_codec="h264_vaapi",
                             rc_mode="VBR", global_quality="",
                             command=build_command(FFmpegOptions(rc_mode="VBR")),
                             command_source="fields"))
        await s.commit()
    await _seed_defaults()          # second boot
    async with SessionLocal() as s:
        builtin = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == REFERENCE_PRESET_NAME))).scalar_one()
        assert builtin.rc_mode == "CQP"
        assert "-rc_mode CQP -global_quality 26" in builtin.command
        assert "-b:v" not in builtin.command        # CQP does not honour it
        mine = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == "My VAAPI 720p"))).scalar_one()
        assert mine.is_builtin is False
        assert mine.rc_mode == "VBR" and "-b:v 1000k" in mine.command


async def test_boot_seed_never_rewrites_a_hand_edited_builtin_command():
    """A built-in whose command text was edited by hand keeps that text: the
    structured fields are refreshed (so the GUI reflects what the app ships)
    but the command the stream path actually runs is the user's."""
    await _seed_defaults()
    custom = ("ffmpeg -i <url> -c:v h264_vaapi -b:v 1800k -rc_mode VBR "
              "-f mpegts pipe:1")
    async with SessionLocal() as s:
        row = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == REFERENCE_PRESET_NAME))).scalar_one()
        row.command = custom
        row.command_source = "manual"
        await s.commit()
    await _seed_defaults()
    async with SessionLocal() as s:
        row = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == REFERENCE_PRESET_NAME))).scalar_one()
        assert row.command == custom
        assert row.rc_mode == "CQP"     # the field says what the app now ships
