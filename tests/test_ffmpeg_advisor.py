"""The FFmpeg optimisation advisor: rules, goals, fixes and their composition.

The properties worth pinning, beyond each rule firing on its own input:

  * the shipped presets stay quiet (an advisor that nags about its own defaults
    is ignored within a day) - the only exceptions are named here;
  * a rule never guesses: without the fact it needs (source fps, GPU
    capabilities, a measurement) it says nothing;
  * every `fix` is something build_command/field_errors accept, and applying
    several fixes composes them instead of letting one silently eat another.
"""

from __future__ import annotations

import pytest

from app.services import ffmpeg_advisor as adv
from app.services.ffmpeg_editor import field_errors
from app.services.ffmpeg_templates import (FFmpegOptions, build_command,
                                           default_presets)


def opts(**kw) -> FFmpegOptions:
    return FFmpegOptions(**kw)


def ids(report) -> set[str]:
    return {f["id"] for f in report["findings"]}


def by_id(report, fid):
    return next(f for f in report["findings"] if f["id"] == fid)


SW = dict(hw_accel="none", video_codec="libx264", video_bitrate="1200k",
          maxrate="1300k", bufsize="2400k", extra_output="-preset veryfast")


# --------------------------------------------------------------------------- #
#  the shipped presets
# --------------------------------------------------------------------------- #
def test_shipped_presets_are_quiet_for_the_balanced_goal():
    for p in default_presets():
        o = adv.as_options({k: v for k, v in p.items() if k in FFmpegOptions.__dataclass_fields__})
        assert adv.advise(o)["findings"] == [], (p["name"], ids(adv.advise(o)))


def test_shipped_presets_raise_nothing_above_a_tip_for_any_goal():
    """Goals add tips (a quality goal wants more bitrate, an internet goal a cap);
    none of them may call a shipped preset broken."""
    for p in default_presets():
        o = adv.as_options({k: v for k, v in p.items() if k in FFmpegOptions.__dataclass_fields__})
        for goal in adv.GOALS:
            bad = [f["id"] for f in adv.advise(o, goal=goal)["findings"]
                   if f["severity"] != "tip"]
            assert not bad, (p["name"], goal, bad)


def test_the_software_preset_ships_a_live_preset_so_it_does_not_trip_its_own_advice():
    sw = next(p for p in default_presets() if p["name"].startswith("Software 720p"))
    assert "-preset veryfast" in sw["command"]


# --------------------------------------------------------------------------- #
#  start / latency
# --------------------------------------------------------------------------- #
def test_long_gop_warns_with_the_two_second_fix():
    r = adv.advise(opts(gop="250", fps="25"))
    f = by_id(r, "gop-long")
    assert f["severity"] == "warn" and f["fix"]["set"] == {"gop": "50"}
    assert "10.0 s" in f["message"]


def test_gop_with_source_fps_assumes_25_and_says_so_but_a_short_one_stays_quiet():
    f = by_id(adv.advise(opts(fps="", gop="250")), "gop-long")
    assert "assuming 25 fps" in f["message"]
    assert "gop-long" not in ids(adv.advise(opts(fps="", gop="50")))


def test_gop_threshold_follows_the_goal():
    o = opts(gop="75", fps="25")            # 3 s
    assert "gop-long" not in ids(adv.advise(o, goal="balanced"))
    assert "gop-long" in ids(adv.advise(o, goal="fast_start"))
    o = opts(gop="125", fps="25")           # 5 s
    assert "gop-long" in ids(adv.advise(o, goal="balanced"))
    assert "gop-long" not in ids(adv.advise(o, goal="quality"))


def test_intra_only_and_default_gop():
    assert "gop-intra" in ids(adv.advise(opts(gop="1")))
    f = by_id(adv.advise(opts(**SW, gop="")), "gop-default")
    assert "250 frames" in f["message"]
    # a VAAPI encoder has no 250-frame default to warn about
    assert "gop-default" not in ids(adv.advise(opts(gop="")))


