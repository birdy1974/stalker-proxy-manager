"""Input Sources bulk enable must allocate distinct playlist order positions."""
import asyncio

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (LivePlaylist, LiveSource, LocalFile, LocalPlaylist, LocalSource,
                        Portal, SeriePlaylist, SerieSource, VodPlaylist, VodSource)
from app.services import playlist_sync

MODELS = {'live': LivePlaylist, 'vod': VodPlaylist, 'series': SeriePlaylist, 'local': LocalPlaylist}


async def seed(kind, count=6, enabled=False):
    async with SessionLocal() as db:
        parent = LocalSource(directory='/tmp/order-tests') if kind == 'local' else Portal(
            name='Order test', base_url='http://order.invalid')
        db.add(parent)
        await db.flush()
        rows = []
        for i in range(count):
            name = f'Channel {i:03}'
            if kind == 'local':
                src = LocalFile(local_source_id=parent.id, relative_path=f'{name}.mkv', filename=f'{name}.mkv', enabled=enabled)
            elif kind == 'live':
                src = LiveSource(portal_id=parent.id, portal_channel_id=str(i), original_name=name, enabled=enabled)
            else:
                cls = VodSource if kind == 'vod' else SerieSource
                src = cls(portal_id=parent.id, portal_item_id=str(i), original_name=name, enabled=enabled)
                if kind == 'series':
                    src.seasons_fetched = True
            db.add(src)
            rows.append(src)
        await db.commit()
        return [r.id for r in rows]


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def toggle(c, kind, ids, enabled=True):
    url = '/api/sources/local/files/toggle' if kind == 'local' else '/api/sources/toggle'
    response = await c.post(url, json={'kind':kind, 'ids':ids, 'enabled':enabled})
    assert response.status_code == 200, response.text
    return response.json()


async def listed(c, kind):
    response = await c.get(f'/api/playlist/{kind}?per_page=200&sort=order')
    assert response.status_code == 200, response.text
    return response.json()['items']


@pytest.mark.parametrize('kind', MODELS)
async def test_bulk_enable_appends_distinct_orders_and_reenable_keeps_positions(kind):
    ids = await seed(kind)
    async with client() as c:
        await toggle(c, kind, ids[:2])
        await toggle(c, kind, ids[2:])
        before = await listed(c, kind)
        assert [r['order'] for r in before] == list(range(1, 7))
        if kind == 'live':
            assert [r['number'] for r in before] == list(range(1, 7))
        await toggle(c, kind, ids, False)
        await toggle(c, kind, ids)
        after = await listed(c, kind)
        assert [(r['id'], r['order']) for r in after] == [(r['id'], r['order']) for r in before]


@pytest.mark.parametrize('kind', MODELS)
async def test_bulk_enable_repairs_existing_duplicate_orders_without_reordering(kind):
    ids = await seed(kind)
    async with client() as c:
        await toggle(c, kind, ids[:3])
        async with SessionLocal() as db:
            rows = (await db.scalars(select(MODELS[kind]).order_by(MODELS[kind].id))).all()
            for row in rows:
                row.order = 1
            rows[1].enabled = False  # disabled positions must not collide on re-enable
            if kind == 'live':
                rows[0].lock_number = True
                rows[0].number = 50
            await db.commit()
        old_ids = [r['id'] for r in await listed(c, kind)]
        await toggle(c, kind, ids)
        rows = await listed(c, kind)
        assert [r['order'] for r in rows] == list(range(1, 7))
        assert [r['id'] for r in rows[:3]] == old_ids
        if kind == 'live':
            assert rows[0]['number'] == 50
            enabled = [r['number'] for r in rows if r['enabled']]
            assert len(set(enabled)) == len(enabled)


@pytest.mark.parametrize('kind', MODELS)
async def test_overlapping_bulk_enables_do_not_allocate_the_same_order(kind, monkeypatch):
    ids = await seed(kind, enabled=True)
    original = playlist_sync._next_order
    async def delayed_next_order(*args, **kwargs):
        value = await original(*args, **kwargs)
        # Widen the existing read-MAX/write gap, without a barrier that would
        # deadlock a correct implementation which serializes transactions.
        await asyncio.sleep(0.05)
        return value
    monkeypatch.setattr(playlist_sync, '_next_order', delayed_next_order)
    async with client() as c:
        await asyncio.gather(toggle(c, kind, ids[:3]), toggle(c, kind, ids[3:]))
        rows = await listed(c, kind)
        assert [r['order'] for r in rows] == list(range(1, 7))


@pytest.mark.parametrize('kind', MODELS)
async def test_bulk_selection_order_is_preserved_and_duplicate_ids_are_ignored(kind):
    ids = await seed(kind)
    selection = [ids[4], ids[1], ids[3], ids[4], 999999]
    async with client() as c:
        result = await toggle(c, kind, selection)
        assert result['count'] == 3
        rows = await listed(c, kind)
        assert [r['order'] for r in rows] == [1, 2, 3]
        suffix = '.mkv' if kind == 'local' else ''
        assert [r['custom_name'] for r in rows] == [f'Channel {i:03}{suffix}' for i in (4, 1, 3)]


