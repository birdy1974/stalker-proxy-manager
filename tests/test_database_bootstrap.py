from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from app import database as db


def _fake_asyncpg(monkeypatch, *, connect):
    mod = ModuleType("asyncpg")

    class InvalidCatalogNameError(Exception):
        pass

    class DuplicateDatabaseError(Exception):
        pass

    mod.InvalidCatalogNameError = InvalidCatalogNameError
    mod.DuplicateDatabaseError = DuplicateDatabaseError
    mod.connect = connect
    monkeypatch.setitem(sys.modules, "asyncpg", mod)
    return mod


def test_missing_postgres_database_detection_walks_wrapped_exceptions(monkeypatch):
    async def _unused_connect(_dsn):  # pragma: no cover - import stub only
        raise AssertionError("connect should not be called")

    fake = _fake_asyncpg(monkeypatch, connect=_unused_connect)
    inner = fake.InvalidCatalogNameError('database "spm" does not exist')
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner

    assert db._is_missing_postgres_database(outer) is True


async def test_create_postgres_database_falls_back_to_template1(monkeypatch):
    calls: list[str] = []
    sql: list[str] = []
    closed: list[str] = []

    class _Conn:
        async def fetchval(self, query, value):
            assert query == "SELECT 1 FROM pg_database WHERE datname = $1"
            assert value == "stalker_proxy_manager"
            return None

        async def execute(self, statement):
            sql.append(statement)

        async def close(self):
            closed.append("yes")

    async def _connect(dsn):
        calls.append(dsn)
        if dsn.endswith("/postgres"):
            raise fake.InvalidCatalogNameError("postgres maintenance db missing")
        return _Conn()

    fake = _fake_asyncpg(monkeypatch, connect=_connect)

    await db._create_postgres_database(
        "postgresql+asyncpg://spm:pw@db:5432/stalker_proxy_manager")

    assert calls == [
        "postgresql://spm:pw@db:5432/postgres",
        "postgresql://spm:pw@db:5432/template1",
    ]
    assert sql == ['CREATE DATABASE "stalker_proxy_manager"']
    assert closed == ["yes"]


async def test_init_db_retries_once_after_creating_a_missing_postgres_database(monkeypatch):
    async def _unused_connect(_dsn):  # pragma: no cover - import stub only
        raise AssertionError("connect should not be called")

    fake = _fake_asyncpg(monkeypatch, connect=_unused_connect)
    attempts: list[str] = []
    created: list[str] = []

    async def _ensure_schema(_metadata):
        attempts.append("ensure")
        if len(attempts) == 1:
            boom = RuntimeError("connect failed")
            boom.__cause__ = fake.InvalidCatalogNameError("missing target db")
            raise boom

    async def _create(_database_url=db.DATABASE_URL):
        created.append("created")

    monkeypatch.setattr(db, "_ensure_schema", _ensure_schema)
    monkeypatch.setattr(db, "_create_postgres_database", _create)
    monkeypatch.setattr(db, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))

    await db.init_db()

    assert attempts == ["ensure", "ensure"]
    assert created == ["created"]