def test_hls_output_is_critical_because_the_proxy_refuses_it():
    f = by_id(adv.advise(opts(output_format="hls")), "hls-output")
    assert f["severity"] == "critical" and f["fix"]["set"] == {"output_format": "mpegts"}


def test_async_depth_extremes_only_on_vaapi_encoders():
    assert "async-depth-high" in ids(adv.advise(opts(async_depth="16")))
    assert "async-depth-low" in ids(adv.advise(opts(async_depth="1")))
    assert "async-depth-high" not in ids(adv.advise(opts(**SW, async_depth="16")))


def test_large_probe_settings_suggest_the_fast_values():
    f = by_id(adv.advise(opts(extra_input="-analyzeduration 10000000")), "probe-large")
    assert {e["flag"] for e in f["fix"]["extra"]} == {"-analyzeduration", "-probesize"}
    assert "probe-large" not in ids(adv.advise(
        opts(extra_input="-analyzeduration 1000000 -probesize 1000000")))


def test_bufsize_ratio_rules_need_a_rendered_bitrate():
    big = opts(**{**SW, "bufsize": "8000k"})
    f = by_id(adv.advise(big), "bufsize-large")
    assert f["fix"]["set"] == {"bufsize": "2400k"}
    small = opts(**{**SW, "bufsize": "600k"})
    assert "bufsize-small" in ids(adv.advise(small))
    # CQP renders no bitrate at all: nothing to compare against
    assert "bufsize-large" not in ids(adv.advise(opts(bufsize="9000k")))


def test_maxrate_below_bitrate_and_missing_cap():
    f = by_id(adv.advise(opts(**{**SW, "maxrate": "900k"})), "maxrate-low")
    assert f["fix"]["set"] == {"maxrate": "1320k"}     # +10 %
    assert "maxrate-missing" in ids(adv.advise(opts(**{**SW, "maxrate": ""})))
    # quality goal does not care about spikes
    assert "maxrate-missing" not in ids(adv.advise(opts(**{**SW, "maxrate": ""}), goal="quality"))


# --------------------------------------------------------------------------- #
#  speed / load
# --------------------------------------------------------------------------- #
def test_libx264_without_preset_is_a_tip_or_a_warning_by_goal():
    o = opts(**{**SW, "extra_output": ""})
    f = by_id(adv.advise(o), "x264-preset")
    assert f["severity"] == "tip"
    assert f["fix"]["extra"] == [{"side": "output", "flag": "-preset", "value": "veryfast"}]
    assert by_id(adv.advise(o, goal="fast_start"), "x264-preset")["severity"] == "warn"
    assert by_id(adv.advise(o, goal="low_cpu"), "x264-preset")["fix"]["extra"][0]["value"] == "superfast"
    assert by_id(adv.advise(o, goal="quality"), "x264-preset")["fix"]["extra"][0]["value"] == "fast"


def test_slow_and_ultrafast_presets():
    f = by_id(adv.advise(opts(**{**SW, "extra_output": "-preset slow"})), "x264-preset-slow")
    assert f["severity"] == "warn"
    assert "x264-preset-slow" not in ids(adv.advise(
        opts(**{**SW, "extra_output": "-preset medium"}), goal="quality"))
    assert "x264-ultrafast" in ids(adv.advise(opts(**{**SW, "extra_output": "-preset ultrafast"})))
    assert "x264-ultrafast" not in ids(adv.advise(
        opts(**{**SW, "extra_output": "-preset ultrafast"}), goal="low_cpu"))


def test_zerolatency_tip_is_only_for_the_fast_start_goal():
    o = opts(**SW)
    assert "x264-zerolatency" in ids(adv.advise(o, goal="fast_start"))
    assert "x264-zerolatency" not in ids(adv.advise(o))
    tuned = opts(**{**SW, "extra_output": "-preset veryfast -tune zerolatency"})
    assert "x264-zerolatency" not in ids(adv.advise(tuned, goal="fast_start"))


