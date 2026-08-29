from app.services.ffmpeg_templates import URL_PLACEHOLDER, build_command, FFmpegOptions
from app.services.ffmpeg_validate import _argv, syntax_check, TEST_VIDEO_URL


def test_syntax_requires_ffmpeg_and_placeholder():
    assert syntax_check("")["ok"] is False
    assert syntax_check("echo hi")["ok"] is False
    ok = syntax_check(f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1")
    assert ok["ok"] is True


def test_argv_inserts_duration_and_swaps_lavfi():
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="libx264"))
    args = _argv(cmd, TEST_VIDEO_URL, lavfi=True)
    assert "-f" in args and "lavfi" in args
    assert "-t" in args and args[args.index("-t") + 1] == "2"
    url_args = _argv(cmd, TEST_VIDEO_URL, lavfi=False)
    assert TEST_VIDEO_URL in url_args
    assert "-t" in url_args
