"""Catch-up (timeshift) for the Xtream output: redirect to the real source, store nothing.

* Xtream-adopted portal: the upstream `/live/...ts` URL is rewritten to the
  panel's `/timeshift/...` URL, and the client gets a 302 to it.
* Stalker-only portal: the panel is asked for an archive link for the programme
  covering the requested start, and the client gets a 302 to that link.
* `tv_archive` / `tv_archive_duration` in the live catalogue follow real availability.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import (LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal,
                        User)
from app.portal.epg import parse_archive_day
from app.services import catchup
from app.services.catchup import (CatchupUnavailable, find_programme, parse_start,
                                  xtream_timeshift_url)

BASE = "http://testserver"
ALL_GROUPS = json.dumps({"live": ["News"], "vod": [], "series": [], "local": []})


def _now_minus(days: float) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(second=0, microsecond=0)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d:%H-%M")


async def _user() -> None:
    async with SessionLocal() as s:
        s.add(User(name="cu", password="secret", enabled=True, m3u_enabled=True,
                   xtream_enabled=True, max_connections=2, groups_json=ALL_GROUPS))
        await s.commit()


async def _channel(*, archive: bool, adopted: bool = False, xtream_url: str | None = None,
                   cmd: str | None = "ffrt http://x/1", stb_tz: str | None = None,
                   macs: tuple[str, ...] = ()) -> int:
    """One live playlist item fed by one source, optionally on a portal with MACs."""
    async with SessionLocal() as s:
        portal = Portal(name="cp", base_url="http://p.invalid/c/", resolved_url="http://p.invalid/c/",
                        enabled=True, xtream_adopted=adopted, stb_timezone=stb_tz)
        s.add(portal)
        await s.flush()
        for i, mac in enumerate(macs):
            s.add(MacAddress(portal_id=portal.id, mac=mac, order=i, status="online"))
        src = LiveSource(portal_id=portal.id, portal_channel_id="555", original_name="Ch",
                         cmd=cmd, tv_archive=archive, enabled=True, xtream_url=xtream_url)
        s.add(src)
        await s.flush()
        item = LivePlaylist(custom_name="Ch", group_name="News", enabled=True, order=1)
        s.add(item)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=item.id, live_source_id=src.id, priority=1))
        await s.commit()
        return item.id


async def _get(path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as c:
        return await c.get(path, follow_redirects=False)


# ------------------------------------------------------------------ pure helpers

def test_parse_start_reads_utc_stamp_and_rejects_garbage():
    assert parse_start("2026-10-07:20-30") == datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc)
    for bad in ("2026-10-07 20:30", "yesterday", "2026-13-01:00-00"):
        try:
            parse_start(bad)
        except CatchupUnavailable as exc:
            assert exc.status == 400
        else:
            raise AssertionError(bad)


def test_xtream_url_is_rewritten_to_the_panel_timeshift_form():
    start = datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc)
    url = xtream_timeshift_url("http://panel.example:8080/live/alice/s3cret/555.ts", 90, start)
    assert url == "http://panel.example:8080/timeshift/alice/s3cret/90/2026-10-07:20-30/555.ts"


def test_only_upstream_live_urls_are_rewritten():
    start = datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc)
    assert xtream_timeshift_url("http://panel/movie/a/b/1.ts", 60, start) is None
    assert xtream_timeshift_url("", 60, start) is None
    assert xtream_timeshift_url(None, 60, start) is None


def test_parse_archive_day_reads_the_panel_rows_in_time_order():
    payload = {"js": {"total_items": 2, "max_page_items": 10, "data": [
        {"id": "b", "start_timestamp": "1791405600", "stop_timestamp": "1791409200",
         "mark_archive": "1"},
        {"id": "a", "start_timestamp": "1791402000", "stop_timestamp": "1791405600",
         "mark_archive": "0"},
        {"id": "", "start_timestamp": "1791402000", "stop_timestamp": "1791405600"},
    ]}}
    total, per_page, rows = parse_archive_day(payload)
    assert (total, per_page) == (2, 10)
    assert [r["id"] for r in rows] == ["a", "b"]
    assert [r["archived"] for r in rows] == [False, True]


def test_parse_archive_day_tolerates_garbage():
    assert parse_archive_day(None) == (0, 0, [])
    assert parse_archive_day({"js": "nope"}) == (0, 0, [])


class _FakeEpg:
    """Serves archive pages; records how many were asked for."""

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.asked: list[int] = []

    async def archive_day(self, ch_id, day, page):
        self.asked.append(page)
        return self.pages[page - 1]


def _page(rows: list[tuple[str, datetime, datetime, str]], page: int = 1, total: int = 0,
          per: int = 10) -> dict:
    data = [{"id": i, "start_timestamp": str(int(a.timestamp())),
             "stop_timestamp": str(int(b.timestamp())), "mark_archive": m}
            for i, a, b, m in rows]
    return {"js": {"total_items": total or len(rows), "max_page_items": per, "data": data}}


async def test_find_programme_takes_the_covering_row_and_stops_early():
    t0 = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
    h = timedelta(hours=1)
    fake = _FakeEpg([_page([("p1", t0, t0 + h, "1"), ("p2", t0 + h, t0 + 2 * h, "1")],
                           total=30, per=10)])
    got = await find_programme(fake, "555", "2026-10-07", t0 + h + timedelta(minutes=10), None)
    assert got == "p2"
    assert fake.asked == [1]   # covered on page 1, no further page walked


async def test_find_programme_accepts_a_near_start_but_not_a_distant_one():
    t0 = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
    near = _FakeEpg([_page([("p1", t0 + timedelta(minutes=3), t0 + timedelta(hours=1), "1")])])
    assert await find_programme(near, "555", "d", t0, None) == "p1"
    far = _FakeEpg([_page([("p1", t0 + timedelta(minutes=30), t0 + timedelta(hours=1), "1")])])
    assert await find_programme(far, "555", "d", t0, None) is None


async def test_find_programme_walks_pages_until_the_time_is_passed():
    t0 = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
    h = timedelta(hours=1)
    fake = _FakeEpg([
        _page([("a", t0, t0 + h, "1")], total=20, per=1),
        _page([("b", t0 + h, t0 + 2 * h, "1")], total=20, per=1),
        _page([("c", t0 + 2 * h, t0 + 3 * h, "1")], total=20, per=1),
    ])
    got = await find_programme(fake, "555", "d", t0 + 2 * h + timedelta(minutes=5), None)
    assert got == "c"
    assert fake.asked == [1, 2, 3]


async def test_find_programme_ignores_rows_without_an_archive():
    t0 = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
    fake = _FakeEpg([_page([("p1", t0, t0 + timedelta(hours=1), "0")])])
    assert await find_programme(fake, "555", "d", t0 + timedelta(minutes=5), None) is None


# ----------------------------------------------------------------- HTTP surface

async def test_xtream_adopted_portal_redirects_to_the_panel_timeshift_url():
    await _user()
    upstream = "http://panel.example:8080/live/alice/s3cret/555.ts"
    cid = await _channel(archive=True, adopted=True, xtream_url=upstream)
    when = _now_minus(1)
    r = await _get(f"/timeshift/cu/secret/90/{_stamp(when)}/{cid}.ts")
    assert r.status_code == 302
    assert r.headers["location"] == (
        f"http://panel.example:8080/timeshift/alice/s3cret/90/{_stamp(when)}/555.ts")


async def test_catalogue_advertises_archive_only_where_a_source_has_it():
    await _user()
    with_archive = await _channel(archive=True)
    no_archive_row = None
    async with SessionLocal() as s:
        p = Portal(name="plain", base_url="http://q.invalid/c/", enabled=True)
        s.add(p)
        await s.flush()
        src = LiveSource(portal_id=p.id, portal_channel_id="9", original_name="Plain",
                         cmd="x", tv_archive=False, enabled=True)
        s.add(src)
        await s.flush()
        item = LivePlaylist(custom_name="Plain", group_name="News", enabled=True, order=2)
        s.add(item)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=item.id, live_source_id=src.id, priority=1))
        await s.commit()
        no_archive_row = item.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as c:
        r = await c.get("/player_api.php", params={"username": "cu", "password": "secret",
                                                   "action": "get_live_streams"})
    assert r.status_code == 200, r.text
    rows = {x["stream_id"]: x for x in r.json()}
    assert rows[with_archive]["tv_archive"] == 1
    assert rows[with_archive]["tv_archive_duration"] == catchup.ARCHIVE_DAYS
    assert rows[no_archive_row]["tv_archive"] == 0
    assert rows[no_archive_row]["tv_archive_duration"] == 0


async def test_no_archive_source_is_a_404():
    await _user()
    cid = await _channel(archive=False)
    r = await _get(f"/timeshift/cu/secret/60/{_stamp(_now_minus(1))}/{cid}.ts")
    assert r.status_code == 404


async def test_bad_bounds_are_refused():
    await _user()
    cid = await _channel(archive=True, adopted=True,
                         xtream_url="http://panel/live/a/b/1.ts")
    assert (await _get(f"/timeshift/cu/secret/60/not-a-date/{cid}.ts")).status_code == 400
    future = _stamp(datetime.now(timezone.utc) + timedelta(days=1))
    assert (await _get(f"/timeshift/cu/secret/60/{future}/{cid}.ts")).status_code == 404
    old = _stamp(_now_minus(catchup.ARCHIVE_DAYS + 2))
    assert (await _get(f"/timeshift/cu/secret/60/{old}/{cid}.ts")).status_code == 404
    assert (await _get(f"/timeshift/cu/secret/0/{_stamp(_now_minus(1))}/{cid}.ts")).status_code == 400


async def test_wrong_credentials_are_refused():
    await _user()
    cid = await _channel(archive=True, adopted=True, xtream_url="http://panel/live/a/b/1.ts")
    r = await _get(f"/timeshift/cu/wrong/60/{_stamp(_now_minus(1))}/{cid}.ts")
    assert r.status_code == 403


# ------------------------------------------------------------ Stalker-only path

class _FakeClient:
    def __init__(self, epg: _FakeEpg, link: str | None = "http://cdn.example/archive.ts",
                 fail: bool = False):
        self.epg = epg
        self.link = link
        self.fail = fail
        self.cmds: list[tuple[str, str]] = []
        self.closed = False

    async def archive_day(self, ch_id, day, page):
        return await self.epg.archive_day(ch_id, day, page)

    async def create_link(self, cmd, kind="itv", **_):
        from app.portal.client import PortalError
        self.cmds.append((cmd, kind))
        if self.fail:
            raise PortalError("refused")
        return self.link

    async def close(self):
        self.closed = True


def _patch_pool(monkeypatch, client):
    class _Pool:
        async def get(self, _session):
            return client

    monkeypatch.setattr(catchup, "POOL", _Pool())
    monkeypatch.setattr(catchup, "PortalSession",
                        SimpleNamespace(from_rows=lambda portal, mac: None))


async def test_stalker_portal_resolves_an_archive_link_for_the_programme(monkeypatch):
    await _user()
    cid = await _channel(archive=True, macs=("00:1A:79:00:00:01",))
    start = _now_minus(1)
    prog_start = start - timedelta(minutes=10)
    epg = _FakeEpg([_page([("prog42", prog_start, prog_start + timedelta(hours=1), "1")])])
    client = _FakeClient(epg)
    _patch_pool(monkeypatch, client)

    r = await _get(f"/timeshift/cu/secret/60/{_stamp(start)}/{cid}.ts")
    assert r.status_code == 302
    assert r.headers["location"] == "http://cdn.example/archive.ts"
    assert client.cmds == [("auto /media/prog42.mpg", "archive")]
    assert client.closed


async def test_stalker_portal_refusing_the_link_is_a_502(monkeypatch):
    await _user()
    cid = await _channel(archive=True, macs=("00:1A:79:00:00:01",))
    start = _now_minus(1)
    epg = _FakeEpg([_page([("p", start, start + timedelta(hours=1), "1")])])
    _patch_pool(monkeypatch, _FakeClient(epg, fail=True))
    r = await _get(f"/timeshift/cu/secret/60/{_stamp(start)}/{cid}.ts")
    assert r.status_code == 502


async def test_stalker_portal_without_a_programme_at_that_time_is_a_404(monkeypatch):
    await _user()
    cid = await _channel(archive=True, macs=("00:1A:79:00:00:01",))
    epg = _FakeEpg([_page([])])
    _patch_pool(monkeypatch, _FakeClient(epg))
    r = await _get(f"/timeshift/cu/secret/60/{_stamp(_now_minus(1))}/{cid}.ts")
    assert r.status_code == 404


async def test_stalker_portal_with_no_usable_mac_is_a_404(monkeypatch):
    await _user()
    cid = await _channel(archive=True, macs=())
    _patch_pool(monkeypatch, _FakeClient(_FakeEpg([])))
    r = await _get(f"/timeshift/cu/secret/60/{_stamp(_now_minus(1))}/{cid}.ts")
    assert r.status_code == 404
