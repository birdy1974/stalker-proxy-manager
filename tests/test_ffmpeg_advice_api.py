"""The advisor over HTTP: endpoints, per-template persistence, the page."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal, _add_missing_columns, engine
from app.main import _seed_defaults, app
from app.models import FFmpegTemplate
from app.services import ffmpeg_env, ffmpeg_speed
from app.services.ffmpeg_templates import (REFERENCE_PRESET_NAME, FFmpegOptions, build_command)

SW = dict(hw_accel="none", video_codec="libx264", video_bitrate="1200k", maxrate="1300k",
          bufsize="2400k", extra_output="")


@pytest.fixture(autouse=True)
def _render_node_present(monkeypatch):
    """CI has no /dev/dri; pretend the default render node exists so only the
    tests that want `device-missing` see it."""
    monkeypatch.setattr(ffmpeg_env, "render_devices", lambda extra=(): [
        {"path": ffmpeg_env.VAAPI_DEVICE, "exists": True, "accessible": True}])


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def fields(**kw) -> dict:
    return {**asdict(FFmpegOptions()), **kw}


async def _row(name: str) -> dict:
    async with SessionLocal() as s:
        row = (await s.execute(select(FFmpegTemplate).where(FFmpegTemplate.name == name))).scalar_one()
        return {c.name: getattr(row, c.name) for c in FFmpegTemplate.__table__.columns}


# --------------------------------------------------------------------------- #
#  /advice
# --------------------------------------------------------------------------- #
async def test_advice_returns_findings_scores_goals_and_host_facts():
    async with client() as c:
        r = await c.post("/api/ffmpeg/advice", json={"options": fields(gop="250"), "goal": "balanced"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["id"] for f in body["findings"]] == ["gop-long"]
    assert body["findings"][0]["fix"]["set"] == {"gop": "50"}
    assert body["counts"] == {"critical": 0, "warn": 1, "tip": 0}
    assert set(body["scores"]) >= {"quality", "start", "efficiency"}
    assert [g["id"] for g in body["goals"]] == ["balanced", "fast_start", "quality", "low_cpu", "internet"]
    assert body["environment"]["cpus"] >= 1 and body["live"] is None


async def test_advice_follows_goal_and_dismissals_and_survives_bad_input():
    async with client() as c:
        opts = fields(gop="75")
        bal = (await c.post("/api/ffmpeg/advice", json={"options": opts})).json()
        fast = (await c.post("/api/ffmpeg/advice", json={"options": opts, "goal": "fast_start"})).json()
        gone = (await c.post("/api/ffmpeg/advice", json={
            "options": fields(gop="250"), "ignored": "gop-long"})).json()
        junk = await c.post("/api/ffmpeg/advice", json={"options": {"gop": ["x"], "fps": None}, "goal": 7})
    assert bal["findings"] == [] and [f["id"] for f in fast["findings"]] == ["gop-long"]
    assert gone["findings"][0]["ignored"] is True and gone["counts"]["warn"] == 0
    assert junk.status_code == 200 and junk.json()["goal"] == "balanced"


async def test_advice_includes_the_last_live_measurement_of_the_named_template():
    ffmpeg_speed.record("My template", 0.6)
    async with client() as c:
        r = (await c.post("/api/ffmpeg/advice", json={
            "options": fields(resolution="1080p"), "name": "My template"})).json()
        other = (await c.post("/api/ffmpeg/advice", json={
            "options": fields(resolution="1080p"), "name": "Other"})).json()
    assert r["live"]["slow"] is True
    live = next(f for f in r["findings"] if f["id"] == "live-slow")
    assert live["severity"] == "critical" and live["fix"]["set"] == {"resolution": "720p"}
    assert other["live"] is None and not any(f["id"] == "live-slow" for f in other["findings"])


async def test_advice_uses_the_hosts_real_render_nodes(monkeypatch):
    monkeypatch.setattr(ffmpeg_env, "render_devices", lambda extra=(): [
        {"path": "/dev/dri/renderD128", "exists": False, "accessible": False},
        {"path": "/dev/dri/renderD129", "exists": True, "accessible": True}])
    async with client() as c:
        r = (await c.post("/api/ffmpeg/advice", json={"options": fields()})).json()
    f = next(f for f in r["findings"] if f["id"] == "device-missing")
    assert f["fix"]["set"] == {"device": "/dev/dri/renderD129"}


async def test_advice_counts_running_transcodes_for_the_host_busy_rule(monkeypatch):
    from app.routers import api_ffmpeg
    monkeypatch.setattr(api_ffmpeg, "_active_transcodes", lambda: 99)
    async with client() as c:
        r = (await c.post("/api/ffmpeg/advice", json={"options": fields(**{**SW, "extra_output": "-preset veryfast"})})).json()
    assert r["environment"]["active_transcodes"] == 99
    assert any(f["id"] == "host-busy" for f in r["findings"])


async def test_active_transcodes_counts_only_running_video_encodes():
    from app.routers.api_ffmpeg import _active_transcodes
    from app.services.stream_manager import MANAGER, StreamHandle

    def h(n, cmd, proc=object(), dead=False):
        x = StreamHandle(id=str(n) * 32, kind="live", item_name="c", user_name=None,
                         template_name="t", command=cmd)
        x.proc, x.dead = proc, dead
        return x
    MANAGER.streams.clear()
    try:
        for x in (h(1, "ffmpeg -i <url> -c:v libx264 pipe:1"), h(2, "ffmpeg -i <url> -c:v copy pipe:1"),
                  h(3, "ffmpeg -i <url> -c:v libx264 pipe:1", proc=None),
                  h(4, "ffmpeg -i <url> -c:v libx264 pipe:1", dead=True),
                  h(5, "ffmpeg -i <url> -c:v h264_vaapi pipe:1")):
            MANAGER.streams[x.id] = x
        assert _active_transcodes() == 2
    finally:
        MANAGER.streams.clear()


# --------------------------------------------------------------------------- #
#  /advice/preview
# --------------------------------------------------------------------------- #
async def test_preview_shows_exact_changes_and_the_resulting_command_without_applying():
    opts = fields(gop="250", async_depth="16", audio_rate="44100")
    async with client() as c:
        r = await c.post("/api/ffmpeg/advice/preview", json={
            "options": opts, "ids": ["gop-long", "async-depth-high", "audio-rate-44k"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {(x["field"], x["from"], x["to"]) for x in body["changes"]} == {
        ("gop", "250", "50"), ("async_depth", "16", "4"), ("audio_rate", "44100", "48000")}
    assert "-g 50" in body["command"] and "-async_depth 4" in body["command"]
    assert body["errors"] == [] and body["skipped"] == []


async def test_preview_reports_conflicts_and_rejects_a_malformed_request():
    opts = fields(**{**SW, "extra_output": "-preset veryfast", "video_bitrate": "300k", "maxrate": "330k", "bufsize": "6000k"})
    async with client() as c:
        ok = (await c.post("/api/ffmpeg/advice/preview", json={
            "options": opts, "ids": ["bitrate-low", "bufsize-large"]})).json()
        bad = await c.post("/api/ffmpeg/advice/preview", json={"options": opts, "ids": "gop-long"})
        empty = (await c.post("/api/ffmpeg/advice/preview", json={"options": opts, "ids": []})).json()
    assert [s["id"] for s in ok["skipped"]] == ["bufsize-large"]
    assert bad.status_code == 422
    assert empty["changes"] == [] and empty["options"]["video_bitrate"] == "300k"


async def test_preview_never_writes_anything():
    await _seed_defaults()
    before = await _row(REFERENCE_PRESET_NAME)
    async with client() as c:
        await c.post("/api/ffmpeg/advice/preview", json={
            "options": fields(gop="250"), "ids": ["gop-long"], "name": REFERENCE_PRESET_NAME})
    assert await _row(REFERENCE_PRESET_NAME) == before


# --------------------------------------------------------------------------- #
#  /advice-summary + /environment
# --------------------------------------------------------------------------- #
async def test_summary_counts_per_template_uses_each_rows_goal_and_skips_bypass_templates():
    await _seed_defaults()
    async with client() as c:
        a = (await c.post("/api/ffmpeg", json={**fields(gop="250"), "name": "Long GOP"})).json()["item"]
        b = (await c.post("/api/ffmpeg", json={**fields(gop="250"), "name": "Dismissed",
                                               "advice_ignored": "gop-long"})).json()["item"]
        d = (await c.post("/api/ffmpeg", json={**fields(gop="75"), "name": "Strict",
                                               "advice_goal": "fast_start"})).json()["item"]
        r = (await c.get("/api/ffmpeg/advice-summary")).json()["items"]
    assert r[str(a["id"])]["warn"] == 1
    assert r[str(b["id"])] == {"critical": 0, "warn": 0, "tip": 0}
    assert r[str(d["id"])]["warn"] == 1
    redirect = await _row("Redirect (bypass ffmpeg)")
    assert str(redirect["id"]) not in r
    reference = await _row(REFERENCE_PRESET_NAME)
    assert r[str(reference["id"])] == {"critical": 0, "warn": 0, "tip": 0}


async def test_environment_endpoint(monkeypatch):
    async def fake_run(args, timeout=0):
        return (0, "Encoders:\n V....D libx264   x\n") if "-encoders" in args else (0, "Hardware acceleration methods:\nvaapi\n")
    monkeypatch.setattr(ffmpeg_env, "_run", fake_run)
    async with client() as c:
        r = (await c.get("/api/ffmpeg/environment")).json()
    assert r["encoders"] == ["libx264"] and r["hwaccels"] == ["vaapi"]
    assert r["cpus"] >= 1 and "active_transcodes" in r and isinstance(r["devices"], list)


# --------------------------------------------------------------------------- #
#  per-template persistence
# --------------------------------------------------------------------------- #
async def test_goal_and_dismissals_are_saved_with_the_template_and_returned():
    async with client() as c:
        made = (await c.post("/api/ffmpeg", json={**fields(), "name": "Tuned", "advice_goal": "Internet",
                                                  "advice_ignored": " gop-long, GOP-long ,x264-preset;drop "})).json()["item"]
        assert made["advice_goal"] == "internet" and made["advice_ignored"] == "gop-long"
        listed = {t["name"]: t for t in (await c.get("/api/ffmpeg")).json()["items"]}
    assert listed["Tuned"]["advice_goal"] == "internet"
    row = await _row("Tuned")
    assert (row["advice_goal"], row["advice_ignored"]) == ("internet", "gop-long")


async def test_defaults_for_new_rows_and_untouched_by_older_clients():
    async with client() as c:
        made = (await c.post("/api/ffmpeg", json={**fields(), "name": "Plain"})).json()["item"]
        assert (made["advice_goal"], made["advice_ignored"]) == ("balanced", "")
        tid = (await c.post("/api/ffmpeg", json={**fields(), "name": "Keep", "advice_goal": "quality",
                                                 "advice_ignored": "fps-high"})).json()["item"]["id"]
        # an older client / script that knows nothing about the advisor
        r = await c.put(f"/api/ffmpeg/{tid}", json={"gop": "50"})
        assert r.status_code == 200, r.text
    row = await _row("Keep")
    assert (row["advice_goal"], row["advice_ignored"]) == ("quality", "fps-high")


async def test_an_unknown_goal_is_rejected_and_nothing_is_saved():
    async with client() as c:
        r = await c.post("/api/ffmpeg", json={**fields(), "name": "Bad", "advice_goal": "turbo"})
        assert r.status_code == 422 and "advice_goal" in r.text
        tid = (await c.post("/api/ffmpeg", json={**fields(), "name": "Ok"})).json()["item"]["id"]
        r = await c.put(f"/api/ffmpeg/{tid}", json={"advice_goal": "turbo"})
        assert r.status_code == 422
    assert (await _row("Ok"))["advice_goal"] == "balanced"


async def test_advice_settings_do_not_touch_the_command():
    async with client() as c:
        tid = (await c.post("/api/ffmpeg", json={**fields(), "name": "Cmd"})).json()["item"]["id"]
        before = (await _row("Cmd"))["command"]
        await c.put(f"/api/ffmpeg/{tid}", json={"advice_goal": "low_cpu", "advice_ignored": "a,b"})
    row = await _row("Cmd")
    assert row["command"] == before == build_command(FFmpegOptions())
    assert row["command_source"] == "fields"


async def test_builtin_reseeding_keeps_the_operators_advice_settings():
    await _seed_defaults()
    row = await _row(REFERENCE_PRESET_NAME)
    async with client() as c:
        await c.put(f"/api/ffmpeg/{row['id']}", json={"advice_goal": "fast_start", "advice_ignored": "fps-high"})
    await _seed_defaults()
    after = await _row(REFERENCE_PRESET_NAME)
    assert (after["advice_goal"], after["advice_ignored"]) == ("fast_start", "fps-high")


async def test_advice_settings_survive_export_and_import():
    async with client() as c:
        await c.post("/api/ffmpeg", json={**fields(), "name": "Exported", "advice_goal": "quality",
                                          "advice_ignored": "fps-high"})
        data = (await c.get("/api/export", params={"section": "ffmpeg"})).json()
        exported = next(t for t in data["ffmpeg_templates"] if t["name"] == "Exported")
        assert exported["advice_goal"] == "quality" and exported["advice_ignored"] == "fps-high"
        assert (await c.delete(f"/api/ffmpeg/{(await _row('Exported'))['id']}")).status_code == 200
        r = await c.post("/api/import", json={"mode": "merge", "data": {"ffmpeg_templates": [exported]}})
        assert r.status_code == 200, r.text
    row = await _row("Exported")
    assert (row["advice_goal"], row["advice_ignored"]) == ("quality", "fps-high")


async def test_old_databases_get_the_new_columns_with_their_defaults():
    async with client() as c:
        await c.post("/api/ffmpeg", json={**fields(), "name": "Legacy"})
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE ffmpeg_templates DROP COLUMN advice_goal")
        await conn.exec_driver_sql("ALTER TABLE ffmpeg_templates DROP COLUMN advice_ignored")
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_add_missing_columns)          # idempotent
    row = await _row("Legacy")
    assert (row["advice_goal"], row["advice_ignored"]) == ("balanced", "")


# --------------------------------------------------------------------------- #
#  the page
# --------------------------------------------------------------------------- #
async def test_the_editor_page_ships_the_advisor_card_and_script():
    async with client() as c:
        page = await c.get("/ffmpeg")
        js = await c.get("/static/js/ffmpeg-advisor.js")
    assert page.status_code == 200
    for needle in ("ffmpeg-advisor.js?v=", 'id="ff-advisor"', 'id="ff-adv-goal"', 'id="ff-adv-review"',
                   'id="ff-adv-list"', "FFmpegAdvisor.init(", "FFmpegAdvisor.state()", "FFmpegAdvisor.load(",
                   "/api/ffmpeg/advice-summary"):
        assert needle in page.text, needle
    # the goal select must not be an `.ff-field`: that class re-renders the command on change
    assert 'id="ff-adv-goal" class="form-select form-select-sm w-auto"' in page.text
    assert js.status_code == 200 and "advice/preview" in js.text
