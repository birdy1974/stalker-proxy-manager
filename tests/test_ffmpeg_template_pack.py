"""The downloadable selected FFmpeg template set.

The JSON under docs/ is an operator-facing artifact, so these tests pin the
directions that matter: every non-marker row still renders from its own
fields, the file contains the selected 13-template catalog, and it imports
through the same endpoint the FFmpeg page uses. SPM's legacy merge is
additive: into an empty template table it creates those 13 rows; into a
seeded install it adds the nine logical rows and skips the four retained
shipped rows already present. It cannot delete shipped templates that are
absent from the file.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import _seed_defaults, app
from app.models import FFmpegTemplate
from app.services.ffmpeg_templates import (
    COPY_PRESET_NAME, E2_DUO2_LIVE_PRESET_NAME, E2_VOD_REMUX_PRESET_NAME,
    E2_VOD_TRANSCODE_PRESET_NAME, FFmpegOptions, PASSTHROUGH_COMMAND,
    PASSTHROUGH_PRESET_NAME, REDIRECT_COMMAND, REDIRECT_PRESET_NAME,
    REFERENCE_PRESET_NAME, build_command, default_presets,
)

PACK_PATH = (Path(__file__).resolve().parents[1]
             / "docs" / "ffmpeg-templates" / "quick-zap-vaapi-cqp.json")

EXPECTED_NAMES = [
    "Enigma2 - Live",
    "Enigma2 - Live (with subtitles)",
    "Enigma2 - VOD",
    "Enigma2 - VOD (transcode)",
    "Web player - LAN",
    "Web player - Internet (slow)",
    "Web player - Internet (fast)",
    "VLC - Internet (slow)",
    "VLC - Internet (fast)",
    COPY_PRESET_NAME,
    "Dreambox DM800se (Enigma2 / MPEG2-SD)",
    REDIRECT_PRESET_NAME,
    PASSTHROUGH_PRESET_NAME,
]

RETAINED_SHIPPED_NAMES = EXPECTED_NAMES[9:]
REMOVED_SHIPPED_NAMES = {
    REFERENCE_PRESET_NAME,
    "VAAPI 1080p ~2.5M",
    "QSV 720p ~1M",
    "Software 720p ~1.2M (libx264)",
    E2_VOD_REMUX_PRESET_NAME,
    E2_VOD_TRANSCODE_PRESET_NAME,
    E2_DUO2_LIVE_PRESET_NAME,
}


def _pack() -> dict:
    return json.loads(PACK_PATH.read_text(encoding="utf-8"))


def _options(row: dict) -> FFmpegOptions:
    return FFmpegOptions(**{k: row[k] for k in FFmpegOptions.__dataclass_fields__})


def test_selected_template_file_is_a_fields_true_ffmpeg_section_export():
    data = _pack()
    assert data["app"] == "stalker-proxy-manager"
    assert data["version"] == 1
    assert data["section"] == "ffmpeg"

    rows = data["ffmpeg_templates"]
    assert [row["name"] for row in rows] == EXPECTED_NAMES
    assert len({row["name"] for row in rows}) == len(rows) == 13
    assert not ({row["name"] for row in rows} & REMOVED_SHIPPED_NAMES)

    shipped = {p["name"]: p for p in default_presets()}
    marker_commands = {
        REDIRECT_PRESET_NAME: REDIRECT_COMMAND,
        PASSTHROUGH_PRESET_NAME: PASSTHROUGH_COMMAND,
    }
    for row in rows:
        assert row["enabled"] is True and row["is_default"] is False
        assert row["command_source"] == "fields"
        if row["name"] in marker_commands:
            assert row["command"] == marker_commands[row["name"]]
        else:
            assert build_command(_options(row)) == row["command"], row["name"]
        if row["video_codec"] == "h264_vaapi" and row["low_power"]:
            tokens = shlex.split(row["command"])
            assert row["rc_mode"] == "CQP", row["name"]
            assert "-low_power" in tokens and "-rc_mode" in tokens
            assert not ({"-b:v", "-maxrate", "-bufsize"} & set(tokens)), row["name"]

    by_name = {row["name"]: row for row in rows}
    logical_aliases = {
        "Enigma2 - VOD": E2_VOD_REMUX_PRESET_NAME,
        "Enigma2 - VOD (transcode)": E2_VOD_TRANSCODE_PRESET_NAME,
    }
    for logical_name, shipped_name in logical_aliases.items():
        row, preset = by_name[logical_name], shipped[shipped_name]
        assert row["is_builtin"] is False
        assert row["command"] == preset["command"]
        assert {k: row[k] for k in FFmpegOptions.__dataclass_fields__} == {
            k: preset[k] for k in FFmpegOptions.__dataclass_fields__}

    for name in RETAINED_SHIPPED_NAMES:
        row, preset = by_name[name], shipped[name]
        assert row["is_builtin"] is True
        assert row["command"] == preset["command"]
        assert {k: row[k] for k in FFmpegOptions.__dataclass_fields__} == {
            k: preset[k] for k in FFmpegOptions.__dataclass_fields__}

    duo2 = by_name["Enigma2 - Live (with subtitles)"]
    shipped_duo2 = shipped[E2_DUO2_LIVE_PRESET_NAME]
    assert {k: duo2[k] for k in FFmpegOptions.__dataclass_fields__} == {
        k: shipped_duo2[k] for k in FFmpegOptions.__dataclass_fields__}
    assert duo2["command"] == shipped_duo2["command"]

    # Every logical Enigma2 transcode is High@4.1. The VOD remux stores the
    # same field value but copies the source video, so no level is rendered.
    for name in ("Enigma2 - Live", "Enigma2 - Live (with subtitles)",
                 "Enigma2 - VOD", "Enigma2 - VOD (transcode)"):
        assert by_name[name]["profile"] == "high", name
        assert by_name[name]["level"] == "4.1", name
    for name in ("Enigma2 - Live", "Enigma2 - Live (with subtitles)",
                 "Enigma2 - VOD (transcode)"):
        tokens = shlex.split(by_name[name]["command"])
        assert tokens[tokens.index("-level") + 1] == "4.1", name
    # The retained Dreambox template is the deliberate legacy-Enigma2
    # exception: its SD compatibility output stays Main@3.1.
    dreambox = by_name["Dreambox DM800se (Enigma2 / MPEG2-SD)"]
    assert dreambox["profile"] == "main" and dreambox["level"] == "3.1"

    zap = by_name["Enigma2 - Live"]
    assert zap["subs"] == "drop" and "-sn" in shlex.split(zap["command"])


async def _template_names() -> list[str]:
    async with SessionLocal() as db:
        return list((await db.execute(
            select(FFmpegTemplate.name).order_by(FFmpegTemplate.name))).scalars())


async def test_selected_template_file_merges_beside_seeded_defaults_without_duplicates():
    await _seed_defaults()
    payload = {"mode": "merge", "data": _pack()}

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://testserver") as client:
        first = await client.post("/api/import", json=payload)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["imported"] == 9
        assert body["updated"] == 0
        assert set(body["skipped"]) == {
            f"ffmpeg:{name}" for name in RETAINED_SHIPPED_NAMES}
        assert set(EXPECTED_NAMES) <= set(await _template_names())

        second = await client.post("/api/import", json=payload)
        assert second.status_code == 200, second.text
        again = second.json()
        assert again["imported"] == 0 and again["updated"] == 0
        assert set(again["skipped"]) == {f"ffmpeg:{name}" for name in EXPECTED_NAMES}

    names = await _template_names()
    assert len(names) == len(set(names))
    assert set(names) == {p["name"] for p in default_presets()} | set(EXPECTED_NAMES)


async def test_selected_template_file_imports_exactly_13_into_an_empty_table():
    payload = {"mode": "merge", "data": _pack()}

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://testserver") as client:
        result = await client.post("/api/import", json=payload)

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["imported"] == 13
    assert body["updated"] == 0
    assert body["skipped"] == []
    assert set(await _template_names()) == set(EXPECTED_NAMES)
