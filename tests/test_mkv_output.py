"""
Matroska output + the `keep` subtitle mode - the VOD/series subtitle path.

Why this exists: MPEG-TS has no slot for text subtitles (SRT/ASS), and ffmpeg
cannot turn text into a bitmap track without rendering it into the picture -
which needs CPU video frames and would defeat hardware-only transcoding. The
only way to deliver VOD/series subtitles from this proxy is therefore to change
the CONTAINER: mux into Matroska and copy every subtitle track through, while
the video is either passed through or re-encoded on the GPU.

Consumer this was built for: an Enigma2 box (Vu+ Duo2 / OpenPLi) playing the
stream with ServiceApp/exteplayer3 (bouquet service reference 5002), which
shows exactly those copied tracks in its subtitle menu.
"""

from __future__ import annotations

from app.services import stream_manager as sm
from app.services.ffmpeg_templates import (
    E2_DUO2_LIVE_PRESET_NAME, E2_VOD_REMUX_PRESET_NAME,
    E2_VOD_TRANSCODE_PRESET_NAME, FFmpegOptions, URL_PLACEHOLDER,
    build_command, coerce_options, default_presets, option_warnings,
    parse_command,
)
from app.services.stream_manager import StreamManager

VA = dict(hw_accel="vaapi", video_codec="h264_vaapi")
MKV = dict(output_format="matroska", subs="keep")


def _specs(args):
    return [args[i + 1] for i, t in enumerate(args) if t == "-map"]


async def _gate(cmd, url, subs, pace=True):
    args = StreamManager._ffmpeg_argv(cmd, url, pace=pace)

    async def fake_probe(target, *, is_url):
        return subs

    orig = sm.subtitle_streams
    sm.subtitle_streams = fake_probe
    try:
        return await StreamManager()._subs_gate(args, url, pace, "Test")
    finally:
        sm.subtitle_streams = orig


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
def test_keep_copies_every_subtitle_track_into_matroska():
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                      audio_codec="copy", resolution="source", **MKV))
    assert "-map 0:s?" in cmd and "-c:s copy" in cmd
    assert "-f matroska" in cmd and "-live 1" in cmd and cmd.endswith("pipe:1")
    assert "-sn" not in cmd and "-mpegts_flags" not in cmd


