"""
The DS918+ (Apollo Lake / iHD driver) VAAPI template.

The reference spec command is kept reproducible, extended with the three knobs
that make VAAPI encoding actually fast and deterministic on that silicon:

  * -low_power 1   -> VAEntrypointEncSliceLP (fixed-function H.264 encoder)
  * -rc_mode CQP   -> explicit rate control, quality-pinned (AUTO is
                      driver-dependent, and CQP needs -global_quality)
  * -async_depth 4 -> more frames in flight
  * -map 0:s? / -c:s dvbsub -> keep DVB subtitles when the source has them

These tests pin both directions of the 2-way sync so a GUI edit can never
silently drop them.
"""

from __future__ import annotations

from app.services.ffmpeg_templates import (
    COPY_PRESET_NAME, FFmpegOptions, REDIRECT_COMMAND, REDIRECT_PRESET_NAME, asdict,
    REFERENCE_PRESET_NAME, build_command, default_presets, parse_command,
    serves_original_file, URL_PLACEHOLDER,
)


def test_vaapi_720p_command_is_the_optimised_reference():
    cmd = build_command(FFmpegOptions())
    assert cmd.startswith("ffmpeg ")
    # resilient input flags (spec reference)
    assert "-rw_timeout 10000000" in cmd
    assert "-reconnect 1" in cmd and "-reconnect_at_eof 1" in cmd
    # full GPU decode + scale + encode pipeline (no CPU round-trip)
    assert "-init_hw_device vaapi=intel:/dev/dri/renderD128" in cmd
    assert "-hwaccel vaapi" in cmd
    assert "-hwaccel_output_format vaapi" in cmd
    assert "scale_vaapi=w=1280:h=720:format=nv12" in cmd
    # the optimisations this change adds
    assert "-c:v h264_vaapi" in cmd
    assert "-low_power 1" in cmd
    assert "-rc_mode CQP" in cmd
    assert "-global_quality 26" in cmd
    assert "-async_depth 4" in cmd
    assert "-f mpegts" in cmd and "+resend_headers" in cmd
    assert URL_PLACEHOLDER in cmd


def test_low_power_is_emitted_only_for_h264_vaapi():
    """Apollo Lake has no fixed-function encoder for HEVC (vainfo: HEVC has
    EncSlice but no EncSliceLP), so -low_power must not leak into hevc_vaapi."""
    hevc = build_command(FFmpegOptions(video_codec="hevc_vaapi"))
    assert "-low_power" not in hevc
    assert "-rc_mode CQP" in hevc and "-async_depth 4" in hevc
    # but it is there for h264_vaapi
    assert "-low_power 1" in build_command(FFmpegOptions(video_codec="h264_vaapi"))


def test_software_and_copy_templates_get_no_vaapi_flags():
    sw = build_command(FFmpegOptions(hw_accel="none", video_codec="libx264"))
    assert "-low_power" not in sw and "-rc_mode" not in sw and "-async_depth" not in sw
    assert "-init_hw_device" not in sw
    cp = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                     audio_codec="copy", resolution="source"))
    assert "-c:v copy" in cp and "-low_power" not in cp


def test_low_power_and_rc_mode_are_toggleable():
    off = build_command(FFmpegOptions(low_power=False, rc_mode="auto"))
    assert "-low_power" not in off
    assert "-rc_mode" not in off
    # lowercase GUI leftovers still render as ffmpeg's uppercase aliases
    cbr = build_command(FFmpegOptions(rc_mode="cbr"))
    assert "-rc_mode CBR" in cbr
    assert "-rc_mode CBR" in build_command(FFmpegOptions(rc_mode="CBR"))
    assert "-rc_mode" not in build_command(FFmpegOptions(rc_mode="AUTO"))


def test_cqp_drops_the_rate_flags_the_encoder_would_ignore():
    """CQP is constant quantiser: -b:v/-maxrate/-bufsize do not reach the wire,
    so rendering them would put flags in the command that the encoder ignores -
    and this text is what the user reads in the GUI and pastes into a shell."""
    cqp = build_command(FFmpegOptions(rc_mode="CQP"))
    for flag in ("-b:v", "-maxrate", "-bufsize"):
        assert flag not in cqp, flag
    assert "-global_quality 26" in cqp
    # ...and the rate-driven modes keep the bitrate tuning untouched
    vbr = build_command(FFmpegOptions(rc_mode="VBR"))
    assert "-b:v 1000k" in vbr and "-maxrate 1100k" in vbr and "-bufsize 2000k" in vbr
    assert "-global_quality" not in vbr
    assert "-b:v 1000k" in build_command(FFmpegOptions(rc_mode="CBR"))
    assert "-b:v 1000k" in build_command(FFmpegOptions(rc_mode="AUTO"))


