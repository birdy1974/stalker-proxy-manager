"""
Xtream players probe `/?username=&password=` for identity.

A previous `root_dispatch` called `UserAuth.verify(db, u, p, "xtream")` and
unpacked `(ok, user)`, but `verify` takes `(username, password, need)` and
returns `User | None`. Every probe 500'd. These tests pin the contract:
  * no credentials  -> 303 to the GUI
  * bad credentials -> 401 JSON (never HTML, never 500)
  * good xtream user -> player_api-style identity (auth=1, username, server_info)
  * xtream_enabled=False -> 401 even with a matching password
"""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import User

BASE = "http://testserver"


async def _user(**kwargs) -> User:
    defaults = dict(name="xtu", password="secret", enabled=True,
                    m3u_enabled=True, xtream_enabled=True, max_connections=2)
    defaults.update(kwargs)
    async with SessionLocal() as s:
        u = User(**defaults)
        s.add(u)
        await s.commit()
        return u


async def test_root_without_credentials_redirects_to_the_gui():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/dashboard")


async def test_root_with_bad_credentials_is_401_json_not_500():
    await _user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get("/?username=xtu&password=WRONG")
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["user_info"]["auth"] == 0
    assert "html" not in r.headers.get("content-type", "").lower()


async def test_root_with_valid_xtream_user_returns_player_api_identity():
    await _user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        root = await c.get("/?username=xtu&password=secret")
        api = await c.get("/player_api.php?username=xtu&password=secret")
    assert root.status_code == 200, f"got {root.status_code}: {root.text}"
    body = root.json()
    info = body["user_info"]
    assert info["auth"] == 1
    assert info["username"] == "xtu"
    assert info["password"] == "secret"
    assert info["status"] == "Active"
    assert info["max_connections"] == "2"
    assert "active_cons" in info
    assert "server_info" in body
    # same shape as the empty-action player_api.php probe
    assert api.status_code == 200
    assert api.json()["user_info"]["username"] == "xtu"
    assert api.json()["user_info"]["auth"] == 1


async def test_root_rejects_a_user_with_xtream_disabled():
    await _user(xtream_enabled=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get("/?username=xtu&password=secret")
    assert r.status_code == 401
    assert r.json()["user_info"]["auth"] == 0
