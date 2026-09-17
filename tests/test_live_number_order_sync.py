"""Channel number <-> final playlist order, kept two ways for live channels.

The 'Channel number (opt)' of the Edit-channel popup is the channel's
position in the final playlist: saving a new number moves the channel to
that position, and reordering / deleting / toggling the playlist updates
the numbers to match.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import LivePlaylist, Portal


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _seed(n: int = 4, disable_last: bool = False) -> list[int]:
    """n enabled channels, number = position; optionally one disabled row."""
    async with SessionLocal() as s:
        s.add(Portal(name="p", base_url="http://p.invalid"))
        await s.flush()
        for i in range(1, n + 1):
            s.add(LivePlaylist(custom_name=f"Ch {i}", number=i, enabled=True, order=i))
        await s.flush()
        if disable_last:
            last = (await s.execute(select(LivePlaylist).where(
                LivePlaylist.custom_name == f"Ch {n}"))).scalar_one()
            last.enabled = False
        await s.commit()
        rows = (await s.execute(select(LivePlaylist).order_by(
            LivePlaylist.order, LivePlaylist.id))).scalars().all()
        return [r.id for r in rows]


async def _final() -> list[tuple[str, int]]:
    """The final playlist as the M3U sees it: enabled, by (order, id);
    each row as (name, number)."""
    async with SessionLocal() as s:
        rows = (await s.execute(select(LivePlaylist)
                 .where(LivePlaylist.enabled.is_(True))
                 .order_by(LivePlaylist.order, LivePlaylist.id))).scalars().all()
        return [(r.custom_name, r.number) for r in rows]


async def _get(custom_name: str) -> LivePlaylist:
    async with SessionLocal() as s:
        return (await s.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == custom_name))).scalar_one()


def _assert_numbers_match(final: list[tuple[str, int]]) -> None:
    for i, (name, number) in enumerate(final, 1):
        assert number == i, f"{name}: number {number} != position {i}"


async def test_number_edit_moves_channel_and_renumbers():
    ids = await _seed(4)
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[3]}", json={"number": 2})
        assert r.status_code == 200, r.text
    final = await _final()
    # Ch 4 jumped to position 2, pushing Ch 2 / Ch 3 down
    assert [name for name, _ in final] == ["Ch 1", "Ch 4", "Ch 2", "Ch 3"]
    _assert_numbers_match(final)


async def test_dnd_reorder_updates_numbers():
    """the 'visa versa' direction: moving the playlist updates the numbers"""
    ids = await _seed(4)
    async with _client() as c:
        r = await c.post("/api/playlist/live/order", json={"items": [
            {"id": ids[1], "order": 1},   # Ch 2 first
            {"id": ids[0], "order": 2},   # Ch 1
            {"id": ids[2], "order": 3},   # Ch 3
            {"id": ids[3], "order": 4},   # Ch 4
        ]})
        assert r.status_code == 200, r.text
    final = await _final()
    assert [name for name, _ in final] == ["Ch 2", "Ch 1", "Ch 3", "Ch 4"]
    _assert_numbers_match(final)


async def test_delete_renumbers_following_channels():
    ids = await _seed(4)
    async with _client() as c:
        r = await c.delete(f"/api/playlist/live/{ids[1]}")   # drop Ch 2
        assert r.status_code == 200, r.text
    final = await _final()
    assert [name for name, _ in final] == ["Ch 1", "Ch 3", "Ch 4"]
    _assert_numbers_match(final)


async def test_enabling_and_disabling_keeps_numbers_in_sync():
    ids = await _seed(3, disable_last=True)   # Ch 3 disabled
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[2]}", json={"enabled": True})
        assert r.status_code == 200, r.text
    final = await _final()
    assert [name for name, _ in final] == ["Ch 1", "Ch 2", "Ch 3"]
    _assert_numbers_match(final)
    # and the way back: disabling Ch 1 shifts the rest up
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[0]}", json={"enabled": False})
        assert r.status_code == 200, r.text
    final = await _final()
    assert [name for name, _ in final] == ["Ch 2", "Ch 3"]
    _assert_numbers_match(final)
    # the disabled channel keeps its number, it just has no position
    assert (await _get("Ch 1")).number == 1


async def test_number_is_clamped_to_the_list():
    ids = await _seed(4)
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[0]}", json={"number": 999})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final[0][0] == "Ch 2" and final[-1][0] == "Ch 1"
    _assert_numbers_match(final)
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[0]}", json={"number": 0})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final[0][0] == "Ch 1"
    _assert_numbers_match(final)


async def test_create_appends_with_its_position_number():
    await _seed(3)
    async with _client() as c:
        r = await c.post("/api/playlist/live",
                         json={"custom_name": "New", "source_ids": []})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final[-1] == ("New", 4)
    _assert_numbers_match(final)
    # ...and creating with a number already set moves it straight there
    async with _client() as c:
        r = await c.post("/api/playlist/live",
                         json={"custom_name": "Jump", "number": 1, "source_ids": []})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final[0][0] == "Jump"
    _assert_numbers_match(final)


async def test_disabled_channel_number_edit_does_not_move_it():
    ids = await _seed(3, disable_last=True)
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[2]}", json={"number": 2})
        assert r.status_code == 200, r.text
    # disabled = not in the final playlist: the value is stored, nothing moves
    row = await _get("Ch 3")
    assert row.number == 2 and row.order == 3 and row.enabled is False
    final = await _final()
    _assert_numbers_match(final)
    # enabling it re-derives the number from its actual position
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[2]}", json={"enabled": True})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final[-1] == ("Ch 3", 3)
    _assert_numbers_match(final)


# ---------------------------------------------------------------- locked numbers
async def _lock(custom_name: str, lock: bool = True) -> None:
    row = await _get(custom_name)
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{row.id}", json={"lock_number": lock})
        assert r.status_code == 200, r.text


def _assert_distinct(final: list[tuple[str, int]]) -> None:
    numbers = [n for _, n in final]
    assert len(numbers) == len(set(numbers)), f"duplicate numbers: {final}"


async def test_lock_survives_reorder():
    """a locked channel keeps its number while the others renumber around it"""
    ids = await _seed(4)
    await _lock("Ch 2")                                  # frozen at 2
    async with _client() as c:
        r = await c.post("/api/playlist/live/order", json={"items": [
            {"id": ids[3], "order": 1},   # Ch 4 dragged to the front,
            {"id": ids[0], "order": 2},   # past the locked Ch 2
            {"id": ids[1], "order": 3},
            {"id": ids[2], "order": 4},
        ]})
        assert r.status_code == 200, r.text
    final = await _final()
    assert [name for name, _ in final] == ["Ch 4", "Ch 1", "Ch 2", "Ch 3"]
    # Ch 2 keeps 2; the others take the free numbers in list order
    assert final == [("Ch 4", 1), ("Ch 1", 3), ("Ch 2", 2), ("Ch 3", 4)]
    _assert_distinct(final)


async def test_lock_survives_delete():
    ids = await _seed(4)
    await _lock("Ch 2")
    async with _client() as c:
        r = await c.delete(f"/api/playlist/live/{ids[0]}")   # drop Ch 1
        assert r.status_code == 200, r.text
    final = await _final()
    assert [name for name, _ in final] == ["Ch 2", "Ch 3", "Ch 4"]
    assert final == [("Ch 2", 2), ("Ch 3", 1), ("Ch 4", 3)]
    _assert_distinct(final)


async def test_lock_survives_enable_disable():
    ids = await _seed(3)
    await _lock("Ch 2")
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[0]}", json={"enabled": False})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final == [("Ch 2", 2), ("Ch 3", 1)]
    _assert_distinct(final)
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[0]}", json={"enabled": True})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final == [("Ch 1", 1), ("Ch 2", 2), ("Ch 3", 3)]
    _assert_distinct(final)


async def test_number_edit_on_a_locked_channel_is_ignored():
    ids = await _seed(4)
    await _lock("Ch 2")
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[1]}", json={"number": 1})
        assert r.status_code == 200, r.text
    final = await _final()
    # the locked channel neither moved nor renumbered
    assert final == [("Ch 1", 1), ("Ch 2", 2), ("Ch 3", 3), ("Ch 4", 4)]
    _assert_distinct(final)


async def test_locking_pins_the_current_number():
    ids = await _seed(4)
    await _lock("Ch 4")                                  # frozen at 4
    async with _client() as c:
        r = await c.post("/api/playlist/live/order", json={"items": [
            {"id": ids[1], "order": 1},
            {"id": ids[2], "order": 2},
            {"id": ids[3], "order": 3},
            {"id": ids[0], "order": 4},   # Ch 1 dragged to the end
        ]})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final == [("Ch 2", 1), ("Ch 3", 2), ("Ch 4", 4), ("Ch 1", 3)]
    assert (await _get("Ch 4")).lock_number is True
    _assert_distinct(final)
    # unlocking again lets the number follow the position
    await _lock("Ch 4", lock=False)
    final = await _final()
    assert final == [("Ch 2", 1), ("Ch 3", 2), ("Ch 4", 3), ("Ch 1", 4)]


async def test_create_with_lock_and_number_lands_there_then_freezes():
    ids = await _seed(3)
    async with _client() as c:
        r = await c.post("/api/playlist/live", json={
            "custom_name": "New", "number": 2, "lock_number": True, "source_ids": []})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final == [("Ch 1", 1), ("New", 2), ("Ch 2", 3), ("Ch 3", 4)]
    # ...and the freeze survives a later reorder
    async with _client() as c:
        r = await c.post("/api/playlist/live/order", json={"items": [
            {"id": r.json()["id"], "order": 1},
            {"id": ids[1], "order": 2},
            {"id": ids[2], "order": 3},
            {"id": ids[0], "order": 4},
        ]})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final == [("New", 2), ("Ch 2", 1), ("Ch 3", 3), ("Ch 1", 4)]
    _assert_distinct(final)


async def test_move_to_a_locked_number_lands_on_the_next_free_one():
    ids = await _seed(4)
    await _lock("Ch 2")                                  # 2 is taken
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[0]}", json={"number": 2})
        assert r.status_code == 200, r.text
    final = await _final()
    # Ch 1 asked for 2, got 3 (the next free number)
    assert final == [("Ch 2", 2), ("Ch 3", 1), ("Ch 1", 3), ("Ch 4", 4)]
    _assert_distinct(final)


async def test_unlock_rederives_a_diverged_number():
    """lock, let the position diverge from the number, unlock - the channel
    flows back with the renumber"""
    ids = await _seed(4)
    await _lock("Ch 3")                                  # frozen at 3
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[3]}", json={"number": 2})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final == [("Ch 1", 1), ("Ch 4", 2), ("Ch 2", 4), ("Ch 3", 3)]
    async with _client() as c:
        r = await c.put(f"/api/playlist/live/{ids[2]}", json={"lock_number": False})
        assert r.status_code == 200, r.text
    final = await _final()
    assert final == [("Ch 1", 1), ("Ch 4", 2), ("Ch 2", 3), ("Ch 3", 4)]
    _assert_distinct(final)


async def test_live_list_carries_lock_number():
    ids = await _seed(3)
    await _lock("Ch 2")
    async with _client() as c:
        r = await c.get("/api/playlist/live")
        assert r.status_code == 200, r.text
        items = {i["custom_name"]: i for i in r.json()["items"]}
    assert items["Ch 2"]["lock_number"] is True
    assert items["Ch 1"]["lock_number"] is False


def test_gui_wires_lock_controls():
    """table lock column, draggable lockout and the popup checkbox are all
    actually in the shipped pages (a broken template renders fine and the
    table simply loses the feature)"""
    from pathlib import Path
    root = Path(__file__).parent.parent
    html = (root / "app/templates/playlist.html").read_text()
    assert "isLocked: (r) => r.lock_number" in html          # locked rows are not draggable
    assert "toggleLock(${r.id}, ${!r.lock_number})" in html   # the table lock column
    assert 'id="cc-lock"' in html                            # the popup checkbox
    assert 'lock_number: $("#cc-lock", body).checked' in html  # the save payload
    assert "row?.lock_number ? \"disabled" in html           # number field read-only while locked
    js = (root / "app/static/js/app.js").read_text()
    assert "o.dnd.isLocked?.(row)" in js                     # the dnd gate honours it
    assert "row-locked" in js                                # grip -> lock icon
    assert "window.toggleLock" in html
