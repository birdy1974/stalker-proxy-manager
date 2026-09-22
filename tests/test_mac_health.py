"""
Multi-MAC health: background status/expiry refresh + genre comparison.

Also covers:
  * busy detection via mac_id (ffmpeg locks AND redirect leases)
  * genre upsert after compare
  * runtime cleanup when a MAC / portal is removed
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import LiveGenre, MacAddress, Portal, VodGenre
from app.services import mac_health as svc


async def _portal(*, macs: list[str], name: str = "multi") -> tuple[int, list[int]]:
    async with SessionLocal() as s:
        p = Portal(name=name, base_url="http://portal.invalid/c/",
                   resolved_url="http://portal.invalid/c/portal.php", enabled=True)
        s.add(p)
        await s.flush()
        ids = []
        for i, mac in enumerate(macs):
            m = MacAddress(portal_id=p.id, mac=mac, order=i, status="unknown", online=False)
            s.add(m)
            await s.flush()
            ids.append(m.id)
        await s.commit()
        return p.id, ids


async def test_refresh_portal_macs_writes_status_and_expiry(monkeypatch):
    pid, mids = await _portal(macs=["00:1A:79:11:11:01", "00:1A:79:11:11:02"])

    calls = []

    async def fake_refresh(portal, mac, *, url):
        calls.append(mac.mac)
        mac.status = "online" if mac.mac.endswith("01") else "expired"
        mac.online = mac.status == "online"
        mac.expire_date = "2030-01-01" if mac.online else "2020-01-01"
        mac.last_checked = datetime.now(timezone.utc)
        mac.last_error = None if mac.online else "expired 2020-01-01"
        return {"mac": mac.mac, "status": mac.status, "online": mac.online,
                "expire_date": mac.expire_date, "detail": mac.last_error or "ok"}

    monkeypatch.setattr(svc, "refresh_mac", fake_refresh)
    monkeypatch.setattr(svc, "_busy_mac_ids", lambda: set())

    rep = await svc.refresh_portal_macs(pid)
    assert rep["ok"] is True
    assert len(rep["results"]) == 2
    assert set(calls) == {"00:1A:79:11:11:01", "00:1A:79:11:11:02"}

    async with SessionLocal() as s:
        rows = {m.mac: m for m in (await s.execute(
            select(MacAddress).where(MacAddress.portal_id == pid)
        )).scalars().all()}
    assert rows["00:1A:79:11:11:01"].status == "online"
    assert rows["00:1A:79:11:11:01"].expire_date == "2030-01-01"
    assert rows["00:1A:79:11:11:02"].status == "expired"
    assert rows["00:1A:79:11:11:02"].online is False
    assert rows["00:1A:79:11:11:01"].last_checked is not None


async def test_refresh_skips_busy_macs_by_id(monkeypatch):
    """Busy is keyed by mac_id so redirect leases (no StreamHandle.mac) work too."""
    pid, mids = await _portal(macs=["00:1A:79:22:22:01", "00:1A:79:22:22:02"])
    busy_id = mids[0]
    monkeypatch.setattr(svc, "_busy_mac_ids", lambda: {busy_id})
    called = []

    async def fake_refresh(portal, mac, *, url):
        called.append(mac.mac)
        mac.status, mac.online = "online", True
        mac.last_checked = datetime.now(timezone.utc)
        return {"mac": mac.mac, "status": "online", "online": True}

    monkeypatch.setattr(svc, "refresh_mac", fake_refresh)
    rep = await svc.refresh_portal_macs(pid, skip_busy=True)
    assert called == ["00:1A:79:22:22:02"]
    assert rep["skipped_busy"] == 1
    skipped = next(r for r in rep["results"] if r["mac"].endswith("01"))
    assert skipped.get("skipped") == "busy"


async def test_redirect_lease_marks_mac_busy():
    """A 302/direct play must look busy to health + the next play, even without ffmpeg."""
    from app.services.stream_manager import MANAGER, REDIRECT_LEASE_S

    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    try:
        assert MANAGER.is_mac_busy(42) is False
        MANAGER.lease_mac(42, seconds=30)
        assert MANAGER.is_mac_busy(42) is True
        assert 42 in MANAGER.busy_mac_ids()
        # Expired lease is dropped lazily.
        MANAGER.redirect_leases[42] = __import__("time").monotonic() - 1
        assert MANAGER.is_mac_busy(42) is False
        assert 42 not in MANAGER.redirect_leases
        # ffmpeg lock still wins even with no lease.
        MANAGER.mac_locks[42] = "stream-x"
        assert MANAGER.is_mac_busy(42) is True
        MANAGER.release_mac(42)
        assert MANAGER.is_mac_busy(42) is False
        assert REDIRECT_LEASE_S >= 60  # long enough to cover a live zap window
    finally:
        MANAGER.mac_locks.clear()
        MANAGER.redirect_leases.clear()


async def test_refresh_all_only_multi_by_default(monkeypatch):
    multi, _ = await _portal(macs=["00:1A:79:33:33:01", "00:1A:79:33:33:02"], name="multi")
    single, _ = await _portal(macs=["00:1A:79:33:33:03"], name="single")
    seen = []

    async def fake_portal(pid, *, skip_busy=True):
        seen.append(pid)
        return {"ok": True, "portal_id": pid, "results": []}

    monkeypatch.setattr(svc, "refresh_portal_macs", fake_portal)
    await svc.refresh_all_macs(only_multi=True)
    assert multi in seen and single not in seen

    seen.clear()
    await svc.refresh_all_macs(only_multi=False)
    assert multi in seen and single in seen


async def test_compare_genres_reports_and_stores(monkeypatch):
    pid, mids = await _portal(macs=["00:1A:79:44:44:01", "00:1A:79:44:44:02", "00:1A:79:44:44:03"])
    # Mark first two online; third expired → skipped.
    async with SessionLocal() as s:
        for mid, st, on in zip(mids, ("online", "online", "expired"), (True, True, False)):
            m = await s.get(MacAddress, mid)
            m.status, m.online = st, on
        await s.commit()

    async def fake_genres(portal, mac, url):
        if mac.mac.endswith("01"):
            live = [{"id": "1", "title": "News"}, {"id": "2", "title": "Sport"}]
            vod = [{"id": "10", "title": "Action"}]
        else:
            live = [{"id": "1", "title": "News"}, {"id": "3", "title": "Kids"}]
            vod = [{"id": "10", "title": "Action"}, {"id": "11", "title": "Comedy"}]
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [{"key": svc._genre_key(g), "name": svc._genre_label(g),
                          "id": str(g.get("id") or "")} for g in live],
                "vod": [{"key": svc._genre_key(g), "name": svc._genre_label(g),
                         "id": str(g.get("id") or "")} for g in vod],
                "series": []}

    monkeypatch.setattr(svc, "_genres_for_mac", fake_genres)
    out = await svc.compare_genres(pid, mids)
    assert out["ok"] is True
    assert out["compared"] == 2
    assert len(out["skipped"]) == 1
    assert out["identical"] is False
    assert out["live"]["identical"] is False
    assert len(out["results"]) == 2
    assert len(out["packages"]) == 2  # each MAC has a distinct exact signature
    assert all(p["counts"]["live"] == 2 for p in out["packages"])
    # News is common; Sport only on 01; Kids only on 02.
    common_names = {g["name"] for g in out["live"]["common"]}
    assert "News" in common_names
    only = out["live"]["only"]
    assert any(g["name"] == "Sport" for g in only["00:1A:79:44:44:01"])
    assert any(g["name"] == "Kids" for g in only["00:1A:79:44:44:02"])

    # Union was written into the genre tables (disabled by default).
    assert out["stored"]["live"] == 3   # News, Sport, Kids
    assert out["stored"]["vod"] == 2    # Action, Comedy
    async with SessionLocal() as s:
        compared_macs = [await s.get(MacAddress, mid) for mid in mids]
        assert (compared_macs[0].genre_count_live,
                compared_macs[0].genre_count_vod,
                compared_macs[0].genre_count_series) == (2, 1, 0)
        assert (compared_macs[1].genre_count_live,
                compared_macs[1].genre_count_vod,
                compared_macs[1].genre_count_series) == (2, 2, 0)
        assert compared_macs[0].genres_compared_at is not None
        assert compared_macs[2].genres_compared_at is None  # expired/skipped
        live = {(r.genre_portal_id, r.name, r.enabled)
                for r in (await s.execute(select(LiveGenre).where(LiveGenre.portal_id == pid))).scalars()}
        vod = {(r.genre_portal_id, r.name, r.enabled)
               for r in (await s.execute(select(VodGenre).where(VodGenre.portal_id == pid))).scalars()}
    assert {("1", "News", False), ("2", "Sport", False), ("3", "Kids", False)} <= live
    assert {("10", "Action", False), ("11", "Comedy", False)} <= vod

    # Re-run with one genre already enabled — flag must be preserved.
    async with SessionLocal() as s:
        row = (await s.execute(select(LiveGenre).where(
            LiveGenre.portal_id == pid, LiveGenre.genre_portal_id == "1"))).scalar_one()
        row.enabled = True
        await s.commit()
    out2 = await svc.compare_genres(pid)
    assert out2["stored"]["live"] == 3
    async with SessionLocal() as s:
        row = (await s.execute(select(LiveGenre).where(
            LiveGenre.portal_id == pid, LiveGenre.genre_portal_id == "1"))).scalar_one()
        assert row.enabled is True

    # A partial later comparison updates successful kinds but does not turn a
    # failed VOD request into a persisted zero.
    await svc._persist_mac_genre_counts([{
        "mac_id": mids[0], "ok": True, "live": [], "vod": [], "series": [],
        "vod_error": "temporary failure",
    }])
    async with SessionLocal() as s:
        mac = await s.get(MacAddress, mids[0])
        assert mac.genre_count_live == 0
        assert mac.genre_count_vod == 1
        assert mac.genre_count_series == 0


async def test_compare_genres_identical_when_same_package(monkeypatch):
    pid, mids = await _portal(macs=["00:1A:79:55:55:01", "00:1A:79:55:55:02"])
    async with SessionLocal() as s:
        for mid in mids:
            m = await s.get(MacAddress, mid)
            m.status, m.online = "online", True
        await s.commit()

    async def fake_genres(portal, mac, url):
        live = [{"id": "1", "title": "News"}, {"id": "2", "title": "Sport"}]
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [{"key": svc._genre_key(g), "name": svc._genre_label(g),
                          "id": str(g.get("id") or "")} for g in live],
                "vod": [], "series": []}

    monkeypatch.setattr(svc, "_genres_for_mac", fake_genres)
    out = await svc.compare_genres(pid)
    assert out["identical"] is True
    assert out["live"]["identical"] is True
    assert out["live"]["only"] == {}
    assert len(out["packages"]) == 1
    assert len(out["packages"][0]["mac_ids"]) == 2
    assert out["stored"]["live"] == 2


async def test_compare_genres_single_mac_is_noop():
    pid, _ = await _portal(macs=["00:1A:79:66:66:01"], name="solo")
    out = await svc.compare_genres(pid)
    assert out["ok"] is True
    assert out["compared"] == 0
    assert out["identical"] is True
    assert "fewer than 2" in (out.get("message") or "")


# ---------------------------------------------------------------- channel ids
def test_diff_channel_ids_classifies_differ_and_missing():
    """Same name + different id is 'differ'; absent on a MAC is 'missing'."""
    per_mac = [
        {"mac": "M1", "ok": True, "live": [
            {"id": "10", "name": "BBC One", "key": "bbc one", "genre": "1", "cmd": "cmd10"},
            {"id": "11", "name": "CNN", "key": "cnn", "genre": "1", "cmd": "cmd11"},
            {"id": "12", "name": "OnlyA", "key": "onlya", "genre": "1", "cmd": ""},
        ]},
        {"mac": "M2", "ok": True, "live": [
            {"id": "99", "name": "BBC One", "key": "bbc one", "genre": "1", "cmd": "cmd99"},
            {"id": "11", "name": "CNN", "key": "cnn", "genre": "1", "cmd": "cmd11"},
        ]},
    ]
    d = svc._diff_channel_ids(per_mac, "live")
    assert d["counts"] == {"same": 1, "differ": 1, "missing": 1}
    assert d["identical"] is False
    by_name = {r["name"]: r for r in d["rows"]}
    assert by_name["BBC One"]["status"] == "differ"
    assert by_name["BBC One"]["ids"] == {"M1": "10", "M2": "99"}
    assert by_name["OnlyA"]["status"] == "missing"
    assert by_name["OnlyA"]["absent"] == ["M2"]
    # Actionable id-conflict rows come first.
    assert d["rows"][0]["status"] == "differ"
    # CNN matched everywhere → only in counts, not the row list.
    assert "CNN" not in by_name


def test_diff_channel_ids_identical_and_kind_error():
    same = [
        {"mac": "M1", "ok": True, "live": [{"id": "1", "name": "A", "key": "a",
                                            "genre": "1", "cmd": ""}]},
        {"mac": "M2", "ok": True, "live": [{"id": "1", "name": "A", "key": "a",
                                            "genre": "1", "cmd": ""}]},
    ]
    d = svc._diff_channel_ids(same, "live")
    assert d["counts"] == {"same": 1, "differ": 0, "missing": 0}
    assert d["identical"] is True
    assert d["rows"] == []

    # A MAC whose live listing failed is reported under failed, not as
    # 'every channel missing on it'.
    partial = same + [{"mac": "M3", "ok": True, "error": "",
                       "live_error": "timeout", "live": []}]
    d2 = svc._diff_channel_ids(partial, "live")
    assert d2["counts"] == {"same": 1, "differ": 0, "missing": 0}
    assert d2["failed"] == {"M3": "timeout"}
    assert d2["macs"] == ["M1", "M2"]


def test_resolve_enabled_for_mac_id_then_name():
    """Packages may renumber genre ids: match id first, then name."""
    enabled = [("5", "News"), ("9", "Sport")]
    mac_view = [
        {"id": "5", "name": "News"},
        {"id": "77", "name": "Sport"},   # renumbered on this package
        {"id": "8", "name": "Kids"},     # not enabled
    ]
    assert svc._resolve_enabled_for_mac(enabled, mac_view) == ["5", "77"]

    # Genre this MAC cannot see at all → dropped (package difference).
    assert svc._resolve_enabled_for_mac([("42", "Cinema")], mac_view) == []


async def test_compare_genres_channel_ids_runs_when_packages_differ(monkeypatch):
    pid, mids = await _portal(macs=["00:1A:79:AA:AA:01", "00:1A:79:AA:AA:02"])
    async with SessionLocal() as s:
        for mid in mids:
            m = await s.get(MacAddress, mid)
            m.status, m.online = "online", True
        s.add(LiveGenre(portal_id=pid, genre_portal_id="1", name="News", enabled=True))
        await s.commit()

    async def fake_genres(portal, mac, url):
        if mac.mac.endswith("01"):
            live = [{"id": "1", "title": "News"}, {"id": "2", "title": "Sport"}]
        else:
            live = [{"id": "1", "title": "News"}, {"id": "3", "title": "Kids"}]
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [{"key": svc._genre_key(g), "name": svc._genre_label(g),
                          "id": str(g.get("id") or "")} for g in live],
                "vod": [], "series": []}

    channel_calls = []

    async def fake_channels(portal, mac, url, kinds, genre_ids, budget):
        channel_calls.append((mac.mac, kinds, dict(genre_ids), budget))
        # Same channel name, different id per package — the hazard case.
        row = ({"id": "10", "name": "BBC One", "key": "bbc one",
                "genre": "1", "cmd": "cmdA"} if mac.mac.endswith("01")
               else {"id": "99", "name": "BBC One", "key": "bbc one",
                     "genre": "1", "cmd": "cmdB"})
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [row], "vod": [], "series": []}

    monkeypatch.setattr(svc, "_genres_for_mac", fake_genres)
    monkeypatch.setattr(svc, "_channels_for_mac", fake_channels)

    out = await svc.compare_genres(pid, mids, channel_kinds=["live"])
    assert out["ok"] is True
    assert out["identical"] is False          # packages differ
    ch = out["channel_ids"]
    assert ch["requested"] == ["live"]
    assert ch["enabled"] == {"live": 1}
    live_blk = ch["kinds"]["live"]
    assert live_blk["counts"]["differ"] == 1
    assert live_blk["rows"][0]["ids"] == {"00:1A:79:AA:AA:01": "10",
                                          "00:1A:79:AA:AA:02": "99"}
    assert "different ids" in out["message"]
    assert len(channel_calls) == 2
    # Live was requested and the enabled genre resolved per MAC.
    assert all(kinds == ["live"] for _m, kinds, _g, _b in channel_calls)
    assert all(gids == {"live": ["1"]} for _m, _k, gids, _b in channel_calls)


async def test_compare_genres_channel_ids_skipped_when_identical(monkeypatch):
    pid, mids = await _portal(macs=["00:1A:79:BB:BB:01", "00:1A:79:BB:BB:02"])
    async with SessionLocal() as s:
        for mid in mids:
            m = await s.get(MacAddress, mid)
            m.status, m.online = "online", True
        await s.commit()

    async def fake_genres(portal, mac, url):
        live = [{"id": "1", "title": "News"}]
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [{"key": svc._genre_key(g), "name": svc._genre_label(g),
                          "id": str(g.get("id") or "")} for g in live],
                "vod": [], "series": []}

    async def no_channels(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("_channels_for_mac must not run on identical packages")

    monkeypatch.setattr(svc, "_genres_for_mac", fake_genres)
    monkeypatch.setattr(svc, "_channels_for_mac", no_channels)

    out = await svc.compare_genres(pid, mids, channel_kinds=["live", "vod"])
    assert out["ok"] is True
    assert out["identical"] is True
    ch = out["channel_ids"]
    assert ch["kinds"] == {}
    assert "identical" in ch["message"]


async def test_compare_genres_channel_ids_none_when_not_requested(monkeypatch):
    """channel_kinds=None/[] stays a genre-only compare (legacy callers)."""
    pid, mids = await _portal(macs=["00:1A:79:CC:CC:01", "00:1A:79:CC:CC:02"])
    async with SessionLocal() as s:
        for mid in mids:
            m = await s.get(MacAddress, mid)
            m.status, m.online = "online", True
        await s.commit()

    async def fake_genres(portal, mac, url):
        live = ([{"id": "1", "title": "News"}] if mac.mac.endswith("01")
                else [{"id": "3", "title": "Kids"}])
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [{"key": svc._genre_key(g), "name": svc._genre_label(g),
                          "id": str(g.get("id") or "")} for g in live],
                "vod": [], "series": []}

    async def no_channels(*a, **k):  # pragma: no cover
        raise AssertionError("_channels_for_mac must not run without channel_kinds")

    monkeypatch.setattr(svc, "_genres_for_mac", fake_genres)
    monkeypatch.setattr(svc, "_channels_for_mac", no_channels)

    out = await svc.compare_genres(pid, mids)
    assert out["channel_ids"] is None
    out2 = await svc.compare_genres(pid, mids, channel_kinds=[])
    assert out2["channel_ids"] is None


async def test_api_compare_genres_passes_channel_kinds(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.services import mac_health

    pid, mids = await _portal(macs=["00:1A:79:DD:DD:01", "00:1A:79:DD:DD:02"])
    async with SessionLocal() as s:
        for mid in mids:
            m = await s.get(MacAddress, mid)
            m.status, m.online = "online", True
        s.add(LiveGenre(portal_id=pid, genre_portal_id="1", name="News", enabled=True))
        await s.commit()

    seen_kinds = []

    async def fake_genres(portal, mac, url):
        live = ([{"id": "1", "title": "News"}, {"id": "2", "title": "Sport"}]
                if mac.mac.endswith("01")
                else [{"id": "1", "title": "News"}, {"id": "3", "title": "Kids"}])
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [{"key": mac_health._genre_key(g),
                          "name": mac_health._genre_label(g),
                          "id": str(g.get("id") or "")} for g in live],
                "vod": [], "series": []}

    async def fake_channels(portal, mac, url, kinds, genre_ids, budget):
        seen_kinds.append(list(kinds))
        return {"mac": mac.mac, "mac_id": mac.id, "ok": True, "error": "",
                "live": [{"id": "10", "name": "BBC One", "key": "bbc one",
                          "genre": "1", "cmd": ""}],
                "vod": [], "series": []}

    monkeypatch.setattr(mac_health, "_genres_for_mac", fake_genres)
    monkeypatch.setattr(mac_health, "_channels_for_mac", fake_channels)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(f"/api/portals/{pid}/compare-genres",
                         json={"mac_ids": mids, "channel_kinds": ["live"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["channel_ids"]["requested"] == ["live"]
        assert seen_kinds == [["live"], ["live"]]   # one call per compared MAC

        # Bad channel_kinds → 400, genre phase never starts.
        r2 = await c.post(f"/api/portals/{pid}/compare-genres",
                          json={"channel_kinds": "live"})
        assert r2.status_code == 400


async def test_removing_mac_clears_runtime_state():
    """PUT /api/portals/{id} dropping a MAC must free locks + pool sessions."""
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.portal.pool import POOL, PortalSession
    from app.services.stream_manager import MANAGER

    pid, mids = await _portal(macs=["00:1A:79:88:88:01", "00:1A:79:88:88:02"], name="cleanup")
    gone_id, keep_id = mids
    MANAGER.mac_locks[gone_id] = "ghost-stream"
    MANAGER.lease_mac(gone_id, seconds=60)
    # Seed a pooled session keyed on the MAC about to disappear.
    session = PortalSession(portal_url="http://portal.invalid/c/portal.php",
                            mac="00:1A:79:88:88:01")
    await POOL.get(session)
    assert any(k[1] == "00:1A:79:88:88:01" for k in POOL._clients), "precondition: session pooled"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.put(f"/api/portals/{pid}", json={
            "name": "cleanup",
            "base_url": "http://portal.invalid/c/",
            "macs": "00:1A:79:88:88:02",          # drop …01
        })
        assert r.status_code == 200, r.text

    assert gone_id not in MANAGER.mac_locks
    assert gone_id not in MANAGER.redirect_leases
    assert not any(k[1] == "00:1A:79:88:88:01" for k in POOL._clients)
    async with SessionLocal() as s:
        left = (await s.execute(select(MacAddress).where(MacAddress.portal_id == pid))).scalars().all()
    assert [m.mac for m in left] == ["00:1A:79:88:88:02"]
    assert keep_id == left[0].id


async def test_deleting_portal_cascades_and_clears_runtime():
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.portal.pool import POOL, PortalSession
    from app.services.stream_manager import MANAGER

    pid, mids = await _portal(macs=["00:1A:79:99:99:01", "00:1A:79:99:99:02"], name="gone")
    # Plant genre + lock + pool leftovers that must not survive.
    async with SessionLocal() as s:
        s.add(LiveGenre(portal_id=pid, genre_portal_id="1", name="News", enabled=True))
        s.add(VodGenre(portal_id=pid, genre_portal_id="10", name="Action", enabled=False))
        await s.commit()
    for mid in mids:
        MANAGER.mac_locks[mid] = "x"
        MANAGER.lease_mac(mid, seconds=60)
    await POOL.get(PortalSession(portal_url="http://portal.invalid/c/portal.php",
                                 mac="00:1A:79:99:99:01"))
    await POOL.get(PortalSession(portal_url="http://portal.invalid/c/",
                                 mac="00:1A:79:99:99:02"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.delete(f"/api/portals/{pid}")
        assert r.status_code == 200, r.text

    async with SessionLocal() as s:
        assert await s.get(Portal, pid) is None
        assert (await s.execute(select(MacAddress).where(MacAddress.portal_id == pid))).scalars().first() is None
        assert (await s.execute(select(LiveGenre).where(LiveGenre.portal_id == pid))).scalars().first() is None
        assert (await s.execute(select(VodGenre).where(VodGenre.portal_id == pid))).scalars().first() is None
    for mid in mids:
        assert mid not in MANAGER.mac_locks
        assert mid not in MANAGER.redirect_leases
    # Sessions for both the resolved URL and the base URL are gone.
    assert not any(k[0].startswith("http://portal.invalid") and "99:99" in k[1]
                   for k in POOL._clients)


async def test_api_endpoints_exist():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    pid, mids = await _portal(macs=["00:1A:79:77:77:01", "00:1A:79:77:77:02"])
    async with SessionLocal() as s:
        for mid in mids:
            m = await s.get(MacAddress, mid)
            m.status, m.online = "online", True
        await s.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/portals/mac-health/refresh", json={"portal_id": pid})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("portal_id") == pid or body.get("ok") is not None

        r2 = await c.post(f"/api/portals/{pid}/compare-genres")
        assert r2.status_code == 200, r2.text
        assert "live" in r2.json()
        assert "stored" in r2.json()
