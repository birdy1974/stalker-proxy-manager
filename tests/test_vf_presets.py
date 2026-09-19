"""
The selectable extra video filters (fault-finding bank).

Each `vf_preset` splices a literal ffmpeg snippet FIRST into the -vf chain so
one suspect at a time can be isolated on a misbehaving box (interlaced source,
10-bit pixels, wrong field flags, broken surface handling). These tests pin the
table, the per-decode-path rendering, the warnings, and both directions of the
2-way sync for every preset on every decode path.
"""

from __future__ import annotations

import shlex

from app.services.ffmpeg_templates import (
    VF_PRESETS, FFmpegOptions, REDIRECT_PRESET_NAME, build_command,
    coerce_options, default_presets, option_warnings, parse_command,
    vf_preset_by_id, vf_snippet,
)


def _vf(cmd: str) -> str:
    toks = shlex.split(cmd)
    return toks[toks.index("-vf") + 1]


def _hw_opts(hw: str) -> dict:
    if hw == "vaapi":
        return {"hw_accel": "vaapi", "video_codec": "h264_vaapi"}
    if hw == "qsv":
        return {"hw_accel": "qsv", "video_codec": "h264_qsv"}
    return {"hw_accel": "none", "video_codec": "libx264"}


def test_the_bank_holds_none_plus_ten_or_more_filters():
    ids = [p.id for p in VF_PRESETS]
    assert ids[0] == "none"
    assert len(ids) >= 11, ids
    assert len(set(ids)) == len(ids)          # ids unique
    assert all(vf_snippet("none", hw) == "" for hw in ("vaapi", "qsv", "none"))


def test_none_is_the_default_and_renders_nothing_new():
    cmd = build_command(FFmpegOptions())
    assert _vf(cmd) == "scale_vaapi=w=1280:h=720:format=nv12,fps=25,setsar=1"
    # every shipped template is none, so all stored commands are byte-identical
    # to before the field existed (the existing 2-way-sync test re-pins that)
    for p in default_presets():
        assert p["vf_preset"] == "none", p["name"]
        for preset in VF_PRESETS:
            for snip in (preset.vaapi, preset.qsv, preset.sw):
                if snip:
                    assert snip not in p["command"], (p["name"], preset.id)
    redirect = {p["name"]: p for p in default_presets()}[REDIRECT_PRESET_NAME]
    assert redirect["vf_preset"] == "none"


def test_each_preset_renders_first_in_chain_on_its_decode_paths():
    for preset in VF_PRESETS:
        if preset.id == "none":
            continue
        for hw in ("vaapi", "qsv", "none"):
            snip = vf_snippet(preset.id, hw)
            if not snip:
                continue                      # N/A there (covered below)
            vf = _vf(build_command(FFmpegOptions(**_hw_opts(hw), vf_preset=preset.id)))
            assert vf.startswith(snip + ","), (preset.id, hw, vf)
            assert vf.endswith(",setsar=1"), (preset.id, hw, vf)


def test_key_snippets_are_the_documented_filter_spellings():
    assert vf_snippet("deint-vaapi-frame", "vaapi") == "deinterlace_vaapi=rate=frame"
    assert vf_snippet("deint-vaapi-bob", "vaapi") == "deinterlace_vaapi=mode=bob:rate=field"
    assert vf_snippet("deint-qsv-advanced", "qsv") == "vpp_qsv=deinterlace=2"
    assert vf_snippet("deint-qsv-bob", "qsv") == "vpp_qsv=deinterlace=1"
    assert vf_snippet("yadif-frame", "none") == "yadif=mode=send_frame:parity=auto"
    assert vf_snippet("bwdif-bob", "none") == "bwdif=mode=send_field:parity=auto"
    assert vf_snippet("hw-roundtrip", "vaapi") == "hwdownload,hwupload"
    assert vf_snippet("pixfmt-420p", "vaapi") == "hwdownload,format=yuv420p,hwupload"
    assert vf_snippet("setfield-prog", "qsv") == "setfield=mode=prog"
    assert vf_snippet("null", "vaapi") == "null"
    assert vf_preset_by_id("nope") is None
    assert vf_snippet("nope", "vaapi") == ""