def test_hevc_rules():
    assert by_id(adv.advise(opts(hw_accel="none", video_codec="libx265")),
                 "hevc-software")["fix"]["set"] == {"video_codec": "libx264"}
    f = by_id(adv.advise(opts(video_codec="hevc_vaapi")), "hevc-hardware")
    assert f["fix"]["set"] == {"video_codec": "h264_vaapi"}
    f = by_id(adv.advise(opts(hw_accel="qsv", video_codec="hevc_qsv")), "hevc-hardware")
    assert f["fix"]["set"] == {"video_codec": "h264_qsv"}


def test_low_power_needs_the_gpu_to_say_so():
    env = {"vaapi": {"/dev/dri/renderD128": {"h264_low_power": True, "h264_encode": True}}}
    f = by_id(adv.advise(opts(low_power=False), env=env), "low-power-off")
    assert f["fix"]["set"] == {"low_power": True}
    assert "low-power-off" not in ids(adv.advise(opts(low_power=False)))       # unknown host
    env_no = {"vaapi": {"/dev/dri/renderD128": {"h264_low_power": False, "h264_encode": True}}}
    f = by_id(adv.advise(opts(low_power=True), env=env_no), "low-power-unsupported")
    assert f["severity"] == "critical"
    assert by_id(adv.advise(opts(low_power=False), env=env, goal="low_cpu"),
                 "low-power-off")["severity"] == "warn"


def test_threads_flag_rules():
    f = by_id(adv.advise(opts(extra_output="-threads 4")), "threads-hardware")
    assert f["fix"]["remove"] == [{"side": "output", "flag": "-threads"}]
    o = opts(**{**SW, "extra_output": "-preset veryfast -threads 16"})
    assert "threads-too-many" not in ids(adv.advise(o))                        # cores unknown
    assert "threads-too-many" in ids(adv.advise(o, env={"cpus": 4}))
    assert "threads-too-many" not in ids(adv.advise(o, env={"cpus": 16}))


def test_high_fps_tip_respects_bob_deinterlacing_and_the_quality_goal():
    assert "fps-high" in ids(adv.advise(opts(fps="50")))
    assert "fps-high" not in ids(adv.advise(opts(fps="50"), goal="quality"))
    assert "fps-high" not in ids(adv.advise(opts(fps="50", vf_preset="deint-vaapi-field")))
    assert "fps-high" not in ids(adv.advise(opts(fps="25")))


def test_transcoding_at_source_size_and_fps_suggests_copy():
    f = by_id(adv.advise(opts(resolution="source", fps="")), "transcode-as-source")
    assert f["fix"]["set"] == {"video_codec": "copy"}
    assert "transcode-as-source" not in ids(adv.advise(opts(resolution="source", fps="25")))
    assert "transcode-as-source" not in ids(adv.advise(
        opts(resolution="source", fps="", video_codec="copy")))


def test_host_busy_only_for_software_encodes_and_only_with_evidence():
    sw = opts(**SW)
    f = by_id(adv.advise(sw, env={"cpus": 2, "active_transcodes": 2}), "host-busy")
    assert f["severity"] == "warn" and "2 transcode(s) already run on 2" in f["message"]
    assert "host-busy" in ids(adv.advise(sw, env={"cpus": 4, "load1": 3.9, "active_transcodes": 0}))
    assert "host-busy" not in ids(adv.advise(sw, env={"cpus": 4, "load1": 0.5, "active_transcodes": 1}))
    assert "host-busy" not in ids(adv.advise(sw))
    assert "host-busy" not in ids(adv.advise(opts(), env={"cpus": 1, "active_transcodes": 9}))


def test_live_measurement_makes_a_critical_finding_with_a_concrete_fix():
    live = {"speed": 0.71, "slow": True, "age_s": 300}
    f = by_id(adv.advise(opts(**{**SW, "extra_output": "-preset medium"}), live=live), "live-slow")
    assert f["severity"] == "critical" and "0.71×" in f["message"] and "5 min ago" in f["message"]
    assert f["fix"]["extra"][0] == {"side": "output", "flag": "-preset", "value": "superfast"}
    hw = by_id(adv.advise(opts(resolution="1080p"), live=live), "live-slow")
    assert hw["fix"]["set"] == {"resolution": "720p"}
    ok = {"speed": 1.0, "slow": False, "age_s": 10}
    assert "live-slow" not in ids(adv.advise(opts(), live=ok))
    assert "live-slow" not in ids(adv.advise(opts(video_codec="copy"), live=live))