def test_cqp_on_a_non_vaapi_encoder_leaves_the_rate_flags_alone():
    """-rc_mode is a VAAPI option; libx264 and QSV do not understand it, so on
    those templates the rate field is inert and the bitrate stays in charge."""
    for codec, hw in (("libx264", "none"), ("h264_qsv", "qsv")):
        cmd = build_command(FFmpegOptions(hw_accel=hw, video_codec=codec, rc_mode="CQP"))
        assert "-b:v 1000k" in cmd, codec
        assert "-rc_mode" not in cmd and "-global_quality" not in cmd, codec


def test_the_qp_can_be_switched_off_and_only_serves_cqp():
    assert "-global_quality" not in build_command(FFmpegOptions(global_quality=""))
    assert "-global_quality" not in build_command(FFmpegOptions(global_quality="AUTO"))
    assert "-global_quality 20" in build_command(FFmpegOptions(global_quality="20"))
    # a QP on a rate-driven template is stored but not rendered
    assert "-global_quality" not in build_command(FFmpegOptions(rc_mode="VBR", global_quality="20"))


def test_parse_command_recovers_the_qp_field():
    cmd = build_command(FFmpegOptions(global_quality="20"))
    res = parse_command(cmd)
    assert res["options"]["global_quality"] == "20"
    assert res["warnings"] == []
    # it must not fall through to the "unknown flag" bucket, or re-syncing the
    # GUI would append a second -global_quality to the command on every pass
    assert "-global_quality" not in res["options"]["extra_output"]
    # the per-stream alias parses to the same field
    alias = parse_command("ffmpeg -i <url> -c:v h264_vaapi -rc_mode CQP -q:v 30 -f mpegts pipe:1")
    assert alias["options"]["global_quality"] == "30"


def test_parse_command_recovers_the_new_fields():
    cmd = build_command(FFmpegOptions())
    res = parse_command(cmd)
    o = res["options"]
    assert o["hw_accel"] == "vaapi"
    assert o["video_codec"] == "h264_vaapi"
    assert o["low_power"] is True
    assert o["rc_mode"] == "CQP"
    assert o["global_quality"] == "26"
    assert o["async_depth"] == "4"
    # the bitrate tuning survives even though CQP does not render it
    assert o["video_bitrate"] == "1000k"
    assert o["resolution"] == "720p"
    assert res["warnings"] == []


def test_parse_command_normalises_numeric_rc_mode():
    res = parse_command("ffmpeg -i <url> -c:v h264_vaapi -rc_mode 2 -low_power 0 "
                        "-async_depth 8 -f mpegts pipe:1")
    o = res["options"]
    assert o["rc_mode"] == "CBR"
    lower = parse_command("ffmpeg -i <url> -c:v h264_vaapi -rc_mode vbr -f mpegts pipe:1")
    assert lower["options"]["rc_mode"] == "VBR"
    assert o["low_power"] is False
    assert o["async_depth"] == "8"


def test_default_presets_ship_the_optimised_vaapi_commands():
    presets = {p["name"]: p for p in default_presets()}
    vaapi = presets[REFERENCE_PRESET_NAME]
    assert "-low_power 1" in vaapi["command"]
    assert "-rc_mode CQP" in vaapi["command"]
    assert "-global_quality 26" in vaapi["command"]
    assert "-async_depth 4" in vaapi["command"]
    # every template the app owns asks for CQP, and none of them renders a
    # -rc_mode the encoder would ignore
    for name, p in presets.items():
        if p["video_codec"].endswith("_vaapi"):
            assert "-rc_mode CQP" in p["command"], name
            assert "-b:v" not in p["command"], name
        else:
            assert "-rc_mode" not in p["command"], name
    # every preset's stored command must match its structured fields (2-way sync);
    # the redirect preset is exempt: it is a marker, not an ffmpeg command.
    for name, p in presets.items():
        if name == REDIRECT_PRESET_NAME:
            continue
        fields = {k: v for k, v in p.items() if k in FFmpegOptions.__dataclass_fields__}
        assert build_command(FFmpegOptions(**fields)) == p["command"], name


