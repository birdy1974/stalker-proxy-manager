"""
API activity: the stdout access line and the dashboard's API block.

uvicorn's own access log is deliberately at WARNING (a media proxy answers a
lot of small requests), which meant the container log showed nothing at all
about API traffic. The middleware in app/main.py replaces it with the app's own
single-stream format and feeds the counters the GUI indicator renders.
"""

from __future__ import annotations

import httpx

from app.services import api_stats


async def test_counters_record_shape_status_and_window():
    api_stats.reset()
    api_stats.record("GET", "/api/dashboard", 200, 12.5, "10.0.0.1")
    api_stats.record("GET", "/play/vod/4711.ts", 200, 3.0, "10.0.0.2")
    api_stats.record("GET", "/play/vod/9.ts", 500, 3.0, "10.0.0.2")

    snap = api_stats.snapshot()
    assert snap["running"] is True
    assert snap["requests_total"] == 3
    assert snap["errors_total"] == 1
    assert snap["requests_last_minute"] == 3
    # numeric path segments are collapsed, or the counter would grow per item
    assert {p["path"]: p["hits"] for p in snap["top_paths"]}["/play/vod/#.ts"] == 2
    assert snap["recent"][0]["status"] == 500, "most recent request first"


async def test_middleware_logs_every_request_and_feeds_the_dashboard():
    """End to end through the real app: the counter sees real HTTP traffic."""
    from app.main import app

    api_stats.reset()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        warm = await c.get("/api/streams")          # a completed request to count
        assert warm.status_code == 200
        r = await c.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()

    assert "api" in body, "dashboard must expose the API block"
    api = body["api"]
    assert api["running"] is True
    # the in-flight /api/dashboard is not counted yet; the one before it is
    assert api["requests_total"] >= 1
    assert any(p["path"] == "/api/streams" for p in api["top_paths"])
    assert api["uptime_s"] >= 0
    assert api["streams_active"] == len(body["streams"])
    assert isinstance(api["top_paths"], list) and api["top_paths"]
    # the in-flight request is not in the snapshot yet, so check the warm one
    assert api["recent"][0]["path"] == "/api/streams"
    # per-user connection usage is what the "who is watching" line renders
    assert api["streams_per_user"] == []


async def test_static_paths_stay_out_of_the_stdout_log():
    """The skip list exists so /static/ churn cannot drown the real messages."""
    from app.config import ACCESS_LOG_SKIP

    assert "/static/" in ACCESS_LOG_SKIP
    assert "/favicon.ico" in ACCESS_LOG_SKIP