# --------------------------------------------------------------------------- #
#  quality
# --------------------------------------------------------------------------- #
def test_bits_per_pixel_low_and_high_with_rates_kept_in_proportion():
    starved = opts(**{**SW, "video_bitrate": "400k", "maxrate": "440k", "bufsize": "800k"})
    f = by_id(adv.advise(starved), "bitrate-low")
    assert f["severity"] == "warn"
    fix = f["fix"]["set"]
    assert fix["video_bitrate"] == "1200k"                       # 0.05 bpp (1152k), rounded
    assert float(fix["maxrate"][:-1]) == pytest.approx(float(fix["video_bitrate"][:-1]) * 1.1, abs=2)
    assert float(fix["bufsize"][:-1]) == pytest.approx(float(fix["video_bitrate"][:-1]) * 2, abs=2)
    wasteful = opts(**{**SW, "video_bitrate": "9000k", "maxrate": "", "bufsize": ""})
    assert "bitrate-high" in ids(adv.advise(wasteful))
    assert "bitrate-low" not in ids(adv.advise(opts(**SW)))


def test_bitrate_goal_tip_and_unknown_size():
    mid = opts(**{**SW, "video_bitrate": "1000k"})               # 0.043 bpp at 720p25
    assert "bitrate-low-goal" in ids(adv.advise(mid, goal="quality"))
    assert "bitrate-low-goal" not in ids(adv.advise(mid, goal="balanced"))
    src = opts(**{**SW, "resolution": "source", "video_bitrate": "100k"})
    assert not {"bitrate-low", "bitrate-low-goal"} & ids(adv.advise(src, goal="quality"))


def test_cqp_uncapped_is_an_internet_goal_tip_only():
    assert "cqp-uncapped" not in ids(adv.advise(opts()))
    f = by_id(adv.advise(opts(), goal="internet"), "cqp-uncapped")
    assert f["fix"]["set"] == {"rc_mode": "VBR"}
    assert "cqp-uncapped" not in ids(adv.advise(opts(rc_mode="VBR"), goal="internet"))


def test_quality_number_extremes():
    assert "quality-too-high" in ids(adv.advise(opts(global_quality="12")))
    assert "quality-too-low" in ids(adv.advise(opts(global_quality="42")))
    assert not {"quality-too-high", "quality-too-low"} & ids(adv.advise(opts(global_quality="26")))
    # the number is not rendered at all in VBR: no advice on it
    assert "quality-too-low" not in ids(adv.advise(opts(rc_mode="VBR", global_quality="45")))


def test_audio_rules():
    f = by_id(adv.advise(opts(audio_codec="mp2", audio_channels="6")), "audio-mp2-surround")
    assert f["severity"] == "critical"
    assert by_id(adv.advise(opts(audio_channels="6", audio_bitrate="128k")),
                 "audio-surround-low")["fix"]["set"] == {"audio_bitrate": "384k"}
    assert "audio-surround-low" not in ids(adv.advise(opts(audio_channels="6", audio_bitrate="384k")))
    assert "audio-rate-44k" in ids(adv.advise(opts(audio_rate="44100")))
    assert not {"audio-surround-low", "audio-rate-44k"} & ids(
        adv.advise(opts(audio_codec="copy", audio_channels="6", audio_rate="44100")))


# --------------------------------------------------------------------------- #
#  compatibility: H.264 level arithmetic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("w,h,fps,level", [
    (1920, 1080, 25, "4.0"), (1920, 1080, 30, "4.0"), (1920, 1080, 50, "4.2"),
    (1280, 720, 50, "3.2"), (1280, 720, 25, "3.1"), (1024, 576, 25, "3.1"),
    (3840, 2160, 30, "5.1"),
])
def test_level_needed_matches_the_h264_tables(w, h, fps, level):
    assert adv.level_needed(w, h, fps) == level