def test_software_filters_wrap_in_a_cpu_round_trip_on_gpu_paths():
    wrapped = _vf(build_command(FFmpegOptions(vf_preset="yadif-frame")))
    assert wrapped.startswith(
        "hwdownload,format=yuv420p,yadif=mode=send_frame:parity=auto,hwupload,"
        "scale_vaapi="), wrapped
    qsv = _vf(build_command(FFmpegOptions(
        **_hw_opts("qsv"), vf_preset="bwdif-frame")))
    assert qsv.startswith("hwdownload,format=yuv420p,bwdif=mode=send_frame:parity=auto,"
                          "hwupload,scale_qsv="), qsv
    # ... and run bare on software frames, where there is nothing to download
    sw = _vf(build_command(FFmpegOptions(
        **_hw_opts("none"), vf_preset="yadif-frame")))
    assert sw.startswith("yadif=mode=send_frame:parity=auto,scale="), sw
    assert "hwdownload" not in sw and "hwupload" not in sw


def test_decode_path_mismatches_render_nothing_and_warn():
    cases = [
        ("deint-vaapi-frame", "none", "VAAPI decoding"),
        ("deint-vaapi-frame", "qsv", "VAAPI decoding"),
        ("deint-qsv-advanced", "vaapi", "Quick Sync (QSV) decoding"),
        ("deint-qsv-bob", "none", "Quick Sync (QSV) decoding"),
        ("hw-roundtrip", "none", "GPU decoding (VAAPI or Quick Sync)"),
        ("pixfmt-420p", "none", "GPU decoding (VAAPI or Quick Sync)"),
    ]
    for pid, hw, need in cases:
        assert vf_snippet(pid, hw) == "", (pid, hw)
        opts = FFmpegOptions(**_hw_opts(hw), vf_preset=pid)
        assert "deinterlace" not in build_command(opts), (pid, hw)
        assert "hwdownload" not in build_command(opts), (pid, hw)
        notes = option_warnings(opts)
        assert any(need in n and "ignored" in n for n in notes), (pid, hw, notes)


def test_a_preset_on_copy_and_an_unknown_id_warn_and_render_nothing():
    cp = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                     audio_codec="copy", resolution="source",
                                     vf_preset="deint-vaapi-frame"))
    assert "-vf" not in cp
    notes = option_warnings(FFmpegOptions(video_codec="copy",
                                          vf_preset="deint-vaapi-frame"))
    assert any("copy/passthrough" in n for n in notes), notes

    unk = FFmpegOptions(vf_preset="fancy-new-filter")
    assert _vf(build_command(unk)).startswith("scale_vaapi="), build_command(unk)
    notes = option_warnings(unk)
    assert any("unknown video filter preset" in n for n in notes), notes
    # coercion lowercases but keeps the id (the GUI shows it as a custom value)
    assert coerce_options({"vf_preset": "  Yadif-Frame "})["vf_preset"] == "yadif-frame"
    assert coerce_options({"vf_preset": ""})["vf_preset"] == "none"


def test_cpu_and_bob_combinations_are_flagged():
    gpu = FFmpegOptions(vf_preset="yadif-frame")
    assert any("through the CPU" in n for n in option_warnings(gpu))
    sw = FFmpegOptions(**_hw_opts("none"), vf_preset="yadif-frame")
    assert not any("through the CPU" in n for n in option_warnings(sw)), \
        option_warnings(sw)
    bob25 = FFmpegOptions(vf_preset="deint-vaapi-field", fps="25")
    assert any("FPS 50" in n for n in option_warnings(bob25)), option_warnings(bob25)
    bob50 = FFmpegOptions(vf_preset="deint-vaapi-field", fps="50")
    assert not any("halves it back" in n for n in option_warnings(bob50))
    plain = FFmpegOptions(vf_preset="deint-vaapi-frame", fps="25")
    assert not any("halves it back" in n for n in option_warnings(plain))
    assert option_warnings(FFmpegOptions()) == []


def test_two_way_sync_reaches_a_fixed_point_for_every_preset_on_every_path():
    for preset in VF_PRESETS:
        for hw in ("vaapi", "qsv", "none"):
            opts = FFmpegOptions(**_hw_opts(hw), vf_preset=preset.id)
            c1 = build_command(opts)
            r1 = parse_command(c1)
            o1, c2 = r1["options"], build_command(FFmpegOptions(**r1["options"]))
            o2 = parse_command(c2)["options"]
            assert r1["warnings"] == [], (preset.id, hw, r1["warnings"])
            assert c1 == c2, f"{preset.id}/{hw}:\n{c1}\n -> {c2}"
            assert o1 == o2, f"{preset.id}/{hw}: options still drifting"
            # a preset that renders nothing there honestly reads back as none
            want = preset.id if vf_snippet(preset.id, hw) else "none"
            assert o1["vf_preset"] == want, (preset.id, hw, o1["vf_preset"])