def test_the_two_way_sync_reaches_a_fixed_point():
    """Looking at a template must not change it. build -> parse -> build used to
    drift: the parser skipped the resilience flags without eating their values, so
    every pass appended another '1 1 1' to extra_input and another
    -mpegts_flags to extra_output - and a template opened twice in the GUI
    carried doubled flags into the stream path."""
    cases = [
        {},                                                 # the shipped CQP default
        {"rc_mode": "VBR"},
        {"rc_mode": "AUTO"},
        {"output_format": "hls"},
        {"hw_accel": "none", "video_codec": "libx264"},
        {"hw_accel": "none", "video_codec": "copy", "audio_codec": "copy",
         "resolution": "source"},
        {"extra_input": "-reconnect 0"},
        {"extra_output": "-mpegts_flags +discont_start"},
        {"output_format": "hls", "extra_output": "-hls_time 2"},
        {"extra_input": "-headers", "extra_output": "User-Agent: x"},   # half-typed text
    ]
    for kw in cases:
        c1 = build_command(FFmpegOptions(**kw))
        o1 = parse_command(c1)["options"]
        c2 = build_command(FFmpegOptions(**o1))
        o2 = parse_command(c2)["options"]
        assert o1 == o2, f"{kw}: options still drifting on the second pass"
        assert c2 == build_command(FFmpegOptions(**o2)), f"{kw}: the command grew again"
        if kw != {"rc_mode": "AUTO"}:
            # The one case a stateless parse cannot get back exactly: a command
            # with no -rc_mode does not say whether the mode is AUTO or our
            # default. That is what `base` is for (see the test above), not a
            # duplication bug - and it converges on the second pass either way.
            assert c1 == c2, f"{kw}: {c1}\n -> {c2}"


def test_parse_command_keeps_what_the_command_cannot_say():
    """A command is not a full description of a template: `-rc_mode` absent can
    mean AUTO, and a CQP command carries no bitrate *by design* while the row
    still holds the numbers to switch back to. `base` is the editor's current
    state, so the fields the text is silent about stay as they were."""
    auto = asdict(FFmpegOptions(rc_mode="AUTO"))
    o = parse_command(build_command(FFmpegOptions(**auto)), base=auto)["options"]
    assert o["rc_mode"] == "AUTO"
    assert "-rc_mode" not in build_command(FFmpegOptions(**o))
    # stateless, the same command reads back as our default
    assert parse_command(build_command(FFmpegOptions(**auto)))["options"]["rc_mode"] == "CQP"

    big = asdict(FFmpegOptions(resolution="1080p", video_bitrate="2500k",
                               maxrate="2750k", bufsize="5000k"))
    kept = parse_command(build_command(FFmpegOptions(**big)), base=big)["options"]
    assert (kept["video_bitrate"], kept["maxrate"], kept["bufsize"]) == ("2500k", "2750k", "5000k")

    # a base posted by a script is coerced, not trusted
    loose = parse_command("ffmpeg -i <url> -c:v h264_vaapi -f mpegts pipe:1",
                          base={"low_power": "false", "video_bitrate": 4000, "junk": 1})["options"]
    assert loose["low_power"] is False and loose["video_bitrate"] == "4000"
    assert "junk" not in loose
    # and a base that is not an options dict at all stays harmless
    assert parse_command("ffmpeg -i <url> -c:v h264_vaapi -f mpegts pipe:1",
                         base=["nope"])["options"]["rc_mode"] == "CQP"


def test_a_software_template_does_not_borrow_the_gpu_on_the_second_pass():
    """`scale=` without a _vaapi/_qsv suffix is the CPU path's own signature. The
    parser used to leave hw_accel on the shipped default, so re-syncing a libx264
    template in the editor added -init_hw_device and a scale_vaapi filter to a
    command that had asked for neither - the CPU fallback turning into a GPU job."""
    sw = build_command(FFmpegOptions(hw_accel="none", video_codec="libx264"))
    assert "-init_hw_device" not in sw and "scale_vaapi" not in sw
    back = parse_command(sw)["options"]
    assert back["hw_accel"] == "none"
    assert back["resolution"] == "720p" and back["aspect"] == "16:9"
    assert build_command(FFmpegOptions(**back)) == sw
    # and the two GPU paths still read back as themselves
    assert parse_command(build_command(FFmpegOptions()))["options"]["hw_accel"] == "vaapi"
    qsv = build_command(FFmpegOptions(hw_accel="qsv", video_codec="h264_qsv"))
    assert parse_command(qsv)["options"]["hw_accel"] == "qsv"


def test_a_flag_typed_into_the_extra_args_wins_over_ours():
    """The app's resilience and container flags are defaults, not policy: the
    template that says `-reconnect 0` means it, and the command text shows one
    occurrence of the flag rather than ours and theirs."""
    reconnect = build_command(FFmpegOptions(extra_input="-reconnect 0"))
    assert "-reconnect 0" in reconnect
    assert "-reconnect 1" not in reconnect
    # ...and it survives the round trip instead of being eaten by our own flag
    assert parse_command(reconnect)["options"]["extra_input"] == "-reconnect 0"

    ts = build_command(FFmpegOptions(extra_output="-mpegts_flags +discont_start"))
    assert "-mpegts_flags +discont_start" in ts and "+resend_headers" not in ts
    hls = build_command(FFmpegOptions(output_format="hls", extra_output="-hls_time 2"))
    assert "-hls_time 2" in hls and "-hls_time 6" not in hls
    assert parse_command(hls)["options"]["extra_output"] == "-hls_time 2"