def test_level_too_low_is_critical_and_unknown_fps_does_not_false_alarm():
    f = by_id(adv.advise(opts(resolution="1080p", fps="50", level="4.0")), "level-too-low")
    assert f["severity"] == "critical" and f["fix"]["set"] == {"level": "4.2"}
    # the Duo2 preset: 1080p, source fps, level 4.0 must stay quiet
    assert "level-too-low" not in ids(adv.advise(opts(resolution="1080p", fps="", level="4.0")))
    assert "level-too-low" not in ids(adv.advise(opts(resolution="source", fps="50", level="3.0")))
    assert "level-too-low" not in ids(adv.advise(
        opts(hw_accel="none", video_codec="libx265", resolution="1080p", fps="50", level="3.0")))


# --------------------------------------------------------------------------- #
#  this host
# --------------------------------------------------------------------------- #
def _env(**kw):
    base = {"devices": [
        {"path": "/dev/dri/renderD128", "exists": True, "accessible": True},
        {"path": "/dev/dri/renderD129", "exists": False, "accessible": False}],
        "cpus": 4, "encoders": ["libx264", "h264_vaapi", "aac"], "vaapi": {}}
    base.update(kw)
    return base


def test_missing_device_is_critical_and_offers_the_node_that_exists():
    f = by_id(adv.advise(opts(device="/dev/dri/renderD129"), env=_env()), "device-missing")
    assert f["severity"] == "critical" and f["fix"]["set"] == {"device": "/dev/dri/renderD128"}
    none = _env(devices=[{"path": "/dev/dri/renderD128", "exists": False, "accessible": False}])
    f = by_id(adv.advise(opts(), env=none), "device-missing")
    assert f["fix"] is None and "No render device is mapped" in f["message"]
    assert "device-missing" not in ids(adv.advise(opts(), env=_env()))
    assert "device-missing" not in ids(adv.advise(opts(), env=None))
    assert "device-missing" not in ids(adv.advise(opts(video_codec="copy"), env=none))
    assert "device-missing" not in ids(adv.advise(opts(**SW), env=none))


def test_unreadable_device_and_missing_encoder():
    denied = _env(devices=[{"path": "/dev/dri/renderD128", "exists": True, "accessible": False}])
    assert by_id(adv.advise(opts(), env=denied), "device-denied")["severity"] == "critical"
    f = by_id(adv.advise(opts(**SW), env=_env(encoders=["aac"])), "encoder-missing")
    assert "libx264" in f["message"]
    assert "encoder-missing" not in ids(adv.advise(opts(**SW), env=_env(encoders=[])))   # unknown, not missing


def test_vaapi_driver_without_encode_entrypoint():
    env = _env(vaapi={"/dev/dri/renderD128": {"h264_encode": False, "h264_low_power": False}})
    assert "encoder-unsupported" in ids(adv.advise(opts(), env=env))
    env = _env(vaapi={"/dev/dri/renderD128": {"h264_encode": None, "h264_low_power": None}})
    assert "encoder-unsupported" not in ids(adv.advise(opts(), env=env))


# --------------------------------------------------------------------------- #
#  goals, dismissal, ordering, robustness
# --------------------------------------------------------------------------- #
def test_goal_and_ignored_sanitising():
    assert adv.clean_goal("FAST_START") == "fast_start"
    assert adv.clean_goal("nonsense") == "balanced" and adv.clean_goal(None) == "balanced"
    assert adv.clean_ignored(" gop-long, GOP-long ,x264-preset;drop,,a") == "gop-long,a"
    assert adv.clean_ignored(["a", "b", "a"]) == "a,b"
    assert len(adv.clean_ignored(",".join(f"r{i}" for i in range(200))).split(",")) == 64


def test_dismissed_findings_stay_in_the_list_but_leave_the_counts():
    o = opts(gop="250")
    r = adv.advise(o, ignored="gop-long")
    f = by_id(r, "gop-long")
    assert f["ignored"] is True
    assert r["counts"] == {"critical": 0, "warn": 0, "tip": 0}
    assert adv.advise(o)["counts"]["warn"] == 1


