"""Recent-success routing and temporary source circuit breaking."""

from __future__ import annotations

from types import SimpleNamespace

from app.portal.client import StalkerClient
from app.portal.pool import ClientPool, PortalSession
from app.services import stream_manager as stream_module
from app.services.stream_manager import _RouteHealth


class Source:
    def __init__(self, source_id: int):
        self.id = source_id


def step(source_id: int, *mac_ids: int):
    source = Source(source_id)
    macs = [SimpleNamespace(id=mac_id) for mac_id in mac_ids]
    return source, SimpleNamespace(id=source_id), macs


def test_recent_success_promotes_source_and_mac_without_rewriting_priority():
    health = _RouteHealth()
    route = ("live", 42)
    first, second = step(1, 11, 12), step(2, 21, 22)

    health.succeeded(route, second[0], second[2][1])
    ordered = health.ordered_chain(route, [first, second])
    assert [row[0].id for row in ordered] == [2, 1]
    assert [mac.id for mac in health.ordered_macs(route, second[0], second[2])] == [22, 21]
    # A different playlist retains its configured source/MAC order.
    assert health.ordered_chain(("live", 99), [first, second]) == [first, second]
    assert health.ordered_macs(("live", 99), second[0], second[2]) == second[2]


def test_repeated_failure_temporarily_suppresses_source_but_keeps_half_open(monkeypatch):
    monkeypatch.setattr(stream_module, "SOURCE_BREAKER_FAILURES", 2)
    monkeypatch.setattr(stream_module, "SOURCE_BREAKER_COOLDOWN", 60.0)
    health = _RouteHealth()
    bad, good = step(1, 11), step(2, 21)
    health.failed(bad[0])
    assert health.ordered_chain(None, [bad, good]) == [bad, good]
    health.failed(bad[0])
    assert health.ordered_chain(None, [bad, good]) == [good]

    health.failed(good[0])
    health.failed(good[0])
    # Never turn a temporary breaker into an immediate empty-chain 404.
    assert health.ordered_chain(None, [bad, good]) == [bad]
    health.succeeded(None, bad[0], bad[2][0])
    assert health.ordered_chain(None, [bad, good]) == [bad]


async def test_pool_warm_authenticates_concurrently_and_isolates_failures(monkeypatch):
    pool = ClientPool()
    calls: list[str] = []

    async def auth(self):
        calls.append(self.mac)
        if self.mac.endswith("02"):
            raise OSError("portal unavailable")
        self._token = "ready"

    monkeypatch.setattr(StalkerClient, "ensure_auth", auth)
    sessions = [
        PortalSession("http://portal/c/portal.php", "00:1A:79:00:00:01"),
        PortalSession("http://portal/c/portal.php", "00:1A:79:00:00:02"),
    ]
    assert await pool.warm(sessions, concurrency=2) == {"ready": 1, "failed": 1}
    assert sorted(calls) == sorted(session.mac for session in sessions)
    assert pool.stats()["sessions_open"] == 2
    await pool.close_all()