def test_keep_is_hardware_safe_video_stays_on_the_gpu():
    """The point of the mode: the subtitle tracks ride along a video pipeline
    that never leaves VAAPI - no hwdownload, no subtitles= burn filter."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", audio_codec="ac3", **MKV))
    assert "scale_vaapi" in cmd and "-c:v h264_vaapi" in cmd
    assert "hwdownload" not in cmd and "subtitles=" not in cmd
    assert "-c:s copy" in cmd and "-f matroska" in cmd


def test_keep_degrades_to_dvb_on_mpegts_instead_of_lying():
    """MPEG-TS cannot carry SRT/ASS. Rendering `-c:s copy` there would abort at
    the muxer, so the command comes out as the bitmap-only DVB mode - and says
    so when it is parsed back."""
    cmd = build_command(FFmpegOptions(**VA, subs="keep"))       # mpegts default
    assert "-c:s dvbsub" in cmd and "-f mpegts" in cmd
    assert parse_command(cmd)["options"]["subs"] == "dvb"
    assert any("Matroska" in w for w in
               option_warnings(FFmpegOptions(**VA, subs="keep")))
    assert option_warnings(FFmpegOptions(**VA, **MKV)) == []


def test_two_way_sync_fixed_point_for_matroska():
    for opts in (FFmpegOptions(hw_accel="none", video_codec="copy",
                               audio_codec="copy", resolution="source", **MKV),
                 FFmpegOptions(**VA, resolution="1080p", audio_codec="ac3", **MKV)):
        cmd = build_command(opts)
        parsed = parse_command(cmd)["options"]
        assert parsed["output_format"] == "matroska"
        assert parsed["subs"] == "keep"
        assert build_command(FFmpegOptions(**parsed)) == cmd


def test_parse_reads_hand_written_matroska_commands():
    r = parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -map 0:v:0 -map 0:a:0? -map 0:s? "
                      "-c:v copy -c:a copy -c:s copy -f mkv pipe:1")
    assert r["options"]["output_format"] == "matroska"
    assert r["options"]["subs"] == "keep"
    # the same maps in a TS command are the bitmap mode, not this one
    assert parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -map 0:s? -c:s copy "
                         "-f mpegts pipe:1")["options"]["subs"] == "dvb"


def test_coerce_normalises_the_container_and_the_new_mode():
    assert coerce_options({"output_format": "MKV"})["output_format"] == "matroska"
    assert coerce_options({"output_format": "Matroska"})["output_format"] == "matroska"
    assert coerce_options({"output_format": "avi"})["output_format"] == "mpegts"
    assert coerce_options({"subs": "KEEP"})["subs"] == "keep"


# --------------------------------------------------------------------------- #
# built-in Enigma2 presets
# --------------------------------------------------------------------------- #
def test_enigma2_presets_are_shipped_and_fit_the_duo2():
    presets = {p["name"]: p for p in default_presets()}

    remux = presets[E2_VOD_REMUX_PRESET_NAME]
    assert remux["video_codec"] == "copy" and remux["audio_codec"] == "copy"
    # VOD is Matroska so SRT/ASS survive on exteplayer3; MPEG-TS+dvbsub is
    # the 502/no-data failure on the box.
    assert remux["output_format"] == "matroska" and remux["subs"] == "keep"
    assert "-f matroska" in remux["command"] and "-c:s copy" in remux["command"]
    assert "-c:s dvbsub" not in remux["command"] and "-f mpegts" not in remux["command"]

    hw = presets[E2_VOD_TRANSCODE_PRESET_NAME]
    # the 4K/HEVC rescue path: GPU video, box-friendly audio, Matroska + copy subs
    assert hw["video_codec"] == "h264_vaapi" and hw["resolution"] == "1080p"
    assert hw["profile"] == "high" and hw["level"] == "4.0"
    assert hw["audio_codec"] == "ac3" and hw["output_format"] == "matroska"
    assert hw["subs"] == "keep"
    assert "-f matroska" in hw["command"] and "-c:s copy" in hw["command"]
    assert "-c:s dvbsub" not in hw["command"]
    assert "libx264" not in hw["command"] and "subtitles=" not in hw["command"]

    live = presets[E2_DUO2_LIVE_PRESET_NAME]
    # live stays MPEG-TS: the box renders DVB bitmap subs natively there
    assert live["output_format"] == "mpegts" and live["subs"] == "dvb"
    assert "-f mpegts" in live["command"] and "-c:s dvbsub" in live["command"]


# --------------------------------------------------------------------------- #
# spawn-time gate
# --------------------------------------------------------------------------- #
async def test_gate_leaves_text_subtitles_alone_for_matroska():
    """The dvb gate drops SRT/ASS (a TS cannot take them). With a Matroska
    output there is nothing to drop - that is the whole feature."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = await _gate(cmd, "/media/movie.mkv",
                       [{"index": 2, "codec": "subrip"},
                        {"index": 3, "codec": "ass"},
                        {"index": 4, "codec": "hdmv_pgs_subtitle"}])
    assert "0:s?" in _specs(args)              # optional map kept as rendered
    assert args[args.index("-c:s") + 1] == "copy"
    assert "-sn" not in args


async def test_gate_maps_around_codecs_matroska_cannot_hold():
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = await _gate(cmd, "/media/movie.mkv",
                       [{"index": 2, "codec": "dvb_teletext"},
                        {"index": 3, "codec": "subrip"}])
    assert _specs(args) == ["0:v:0", "0:a:0?", "0:s:1"]      # teletext skipped
    assert args[args.index("-c:s") + 1] == "copy"


async def test_gate_drops_when_only_unsupported_tracks_exist():
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = await _gate(cmd, "/media/movie.mkv", [{"index": 2, "codec": "dvb_teletext"}])
    assert "-c:s" not in args and "-sn" in args