def test_removing_the_filter_from_the_text_resets_the_field():
    c1 = build_command(FFmpegOptions(vf_preset="deint-vaapi-frame"))
    assert "deinterlace_vaapi=rate=frame," in c1
    edited = c1.replace("deinterlace_vaapi=rate=frame,", "")
    base = {"vf_preset": "deint-vaapi-frame", "hw_accel": "vaapi"}
    assert parse_command(edited, base=base)["options"]["vf_preset"] == "none"
    # ... while a wholly foreign -vf (kept as-is) leaves the base value alone
    foreign = "ffmpeg -i <url> -vf hqdn3d -c:v h264_vaapi -f mpegts pipe:1"
    assert parse_command(foreign, base=base)["options"]["vf_preset"] == "deint-vaapi-frame"


def test_similar_snippets_parse_back_to_the_right_preset():
    bob = build_command(FFmpegOptions(vf_preset="deint-vaapi-bob"))
    field = build_command(FFmpegOptions(vf_preset="deint-vaapi-field"))
    assert parse_command(bob)["options"]["vf_preset"] == "deint-vaapi-bob"
    assert parse_command(field)["options"]["vf_preset"] == "deint-vaapi-field"
    yadif = build_command(FFmpegOptions(vf_preset="yadif-bob"))
    bwdif = build_command(FFmpegOptions(vf_preset="bwdif-bob"))
    assert parse_command(yadif)["options"]["vf_preset"] == "yadif-bob"
    assert parse_command(bwdif)["options"]["vf_preset"] == "bwdif-bob"


def test_a_foreign_filter_beside_a_scale_warns_but_keeps_the_chain():
    cmd = ("ffmpeg -init_hw_device vaapi=intel:/dev/dri/renderD128 -i <url> "
           "-vf scale_vaapi=w=1280:h=720:format=nv12,hqdn3d,fps=25,setsar=1 "
           "-map 0:v:0 -c:v h264_vaapi -f mpegts pipe:1")
    res = parse_command(cmd)
    assert res["options"]["hw_accel"] == "vaapi"
    assert res["options"]["resolution"] == "720p"
    assert res["options"]["vf_preset"] == "none"
    assert any("hqdn3d" in w for w in res["warnings"]), res["warnings"]


def test_universal_presets_render_on_all_three_decode_paths():
    for pid in ("setfield-prog", "setfield-tff", "null",
                "yadif-frame", "bwdif-bob"):
        for hw in ("vaapi", "qsv", "none"):
            snip = vf_snippet(pid, hw)
            assert snip, (pid, hw)
            vf = _vf(build_command(FFmpegOptions(**_hw_opts(hw), vf_preset=pid)))
            assert snip in vf, (pid, hw, vf)
            assert not any("needs" in n and "ignored" in n
                           for n in option_warnings(
                               FFmpegOptions(**_hw_opts(hw), vf_preset=pid))), (pid, hw)


def test_the_duo2_live_template_takes_a_deinterlacer():
    """The user's actual fault-finding move: clone the Duo2 live template and
    switch the Video filter - the preset splices in before the 1080p scale."""
    duo2 = {p["name"]: p for p in default_presets()}[
        "Vu+ Duo2 live (Enigma2 / H.264 1080p MPEG-TS)"]
    fields = {k: v for k, v in duo2.items()
              if k in FFmpegOptions.__dataclass_fields__}
    cmd = build_command(FFmpegOptions(**{**fields, "vf_preset": "deint-vaapi-frame"}))
    assert ("-vf deinterlace_vaapi=rate=frame,"
            "scale_vaapi=w=1920:h=1080:format=nv12,setsar=1" in cmd), cmd
    assert "fps=" not in cmd and "-r " not in cmd
    back = parse_command(cmd)["options"]
    assert back["fps"] == ""
    assert back["vf_preset"] == "deint-vaapi-frame"
    assert build_command(FFmpegOptions(**back)) == cmd
