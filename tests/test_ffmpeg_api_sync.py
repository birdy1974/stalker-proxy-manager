"""The ffmpeg template API's side of the two-way sync.

`build` and `parse` exist so the editor can keep the option fields and the
command text in step; the GUI always sends both, but the CRUD endpoints can be
called on their own (a script, an import, a future UI widget), and two things
then need to hold:

  * the stored command never contradicts the stored fields - `command` is
    derived state while `command_source` says it was rendered from the fields;
  * a hand-written command is never re-rendered behind the user's back, and the
    `@redirect` marker is never turned into an ffmpeg command by a field edit.

Both are exercised through the real endpoints, with the real DB rows, because
every one of them is a property of the row the stream path later reads.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app, _seed_defaults
from app.models import FFmpegTemplate
from app.services.ffmpeg_templates import (FFmpegOptions, REDIRECT_COMMAND,
                                           REDIRECT_PRESET_NAME,
                                           REFERENCE_PRESET_NAME, build_command)

BASE = "http://testserver"


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE)


async def _by_name(name: str) -> dict:
    async with SessionLocal() as s:
        row = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == name))).scalar_one()
        return {c.name: getattr(row, c.name) for c in FFmpegTemplate.__table__.columns}


async def test_a_fields_only_put_re_renders_the_command():
    """The bug: PUT {"rc_mode": "VBR"} updated the column and left a CQP command
    in `command` - and the command is what the stream path runs. Flipping the
    mode has to bring the bitrate flags back and drop the QP."""
    await _seed_defaults()
    row = await _by_name(REFERENCE_PRESET_NAME)
    assert "-rc_mode CQP -global_quality 26" in row["command"]
    async with await _client() as c:
        r = await c.put(f"/api/ffmpeg/{row['id']}", json={"rc_mode": "VBR"})
        assert r.status_code == 200, r.text
    after = await _by_name(REFERENCE_PRESET_NAME)
    assert after["rc_mode"] == "VBR"
    assert "-rc_mode VBR" in after["command"]
    assert "-b:v 1000k" in after["command"] and "-global_quality" not in after["command"]
    assert after["command"] == build_command(
        FFmpegOptions(**{k: v for k, v in after.items()
                         if k in FFmpegOptions.__dataclass_fields__}))


async def test_a_payload_carrying_its_own_command_wins_as_sent():
    """The GUI saves fields *and* text in one request; that text is what gets
    stored, even if it disagrees with the fields (it is what the user read)."""
    await _seed_defaults()
    row = await _by_name(REFERENCE_PRESET_NAME)
    mine = "ffmpeg -i <url> -c:v h264_vaapi -b:v 999k -f mpegts pipe:1"
    async with await _client() as c:
        r = await c.put(f"/api/ffmpeg/{row['id']}",
                        json={"rc_mode": "VBR", "command": mine})
    assert (await _by_name(REFERENCE_PRESET_NAME))["command"] == mine


async def test_a_manual_command_survives_field_edits():
    """`command_source: manual` is the promise that the text is the user's."""
    await _seed_defaults()
    row = await _by_name(REFERENCE_PRESET_NAME)
    mine = "ffmpeg -i <url> -c:v h264_vaapi -b:v 1800k -rc_mode CBR -f mpegts pipe:1"
    async with await _client() as c:
        await c.put(f"/api/ffmpeg/{row['id']}", json={"command": mine,
                                                       "command_source": "manual"})
        await c.put(f"/api/ffmpeg/{row['id']}",
                    json={"rc_mode": "CQP", "global_quality": "30"})
    after = await _by_name(REFERENCE_PRESET_NAME)
    assert after["command"] == mine
    assert (after["rc_mode"], after["global_quality"]) == ("CQP", "30")


async def test_the_redirect_marker_is_never_rendered_into_a_command():
    """The redirect preset is a sentinel with ordinary option fields behind it
    (command_source is 'fields', like any other row), so the re-render has to
    know not to touch it: a rendered command there would replace a 302 with an
    ffmpeg spawn for every channel using the default template."""
    await _seed_defaults()
    row = await _by_name(REDIRECT_PRESET_NAME)
    assert row["command"] == REDIRECT_COMMAND and row["command_source"] == "fields"
    async with await _client() as c:
        r = await c.put(f"/api/ffmpeg/{row['id']}",
                        json={"rc_mode": "VBR", "video_bitrate": "4000k",
                              "resolution": "2160p"})
        assert r.status_code == 200
    after = await _by_name(REDIRECT_PRESET_NAME)
    assert after["command"] == REDIRECT_COMMAND
    assert after["rc_mode"] == "VBR"          # the fields did change


async def test_parse_keeps_the_fields_a_command_cannot_express():
    """The editor's command -> fields direction is allowed to be partial: it is
    handed the row's own fields as the base, so a CQP command (no bitrate in the
    text) does not reset the template's tuning to the shipped default."""
    await _seed_defaults()
    row = await _by_name("VAAPI 1080p ~2.5M")
    assert "-b:v" not in row["command"] and row["video_bitrate"] == "2500k"
    async with await _client() as c:
        bare = (await c.post("/api/ffmpeg/parse", json={"command": row["command"]})).json()
        with_base = (await c.post("/api/ffmpeg/parse", json={
            "command": row["command"],
            "base": {k: v for k, v in row.items() if k in FFmpegOptions.__dataclass_fields__}})).json()
    assert bare["options"]["video_bitrate"] == "1000k"        # stateless: the default
    assert with_base["options"]["video_bitrate"] == "2500k"  # from the row
    assert with_base["options"]["resolution"] == "1080p"
    assert with_base["options"]["rc_mode"] == "CQP"


async def test_build_coerces_what_a_script_posts():
    """`/build` takes a raw JSON dict, so numbers and 'false' have to become what
    the dataclass declares instead of raising inside the renderer."""
    async with await _client() as c:
        r = await c.post("/api/ffmpeg/build", json={"video_bitrate": 4000,
                                                    "low_power": "false",
                                                    "rc_mode": "cbr",
                                                    "unknown_field": 1})
    assert r.status_code == 200, r.text
    cmd = r.json()["command"]
    assert "-b:v 4000" in cmd and "-rc_mode CBR" in cmd
    assert "-low_power" not in cmd and "unknown_field" not in cmd