async def test_gate_keeps_the_optional_map_when_the_probe_fails():
    """`-map 0:s?` is optional, so an unprobeable source costs nothing: no
    subtitle track simply means no subtitle track, not a dead stream."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    for probe in (None, []):
        args = await _gate(cmd, "http://cdn/movie.mkv", probe)
        assert "0:s?" in _specs(args) and args[args.index("-c:s") + 1] == "copy"


async def test_gate_does_not_probe_live_matroska():
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = StreamManager._ffmpeg_argv(cmd, "http://cdn/live.ts", pace=False)

    async def fail_probe(target, *, is_url):
        raise AssertionError("live must not be probed")

    orig = sm.subtitle_streams
    sm.subtitle_streams = fail_probe
    try:
        out = await StreamManager()._subs_gate(args, "http://cdn/live.ts", False, "Test")
    finally:
        sm.subtitle_streams = orig
    assert out == args


def test_live_play_of_vod_mkv_template_becomes_mpegts():
    """exteplayer3 is audio-only on a live MKV pipe. Assigned to a live
    channel, the VOD MKV template must still produce a picture."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", audio_codec="ac3", **MKV))
    live = StreamManager._ffmpeg_argv(cmd, "http://cdn/live.ts", title="F1 - Olav",
                                      pace=False)
    vod = StreamManager._ffmpeg_argv(cmd, "http://cdn/movie.mkv", title="F1 - Olav",
                                     pace=True)
    assert live[live.index("-f") + 1] == "mpegts"
    assert "-live" not in live
    assert "-c:s" not in live and "-sn" in live
    assert vod[vod.index("-f") + 1] == "matroska"
    assert "-c:s" in vod
    # title with a minus stays one argv value, not extra flags
    assert live[live.index("-metadata") + 1] == "title=F1 - Olav"
    assert vod[vod.index("-metadata") + 1] == "title=F1 - Olav"


# --------------------------------------------------------------------------- #
# the Dutch (nl) / English check for multiple-subtitle VODs
# --------------------------------------------------------------------------- #
async def test_probe_captures_subtitle_languages_for_the_nl_en_check(monkeypatch):
    """The gate's verdict can only be as good as the probe: the (dut)/(eng)
    tag ffmpeg prints after the stream index must survive the parse — and a
    track without a tag must come back as lang=None, not crash."""
    from app.services import probe as probe_svc
    from app.services.probe import subtitle_streams

    banner = (
        "Input #0, matroska,webm, from 'movie.mkv':\n"
        "  Stream #0:2(fre): Subtitle: subrip\n"
        "  Stream #0:3(dut): Subtitle: subrip (default)\n"
        "  Stream #0:4(nld): Subtitle: ass\n"
        "  Stream #0:5[0x810]: Subtitle: dvb_subtitle\n"
        "Output #0, null, from 'pipe:1':\n"
    ).encode()

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"", banner)

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(probe_svc.asyncio, "create_subprocess_exec", fake_exec)
    subs = await subtitle_streams("/tmp/lang-probe-movie.mkv", is_url=False)
    assert subs == [
        {"index": 2, "codec": "subrip", "lang": "fre"},
        {"index": 3, "codec": "subrip", "lang": "dut"},
        {"index": 4, "codec": "ass", "lang": "nld"},
        {"index": 5, "codec": "dvb_subtitle", "lang": None},
    ]


async def test_mkv_gate_confirms_nl_or_en_when_the_source_tags_them(monkeypatch):
    """A VOD with multiple tracks: if Dutch or English is among them it IS in
    the Matroska output (-map 0:s? copies every track) — and the stream log
    says so, accepting ISO-639-1/2 and BCP47 spellings alike."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, message))

    monkeypatch.setattr(sm, "db_log", fake_log)
    args = await _gate(cmd, "/media/movie.mkv", [
        {"index": 2, "codec": "subrip", "lang": "fr"},
        {"index": 3, "codec": "subrip", "lang": "nl-NL"},
        {"index": 4, "codec": "ass", "lang": "en-GB"},
    ])
    assert "0:s?" in _specs(args)              # the check observes, never filters
    hits = [m for lv, m in logged if "includes Dutch/English" in m]
    assert hits, f"no inclusion verdict logged: {logged}"
    assert "nl" in hits[0] and "en" in hits[0]
    assert all(lv == "INFO" for lv, m in logged if "includes Dutch/English" in m)


async def test_mkv_gate_warns_when_neither_nl_nor_en_is_present(monkeypatch):
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, message))

    monkeypatch.setattr(sm, "db_log", fake_log)
    args = await _gate(cmd, "/media/movie.mkv", [
        {"index": 2, "codec": "subrip", "lang": "fr"},
        {"index": 3, "codec": "ass", "lang": "de"},
        {"index": 4, "codec": "subrip", "lang": "it"},
    ])
    assert "0:s?" in _specs(args)              # still copies them all
    warns = [m for lv, m in logged if lv == "WARNING" and "no Dutch (nl) or English" in m]
    assert warns, f"missing nl/en WARNING: {logged}"
    assert "fr" in warns[0] and "de" in warns[0]


async def test_mkv_gate_stays_quiet_for_a_single_subtitle_track(monkeypatch):
    """Scoped to MULTIPLE subtitles: one track means no menu to choose from,
    so the check does not spam the log for every ordinary play."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, message))

    monkeypatch.setattr(sm, "db_log", fake_log)
    args = await _gate(cmd, "/media/movie.mkv",
                       [{"index": 2, "codec": "subrip", "lang": "fr"}])
    assert "0:s?" in _specs(args)
    assert logged == [], f"single-track play must log no nl/en verdict: {logged}"