@pytest.mark.parametrize('kind', MODELS)
async def test_overlapping_enables_of_same_sources_are_idempotent(kind):
    ids = await seed(kind, enabled=True)
    async with client() as c:
        await asyncio.gather(toggle(c, kind, ids), toggle(c, kind, ids))
        rows = await listed(c, kind)
        assert len(rows) == len(ids)
        assert [r['order'] for r in rows] == list(range(1, 7))


@pytest.mark.parametrize('kind', MODELS)
async def test_reorder_uses_existing_page_or_filter_slots_not_page_local_numbers(kind):
    ids = await seed(kind)
    async with client() as c:
        await toggle(c, kind, ids[:5])
        before = await listed(c, kind)
        # A later page / filtered result containing positions 3 and 5.
        response = await c.post(f'/api/playlist/{kind}/order', json={'ids':[before[4]['id'], before[2]['id']]})
        assert response.status_code == 200, response.text
        after = await listed(c, kind)
        # Minimal response lets the browser update Ord / Live numbers in place
        # without another enriched playlist-list request.
        positions = {r['id']: r for r in response.json()['items']}
        assert set(positions) == {before[4]['id'], before[2]['id']}
        for row in after:
            if row['id'] in positions:
                expected = {'id': row['id'], 'order': row['order']}
                if kind == 'live':
                    expected['number'] = row['number']
                assert positions[row['id']] == expected
        assert [r['id'] for r in after] == [before[i]['id'] for i in (0, 1, 4, 3, 2)]
        assert [r['order'] for r in after] == [1, 2, 3, 4, 5]
        await toggle(c, kind, ids[5:])
        assert [r['order'] for r in await listed(c, kind)] == list(range(1, 7))


@pytest.mark.parametrize('kind', MODELS)
async def test_startup_repairs_legacy_orders_once_without_losing_edits(kind):
    from app.main import _repair_playlist_orders
    ids = await seed(kind)
    async with client() as c:
        await toggle(c, kind, ids)
        async with SessionLocal() as db:
            rows = (await db.scalars(select(MODELS[kind]).order_by(MODELS[kind].id))).all()
            for i, row in enumerate(rows):
                row.order = 0 if i < 3 else 10
                row.custom_name = f'Custom {i}'
                row.group_name = 'My edited group'
            rows[1].enabled = False
            if kind == 'live':
                rows[0].number = 77
                rows[0].lock_number = True
            await db.commit()
        before = await listed(c, kind)
        await _repair_playlist_orders()
        after = await listed(c, kind)
        assert [r['order'] for r in after] == list(range(1, 7))
        assert [(r['id'], r['custom_name'], r['group_name'], r['enabled']) for r in after] == [
            (r['id'], r['custom_name'], r['group_name'], r['enabled']) for r in before]
        if kind == 'live':
            assert after[0]['number'] == 77
            assert after[0]['lock_number'] is True
        await _repair_playlist_orders()
        assert await listed(c, kind) == after


async def test_local_directory_bulk_enable_allocates_distinct_file_positions():
    await seed('local')
    async with SessionLocal() as db:
        directory_id = await db.scalar(select(LocalSource.id))
    async with client() as c:
        response = await c.post('/api/sources/local/dirs/toggle', json={'ids':[directory_id], 'enabled':True})
        assert response.status_code == 200, response.text
        assert [r['order'] for r in await listed(c, 'local')] == list(range(1, 7))


async def test_live_move_by_number_reserves_disabled_playlist_positions():
    ids = await seed('live', count=3)
    async with client() as c:
        await toggle(c, 'live', ids)
        before = await listed(c, 'live')
        await c.put(f"/api/playlist/live/{before[1]['id']}", json={'enabled':False})
        await c.put(f"/api/playlist/live/{before[2]['id']}", json={'number':1})
        after = await listed(c, 'live')
        assert [r['order'] for r in after] == [1, 2, 3]
        assert [r['id'] for r in after] == [before[i]['id'] for i in (2, 1, 0)]


async def test_postgres_lock_keys_are_stable_and_reacquired_after_transaction_end():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sqlalchemy.dialects import postgresql
    from app.services.playlist_order import lock_playlist_order
    transaction = object()
    db = SimpleNamespace(
        info={}, no_autoflush=nullcontext(), execute=AsyncMock(),
        get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()),
        sync_session=SimpleNamespace(get_transaction=lambda: transaction))
    for model in MODELS.values():
        await lock_playlist_order(db, model)
        await lock_playlist_order(db, model)
    assert db.execute.await_count == 4  # one lock per kind in the transaction
    params = []
    for call in db.execute.await_args_list:
        statement = call.args[0].compile(dialect=postgresql.dialect())
        assert 'pg_advisory_xact_lock' in str(statement)
        params.append(tuple(statement.params.values()))
    assert params == [(0x53504D, i) for i in range(1, 5)]
    transaction = object()  # commit/rollback followed by a new transaction
    await lock_playlist_order(db, LivePlaylist)
    assert db.execute.await_count == 5