def test_findings_are_ordered_critical_first_and_well_formed():
    r = adv.advise(opts(output_format="hls", gop="250", fps="50", async_depth="16"))
    sev = [f["severity"] for f in r["findings"]]
    assert sev == sorted(sev, key=adv._SEV_RANK.__getitem__)
    for f in r["findings"]:
        assert set(f) >= {"id", "severity", "axis", "fields", "message", "why", "fix", "ignored"}
        assert f["severity"] in adv.SEVERITIES and f["fields"]


def test_a_crashing_rule_does_not_hide_the_others(monkeypatch):
    def boom(c):
        raise RuntimeError("bug")
    monkeypatch.setattr(adv, "RULES", [boom, *adv.RULES])
    assert "gop-long" in ids(adv.advise(opts(gop="250")))


def test_garbage_option_values_never_raise():
    r = adv.advise({"gop": "abc", "fps": "x", "video_bitrate": "??", "level": "9.9",
                    "resolution": "nope", "async_depth": "", "global_quality": "q",
                    "audio_channels": "", "extra_output": "-preset 'unterminated"})
    assert isinstance(r["findings"], list) and set(r["scores"]) >= {"quality", "start", "efficiency"}


# --------------------------------------------------------------------------- #
#  every fix is acceptable to the editor, and they compose
# --------------------------------------------------------------------------- #
def test_every_suggested_fix_yields_valid_fields_and_a_renderable_command():
    cases = [
        opts(output_format="hls", gop="250", fps="50", async_depth="16", low_power=False,
             extra_input="-analyzeduration 10000000", audio_channels="6", audio_rate="44100",
             global_quality="12", resolution="1080p", level="4.0"),
        opts(**{**SW, "extra_output": "", "video_bitrate": "300k", "bufsize": "9000k",
                "maxrate": "200k", "gop": ""}),
        opts(video_codec="hevc_vaapi", extra_output="-threads 8"),
    ]
    for o in cases:
        r = adv.advise(o, goal="fast_start", env={"cpus": 2})
        fixable = [f["id"] for f in r["findings"] if f["fix"]]
        res = adv.apply_selected(o, fixable, goal="fast_start", env={"cpus": 2})
        after = adv.as_options(res["options"])
        assert field_errors(after) == [], (fixable, field_errors(after), res["skipped"])
        build_command(after)


def test_applying_the_fixes_actually_removes_what_they_were_about():
    o = opts(gop="250", fps="25", async_depth="16", audio_rate="44100")
    before = ids(adv.advise(o))
    chosen = [f["id"] for f in adv.advise(o)["findings"] if f["fix"]]
    res = adv.apply_selected(o, chosen)
    after = ids(adv.advise(adv.as_options(res["options"])))
    assert {"gop-long", "async-depth-high", "audio-rate-44k"} <= before
    assert not ({"gop-long", "async-depth-high", "audio-rate-44k"} & after)
    changed = {c["field"] for c in res["changes"]}
    assert changed == {"gop", "async_depth", "audio_rate"}
    assert all(c["from"] != c["to"] and c["ids"] for c in res["changes"])


def test_extra_flag_fixes_compose_instead_of_overwriting_each_other():
    o = opts(**{**SW, "extra_output": ""})
    res = adv.apply_selected(o, ["x264-preset", "x264-zerolatency"], goal="fast_start")
    out = res["options"]["extra_output"]
    assert "-preset veryfast" in out and "-tune zerolatency" in out
    assert res["skipped"] == []
    assert [c["field"] for c in res["changes"]] == ["extra_output"]


def test_conflicting_fixes_are_skipped_and_reported_not_merged():
    # bitrate-low rewrites bitrate+maxrate+bufsize; bufsize-large rewrites bufsize
    o = opts(**{**SW, "video_bitrate": "300k", "maxrate": "330k", "bufsize": "6000k"})
    r = adv.advise(o)
    assert {"bitrate-low", "bufsize-large"} <= ids(r)
    res = adv.apply_selected(o, ["bitrate-low", "bufsize-large"])
    assert [s["id"] for s in res["skipped"]] == ["bufsize-large"]
    assert "conflicts with 'bitrate-low'" in res["skipped"][0]["reason"]
    assert res["options"]["video_bitrate"] == "1200k"