def test_a_passthrough_command_does_not_invent_a_resolution():
    """`-c:v copy` has no scale filter to read a size from, and parsing from
    scratch used to hand back the dataclass default - a 720p the command never
    claimed, which the next render then denied."""
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                      audio_codec="copy", resolution="source"))
    o = parse_command(cmd)["options"]
    assert o["resolution"] == "source"
    assert o["video_bitrate"] == "1000k"      # a field the command does not carry stays put


def test_serves_original_file_covers_redirect_and_copy():
    assert serves_original_file(REDIRECT_COMMAND) is True
    assert serves_original_file("") is True
    copy = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                       audio_codec="copy", resolution="source"))
    assert serves_original_file(copy) is True
    assert serves_original_file(f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1") is True
    vaapi = build_command(FFmpegOptions())
    assert serves_original_file(vaapi) is False
    sw = build_command(FFmpegOptions(hw_accel="none", video_codec="libx264"))
    assert serves_original_file(sw) is False
    presets = {p["name"]: p for p in default_presets()}
    assert serves_original_file(presets[COPY_PRESET_NAME]["command"]) is True
    assert serves_original_file(presets[REDIRECT_PRESET_NAME]["command"]) is True


def test_redirect_preset_is_a_sentinel_not_a_command():
    """The per-channel 'bypass ffmpeg' preset ships as a marker row the stream
    path recognises, not as a real (and bogus) ffmpeg command."""
    presets = {p["name"]: p for p in default_presets()}
    redirect = presets[REDIRECT_PRESET_NAME]
    assert redirect["command"] == REDIRECT_COMMAND
    assert redirect["enabled"] is True
    # still carries the full structured field set so the seeder treats it
    # like any other template
    for f in FFmpegOptions.__dataclass_fields__:
        assert f in redirect, f
    assert build_command(FFmpegOptions()) != REDIRECT_COMMAND


def test_dreambox_preset_targets_mpeg2_ts_for_enigma2():
    """The DM800se (Enigma2/openpli, ancient) needs an MPEG-2 transport stream:
    H.264 Main@3.1 video + MPEG-1 Layer II audio (mp2) at a modest SD bitrate."""
    dreambox = {p["name"]: p for p in default_presets()}[
        "Dreambox DM800se (Enigma2 / MPEG2-SD)"]
    assert dreambox["resolution"] == "576p"
    assert dreambox["video_codec"] == "h264_vaapi"
    assert dreambox["profile"] == "main" and dreambox["level"] == "3.1"
    assert dreambox["audio_codec"] == "mp2"
    assert dreambox["output_format"] == "mpegts"
    assert "-c:v h264_vaapi" in dreambox["command"]
    assert "-c:a mp2" in dreambox["command"]
    assert "-profile:v main" in dreambox["command"]
    assert "scale_vaapi=w=1024:h=576:format=nv12" in dreambox["command"]
    assert "-f mpegts" in dreambox["command"]


def test_persistent_templates_carry_optional_dvb_subtitles():
    """Every real ffmpeg command maps optional DVB subs; redirect is a marker."""
    variants = [
        FFmpegOptions(),
        FFmpegOptions(hw_accel="none", video_codec="libx264"),
        FFmpegOptions(hw_accel="none", video_codec="copy",
                      audio_codec="copy", resolution="source"),
        FFmpegOptions(video_codec="hevc_vaapi"),
        FFmpegOptions(hw_accel="qsv", video_codec="h264_qsv"),
        FFmpegOptions(audio_codec="none"),
    ]
    for opts in variants:
        cmd = build_command(opts)
        toks = cmd.split()
        assert "-sn" not in toks
        assert "-map" in toks and "0:s?" in toks
        assert "-c:s" in toks and "dvbsub" in toks
        assert "-dn" in toks
        parsed = parse_command(cmd)
        assert "-c:s" not in (parsed["options"]["extra_output"] or "")

    for name, p in {p["name"]: p for p in default_presets()}.items():
        if name == REDIRECT_PRESET_NAME:
            assert p["command"] == REDIRECT_COMMAND
            continue
        toks = p["command"].split()
        assert "-sn" not in toks, name
        assert "0:s?" in toks, name
        assert "-c:s" in toks and "dvbsub" in toks, name
        assert "-dn" in toks, name
