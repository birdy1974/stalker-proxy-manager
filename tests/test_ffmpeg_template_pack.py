"""The downloadable Quick Zap VAAPI CQP template pack.

The JSON under docs/ is an operator-facing artifact, so these tests pin both
directions that matter: every row still renders from its own fields, and the
file merges into a seeded install through the same endpoint the FFmpeg page
uses. SPM's legacy merge is additive: shipped names already present are
skipped, distinct scenario names are inserted exactly once.
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
    E2_DUO2_LIVE_PRESET_NAME, E2_VOD_REMUX_PRESET_NAME,
    E2_VOD_TRANSCODE_PRESET_NAME, FFmpegOptions, build_command,
    default_presets,
)

PACK_PATH = (Path(__file__).resolve().parents[1]
             / "docs" / "ffmpeg-templates" / "quick-zap-vaapi-cqp.json")

EXPECTED_NAMES = [
    "Vu+ Duo2 live - LP CQP (custom)",
    "Vu+ Duo2 live - Quick Zap no subs (VAAPI LP CQP26)",
    E2_VOD_REMUX_PRESET_NAME,
    E2_VOD_TRANSCODE_PRESET_NAME,
    "Web LAN - Copy video + AAC (fast start)",
    "Web Internet Slow - VAAPI LP 720p CQP32",
    "VLC Internet Slow - VAAPI LP 720p CQP30",
    "Web Internet Fast - VAAPI LP 1080p CQP25",
    "VLC Internet Fast - Copy MPEG-TS (fast start)",
]


def _pack() -> dict:
    return json.loads(PACK_PATH.read_text(encoding="utf-8"))


def _options(row: dict) -> FFmpegOptions:
    return FFmpegOptions(**{k: row[k] for k in FFmpegOptions.__dataclass_fields__})


def test_quick_zap_pack_is_a_fields_true_ffmpeg_section_export():
    data = _pack()
    assert data["app"] == "stalker-proxy-manager"
    assert data["version"] == 1
    assert data["section"] == "ffmpeg"

    rows = data["ffmpeg_templates"]
    assert [row["name"] for row in rows] == EXPECTED_NAMES
    assert len({row["name"] for row in rows}) == len(rows)

    shipped = {p["name"]: p for p in default_presets()}
    for row in rows:
        assert row["enabled"] is True and row["is_default"] is False
        assert row["command_source"] == "fields"
        assert build_command(_options(row)) == row["command"], row["name"]
        if row["video_codec"] == "h264_vaapi" and row["low_power"]:
            tokens = shlex.split(row["command"])
            assert row["rc_mode"] == "CQP", row["name"]
            assert "-low_power" in tokens and "-rc_mode" in tokens
            assert not ({"-b:v", "-maxrate", "-bufsize"} & set(tokens)), row["name"]

    by_name = {row["name"]: row for row in rows}
    for name in (E2_VOD_REMUX_PRESET_NAME, E2_VOD_TRANSCODE_PRESET_NAME):
        row, preset = by_name[name], shipped[name]
        assert row["is_builtin"] is True
        assert row["command"] == preset["command"]
        assert {k: row[k] for k in FFmpegOptions.__dataclass_fields__} == {
            k: preset[k] for k in FFmpegOptions.__dataclass_fields__}

    duo2 = by_name["Vu+ Duo2 live - LP CQP (custom)"]
    shipped_duo2 = shipped[E2_DUO2_LIVE_PRESET_NAME]
    assert {k: duo2[k] for k in FFmpegOptions.__dataclass_fields__} == {
        k: shipped_duo2[k] for k in FFmpegOptions.__dataclass_fields__}
    assert duo2["command"] == shipped_duo2["command"]

    zap = by_name["Vu+ Duo2 live - Quick Zap no subs (VAAPI LP CQP26)"]
    assert zap["subs"] == "drop" and "-sn" in shlex.split(zap["command"])


async def _template_names() -> list[str]:
    async with SessionLocal() as db:
        return list((await db.execute(
            select(FFmpegTemplate.name).order_by(FFmpegTemplate.name))).scalars())


async def test_quick_zap_pack_merges_beside_seeded_defaults_without_duplicates():
    await _seed_defaults()
    payload = {"mode": "merge", "data": _pack()}

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://testserver") as client:
        first = await client.post("/api/import", json=payload)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["imported"] == 7
        assert body["updated"] == 0
        assert set(body["skipped"]) == {
            f"ffmpeg:{E2_VOD_REMUX_PRESET_NAME}",
            f"ffmpeg:{E2_VOD_TRANSCODE_PRESET_NAME}",
        }
        assert set(EXPECTED_NAMES) <= set(await _template_names())

        second = await client.post("/api/import", json=payload)
        assert second.status_code == 200, second.text
        again = second.json()
        assert again["imported"] == 0 and again["updated"] == 0
        assert set(again["skipped"]) == {f"ffmpeg:{name}" for name in EXPECTED_NAMES}

    names = await _template_names()
    assert len(names) == len(set(names))