async def test_mkv_gate_reports_untagged_languages_as_unverifiable(monkeypatch):
    """Persisted pre-language metadata (or a source that ships its tracks
    untagged) must not read as a confident 'missing': it says so instead."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, message))

    monkeypatch.setattr(sm, "db_log", fake_log)
    await _gate(cmd, "/media/movie.mkv",
                [{"index": 2, "codec": "subrip"},        # old shape: no lang key
                 {"index": 3, "codec": "ass", "lang": None}])
    infos = [m for lv, m in logged if "untagged" in m and "cannot verify" in m]
    assert infos, f"missing untagged verdict: {logged}"
    assert not any("no Dutch (nl)" in m for _lv, m in logged), (
        "untagged tracks must not be reported as definitively missing nl/en")


async def test_mkv_gate_flags_when_all_tracks_were_dropped(monkeypatch):
    """Teletext-only source: nothing survives into the Matroska, so nl/en is
    definitively NOT included — the verdict says exactly that (multiple subs,
    so in scope)."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, message))

    monkeypatch.setattr(sm, "db_log", fake_log)
    args = await _gate(cmd, "/media/movie.mkv", [
        {"index": 2, "codec": "dvb_teletext", "lang": "nl"},
        {"index": 3, "codec": "eia_608", "lang": "en"},
    ])
    assert "-sn" in args                        # existing drop behaviour intact
    warns = [m for lv, m in logged
             if lv == "WARNING" and "no Dutch (nl) or English" in m]
    assert warns and "dropped" in warns[0], f"missing dropped-verdict: {logged}"


# --------------------------------------------------------------------------- #
# HTTP surface: the .mkv aliases
# --------------------------------------------------------------------------- #
async def test_mkv_urls_exist_next_to_the_ts_ones():
    """Same items, same auth, same pipeline - only the announced container
    differs. Enigma2 bouquets point VOD/series at `.mkv` because set-top boxes
    sniff the extension; the routing must therefore exist for every kind."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.main import app, _seed_defaults
    from app.models import (
        FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource, Portal, User,
    )

    await _seed_defaults()
    async with SessionLocal() as s:
        tpl = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == E2_VOD_REMUX_PRESET_NAME))).scalar_one()
        assert tpl.is_builtin is True and tpl.output_format == "matroska"
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        src = LiveSource(portal_id=portal.id, portal_channel_id="1",
                         original_name="Ch", cmd="ffmpeg http://x/1.ts", enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Ch", enabled=True, ffmpeg_template_id=tpl.id)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        s.add(User(name="mkv", password="pw", enabled=True, m3u_enabled=True))
        await s.commit()
        pid = pl.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        # dead portal -> 404 "no available source", but the ROUTE resolved and
        # authenticated (a missing route would be 404 with a FastAPI detail, and
        # bad credentials would be 403)
        r = await c.get(f"/play/live/{pid}.mkv?u=mkv&p=pw")
        assert r.status_code == 404 and "no available source" in r.text
        assert (await c.get(f"/play/vod/1.mkv?u=mkv&p=pw")).status_code in (404, 502)
        assert (await c.get(f"/play/episode/1.mkv?u=mkv&p=pw")).status_code in (404, 502)
        assert (await c.get(f"/movie/mkv/pw/1.mkv")).status_code in (404, 502)
        assert (await c.get(f"/series/mkv/pw/1.mkv")).status_code in (404, 502)
        # credentials are still enforced on the new routes
        assert (await c.get(f"/play/live/{pid}.mkv?u=mkv&p=wrong")).status_code == 403
