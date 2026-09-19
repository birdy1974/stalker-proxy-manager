"""V2 coverage is tied to metadata so a new table/column cannot silently be lost."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Boolean, DateTime, Float, Integer, select

from app.database import SessionLocal
from app.main import app
from app.models import Base
from app.services import backup


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def seed(prefix="a", ident=11):
    """Populate every column of every table, including the implicit area FK."""
    async with SessionLocal() as db:
        tables = [t for t in Base.metadata.sorted_tables if t.name != "area_item_templates"] + [backup.TABLES["area_item_templates"]]
        for table in tables:
            values = {}
            for c in table.c:
                if isinstance(c.type, Boolean):
                    value = False
                elif isinstance(c.type, DateTime):
                    value = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
                elif isinstance(c.type, Integer):
                    value = ident if c.primary_key or c.foreign_keys else 3
                elif isinstance(c.type, Float):
                    value = 3.25
                else:
                    value = prefix + "-" + c.name
                    if getattr(c.type, "length", None):
                        value = value[:c.type.length]
                values[c.name] = value
            if table.name == "area_item_templates":
                values.update(kind="live", playlist_id=ident)
            if table.name == "settings":
                values["value"] = '"setting-value"'
            await db.execute(table.insert().values(**values))
        await db.commit()


async def wipe():
    async with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(table.delete())
        await db.commit()


async def export(c, **payload):
    r = await c.post("/api/backup/export", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


async def rows():
    async with SessionLocal() as db:
        return {n: [dict(r) for r in (await db.execute(select(t))).mappings()] for n, t in backup.TABLES.items()}


async def test_every_table_and_column_is_exported():
    assert set(backup.IDENTITIES) == set(backup.TABLES)
    await seed()
    async with client() as c:
        data = await export(c)
        assert set(data["tables"]) == set(backup.TABLES)
        for name, records in data["tables"].items():
            assert len(records) == 1
            assert set(records[0]) == set(backup.TABLES[name].c.keys())
        catalog = (await c.get("/api/backup/catalog")).json()
        assert {t["name"] for t in catalog["tables"]} == set(backup.TABLES)
        assert catalog["setting_keys"] == ["a-key"]


async def test_full_restore_rebinds_every_relationship_and_preserves_every_value():
    await seed()
    original = await rows()
    async with client() as c:
        data = await export(c)
        await wipe()
        await seed("b", 200)  # foreign IDs collide with neither source nor destination
        r = await c.post("/api/backup/restore", json={"data": data, "confirm_add_only": True})
        assert r.status_code == 200, r.text
        assert r.json()["added"] == len(backup.TABLES) - 1
        assert r.json()["runtime_skipped"] == 1
        restored = await rows()
        for name, records in original.items():
            if name == "active_streams":
                assert len(restored[name]) == 1
                continue
            assert len(restored[name]) == 2, name
            new = restored[name][-1]
            for key, value in records[0].items():
                if key == backup.pk(backup.TABLES[name]).name and name != "settings":
                    assert new[key] == 201
                elif backup.TABLES[name].c[key].foreign_keys or (name == "area_item_templates" and key == "playlist_id"):
                    assert new[key] == 201, (name, key)
                else:
                    assert new[key] == value, (name, key)
        # Idempotent even with different IDs and all timestamp-bearing tables.
        again = await c.post("/api/backup/restore", json={"data": data, "confirm_add_only": True})
        assert again.status_code == 200, again.text
        assert again.json()["added"] == 0
        assert again.json()["existing"] == len(backup.TABLES) - 1


@pytest.mark.parametrize("table", sorted(backup.TABLES))
async def test_individual_table_export_restore_with_dependencies(table):
    await seed()
    async with client() as c:
        data = await export(c, tables=[table])
        assert data["selected_tables"] == [table]
        assert len(data["tables"][table]) == 1
        await wipe()
        preview = await c.post("/api/backup/preview", json={"data": data})
        assert preview.status_code == 200, preview.text
        assert all(not records for records in (await rows()).values()), "preview must not persist inserts"
        response = await c.post("/api/backup/restore", json={"data": data, "confirm_add_only": True})
        assert response.status_code == 200, response.text
        assert response.json() == preview.json()
        restored = await rows()
        for name in backup.TABLES:
            assert len(restored[name]) == (1 if name in data["tables"] and name != "active_streams" else 0), name


async def test_additive_restore_preserves_existing_rows_and_settings():
    await seed()
    async with client() as c:
        data = await export(c)
        # Change all non-identity columns in the file. No existing row may change.
        data["tables"]["settings"][0]["value"] = '"overwrite-attempt"'
        data["tables"]["portals"][0]["notes"] = "overwrite-attempt"
        data["tables"]["live_playlist"][0]["enabled"] = True
        data["tables"]["live_playlist_sources"][0]["priority"] = 99
        before = await rows()
        r = await c.post("/api/backup/restore", json={"data": data, "confirm_add_only": True})
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 0
        assert await rows() == before


async def test_one_setting_can_be_exported_restored_and_deleted():
    await seed()
    async with client() as c:
        data = await export(c, tables=["settings"], setting_keys=["a-key"])
        assert set(data["tables"]) == {"settings"}
        async with SessionLocal() as db:
            await db.execute(backup.TABLES["settings"].insert().values(key="keep", value="true"))
            await db.commit()
        payload = {"tables": ["settings"], "setting_keys": ["a-key"],
                   "confirmation": "DELETE SELECTED", "confirm_delete": True}
        r = await c.post("/api/backup/delete", json=payload)
        assert r.status_code == 200, r.text
        assert (await rows())["settings"] == [{"key": "keep", "value": "true"}]
        r = await c.post("/api/backup/restore", json={"data": data, "tables": ["settings"],
                                                    "setting_keys": ["a-key"], "confirm_add_only": True})
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 1
        assert len((await rows())["settings"]) == 2


@pytest.mark.parametrize("table", sorted(backup.TABLES))
async def test_individual_table_deletion_matches_preview_and_leaves_no_orphans(table):
    await seed()
    async with client() as c:
        preview = await c.post("/api/backup/delete-preview", json={"tables": [table]})
        assert preview.status_code == 200, preview.text
        before = await rows()
        assert all(len(r) == 1 for r in before.values())
        deleted = await c.post("/api/backup/delete", json={"tables": [table],
            "confirmation": "DELETE SELECTED", "confirm_delete": True})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == preview.json()
        after = await rows()
        counts = {t["name"]: t for t in preview.json()["tables"]}
        for name, records in after.items():
            assert len(records) == 1 - counts.get(name, {}).get("deleted", 0), name
            for row in records:
                for _col, parent, ref in backup.references(name, row):
                    assert any(r[backup.pk(backup.TABLES[parent]).name] == ref for r in after[parent]), (name, parent)


async def test_delete_everything_requires_exact_confirmation_and_empties_all_tables():
    await seed()
    async with client() as c:
        for payload in ({"all": True}, {"all": True, "confirmation": "DELETE SELECTED", "confirm_delete": True},
                        {"all": True, "confirmation": "DELETE EVERYTHING"}):
            assert (await c.post("/api/backup/delete", json=payload)).status_code == 400
            assert all(len(r) == 1 for r in (await rows()).values())
        r = await c.post("/api/backup/delete", json={"all": True,
            "confirmation": "DELETE EVERYTHING", "confirm_delete": True})
        assert r.status_code == 200, r.text
        assert all(not r for r in (await rows()).values())


async def test_confirmation_validation_and_atomic_rollback():
    await seed()
    async with client() as c:
        data = await export(c)
        assert (await c.post("/api/backup/restore", json={"data": data})).status_code == 400
        await wipe()
        bad = deepcopy(data)
        # This conflicts with a unique token after other valid records were inserted.
        second = dict(bad["tables"]["enigma2_profiles"][0], id=12, name="different")
        bad["tables"]["enigma2_profiles"].append(second)
        for endpoint in ("restore", "preview"):
            r = await c.post(f"/api/backup/{endpoint}", json={"data": bad, "confirm_add_only": True})
            assert r.status_code == 409, r.text
            assert all(not records for records in (await rows()).values())
        for bad in ({}, {**data, "version": 999}, {**data, "tables": {"fake": []}},
                    {**data, "tables": {"settings": [{}]}},
                    {**data, "tables": {"settings": [{"key": "x", "value": 5}]}},
                    {**data, "tables": {"mac_addresses": data["tables"]["mac_addresses"]}}):
            r = await c.post("/api/backup/restore", json={"data": bad, "confirm_add_only": True})
            assert r.status_code == 400, r.text
            assert all(not records for records in (await rows()).values())
        assert (await c.post("/api/backup/export", json={"tables": ["not_a_table"]})).status_code == 400
        assert (await c.post("/api/backup/delete-preview", json={"tables": []})).status_code == 400


async def test_restore_one_table_from_full_backup_only_adds_its_dependencies():
    await seed()
    async with client() as c:
        data = await export(c)
        await wipe()
        r = await c.post("/api/backup/restore", json={"data": data,
            "tables": ["epg_channels"], "confirm_add_only": True})
        assert r.status_code == 200, r.text
        assert {n for n, records in (await rows()).items() if records} == {"epg_channels", "epg_sources", "portals"}  # portal-backed guide dependency


async def test_deletion_rejected_during_fetch_or_stream(monkeypatch):
    from app.services import fetch_jobs
    from app.services.stream_manager import MANAGER
    async with client() as c:
        payload = {"all": True, "confirmation": "DELETE EVERYTHING", "confirm_delete": True}
        monkeypatch.setattr(fetch_jobs, "list_jobs", lambda: [{"status": "running"}])
        assert (await c.post("/api/backup/delete", json=payload)).status_code == 409
        monkeypatch.setattr(fetch_jobs, "list_jobs", lambda: [])
        monkeypatch.setattr(MANAGER, "list", lambda: [{"id": "live"}])
        assert (await c.post("/api/backup/delete", json=payload)).status_code == 409


async def test_all_backup_endpoints_require_admin(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "SKIP_LOGIN", False)
    async with client() as c:
        assert (await c.get("/api/backup/catalog")).status_code == 401
        for endpoint in ("export", "preview", "restore", "delete-preview", "delete"):
            assert (await c.post(f"/api/backup/{endpoint}", json={})).status_code == 401


async def test_settings_page_renders_new_controls_and_versioned_script():
    async with client() as c:
        response = await c.get('/settings')
        assert response.status_code == 200, response.text
        assert 'Backup &amp; restore' in response.text
        assert 'only additional information is added' in response.text
        assert 'DELETE SELECTED' in response.text
        assert 'settings-backup.js?v=' in response.text
        assert response.text.count('id="import-file"') == 1


async def test_dependencies_bind_to_existing_identity_not_old_numeric_id():
    await seed()
    async with client() as c:
        data = await export(c, tables=['live_sources'])
        await wipe()
        async with SessionLocal() as db:
            portal = backup.TABLES['portals']
            await db.execute(portal.insert().values(id=11, name='unrelated', base_url='http://other'))
            await db.execute(portal.insert().values(id=400, name='a-name', base_url='http://keep', notes='local'))
            await db.commit()
        result = await c.post('/api/backup/restore', json={'data': data, 'confirm_add_only': True})
        assert result.status_code == 200, result.text
        assert result.json()['existing'] == 1
        restored = await rows()
        assert restored['live_sources'][0]['portal_id'] == 400
        assert restored['live_genres'][0]['portal_id'] == 400
        existing = next(p for p in restored['portals'] if p['id'] == 400)
        assert existing['base_url'] == 'http://keep'
        assert existing['notes'] == 'local'


@pytest.mark.parametrize('kind,parent', backup.PLAYLISTS.items())
async def test_polymorphic_area_override_remapping_and_deletion(kind, parent):
    await seed()
    async with SessionLocal() as db:
        await db.execute(backup.TABLES['area_item_templates'].update().values(kind=kind))
        await db.commit()
    async with client() as c:
        data = await export(c, tables=['area_item_templates'])
        assert parent in data['tables']
        await wipe()
        await seed('b', 200)
        result = await c.post('/api/backup/restore', json={'data': data, 'confirm_add_only': True})
        assert result.status_code == 200, result.text
        restored = await rows()
        assert restored['area_item_templates'][-1]['playlist_id'] == 201
        assert restored['area_item_templates'][-1]['kind'] == kind
        result = await c.post('/api/backup/delete', json={'tables': [parent],
            'confirmation': 'DELETE SELECTED', 'confirm_delete': True})
        assert result.status_code == 200, result.text
        assert not any(r['kind'] == kind for r in (await rows())['area_item_templates'])