def test_findings_without_a_fix_and_unknown_ids_are_reported():
    o = opts(hw_accel="none", video_codec="libx264", video_bitrate="1200k", maxrate="1300k",
             bufsize="2400k", extra_output="-preset veryfast")
    env = {"devices": [{"path": "/dev/dri/renderD128", "exists": True, "accessible": True}],
           "encoders": ["aac"]}
    res = adv.apply_selected(o, ["encoder-missing", "does-not-exist"], env=env)
    assert res["skipped"] == [{"id": "encoder-missing", "reason": "no automatic fix for this finding"}]
    assert res["changes"] == []


def test_a_fix_the_editor_would_reject_is_skipped_with_its_reason(monkeypatch):
    def bad_fix(c):
        return adv._f("bad", "tip", "load", ["extra_output"], "m", "w",
                      extra=[{"side": "output", "flag": "-preset", "value": ""}])
    monkeypatch.setattr(adv, "RULES", [bad_fix])
    res = adv.apply_selected(opts(), ["bad"])
    assert res["changes"] == [] and "requires a value" in res["skipped"][0]["reason"]


# --------------------------------------------------------------------------- #
#  scores
# --------------------------------------------------------------------------- #
def test_copy_scores_full_marks_and_hardware_beats_software_on_load():
    c = adv.advise(opts(video_codec="copy"))["scores"]
    assert (c["quality"], c["efficiency"], c["basis"]) == (100, 100, "copy")
    hw = adv.advise(opts())["scores"]["efficiency"]
    sw = adv.advise(opts(**{**SW, "extra_output": ""}))["scores"]["efficiency"]
    assert hw > sw


def test_scores_move_in_the_expected_direction():
    base = adv.advise(opts(**SW))["scores"]
    slow = adv.advise(opts(**{**SW, "extra_output": "-preset slow"}))["scores"]
    assert slow["efficiency"] < base["efficiency"] and slow["quality"] > base["quality"]
    long_gop = adv.advise(opts(**{**SW, "gop": "250"}))["scores"]
    assert long_gop["start"] < base["start"]
    hls = adv.advise(opts(output_format="hls"))["scores"]
    assert hls["start"] < adv.advise(opts())["scores"]["start"]
    starved = adv.advise(opts(**{**SW, "video_bitrate": "300k"}))["scores"]
    assert starved["quality"] < base["quality"]
    for s in (base, slow, long_gop, hls, starved):
        assert all(0 <= s[k] <= 100 for k in ("quality", "start", "efficiency"))


# --------------------------------------------------------------------------- #
#  helpers used by the live-speed log line
# --------------------------------------------------------------------------- #
def test_transcode_command_detection_and_hints():
    assert adv.is_transcode_command("ffmpeg -i <url> -c:v libx264 -c:a aac pipe:1")
    assert adv.is_transcode_command("ffmpeg -i <url> -c:v h264_vaapi -f mpegts pipe:1")
    assert not adv.is_transcode_command("ffmpeg -i <url> -c:v copy -c:a copy pipe:1")
    assert not adv.is_transcode_command("@redirect") and not adv.is_transcode_command(None)
    assert "-preset" in adv.hint_for_command("ffmpeg -i <url> -c:v libx264 pipe:1")
    assert "veryfast" in adv.hint_for_command("ffmpeg -i <url> -c:v libx264 -preset slow pipe:1")
    assert "libx265" in adv.hint_for_command("ffmpeg -i <url> -c:v libx265 pipe:1")
    assert "source" in adv.hint_for_command("ffmpeg -i <url> -c:v h264_vaapi pipe:1")


def test_rate_helpers_round_trip():
    assert adv.rate_bps("2.5M") == 2_500_000 and adv.rate_bps("800k") == 800_000
    assert adv.rate_bps("x") is None and adv.rate_bps("") is None
    assert adv.fmt_rate(2_500_000) == "2500k" and adv.fmt_rate(1_150_000) == "1150k"
