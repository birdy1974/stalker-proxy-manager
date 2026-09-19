"""Resolve draft connection settings without an implicit Save."""
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models import Portal, MacAddress
from app.routers import api_portals
from app.portal.resolver import ResolveResult

DRAFT = {'name':'Draft portal', 'base_url':'https://draft.invalid/c/',
         'macs':'00-1a-79-ab-cd-ef', 'proxy_url':'http://proxy.invalid:8080',
         'tls_insecure':True, 'identity_mode':'mag250', 'stb_timezone':'Europe/Amsterdam'}


@pytest.fixture
def probes(monkeypatch):
    resolver = AsyncMock(return_value=ResolveResult(ok=True,
        portal_url='https://draft.invalid/c/portal.php', path='/c/', attempts=['ok']))
    client = AsyncMock()
    client.refresh_capabilities.return_value = {'modules':['tv','vclub'], 'version':{'label':'Test panel'}}
    sessions = []
    def make_client(session):
        sessions.append(session)
        return client
    monkeypatch.setattr(api_portals, 'resolve_portal', resolver)
    monkeypatch.setattr(api_portals.PortalSession, 'client', make_client)
    monkeypatch.setattr(api_portals.POOL, 'get', AsyncMock(side_effect=AssertionError('draft must not use playback pool')))
    return resolver, client, sessions


async def request(payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        return await c.post('/api/portals/resolve', json=payload)


async def test_new_draft_resolves_current_settings_and_does_not_save(probes):
    resolver, client, sessions = probes
    response = await request(DRAFT)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['ok'] and result['saved'] is False
    assert result['modules'] == ['tv','vclub']
    resolver.assert_awaited_once_with(DRAFT['base_url'], mac='00:1A:79:AB:CD:EF',
                                    proxy=DRAFT['proxy_url'], tls_insecure=True)
    session = sessions[0]
    assert session.identity_mode == 'mag250' and session.timezone == 'Europe/Amsterdam'
    assert session.proxy == DRAFT['proxy_url'] and session.tls_insecure
    client.close.assert_awaited_once()
    async with SessionLocal() as db:
        assert list(await db.scalars(select(Portal))) == []
        assert list(await db.scalars(select(MacAddress))) == []


async def test_existing_draft_uses_unsaved_url_and_keeps_saved_credentials_without_writes(probes):
    async with SessionLocal() as db:
        portal = Portal(name='Saved', base_url='http://saved.invalid/c/',
                        resolved_url='http://saved.invalid/c/portal.php', portal_version='Old version',
                        identity_mode='minimal', tls_insecure=False)
        db.add(portal)
        await db.flush()
        mac = MacAddress(portal_id=portal.id, mac='00:1A:79:AB:CD:EF', order=0,
                         password='local-test-only', sn='pinned-sn', device_id='pinned-device')
        db.add(mac)
        await db.commit()
        pid = portal.id
    response = await request({**DRAFT, 'portal_id':pid})
    assert response.status_code == 200, response.text
    session = probes[2][0]
    assert session.portal_url.startswith('https://draft.invalid/')
    assert session.password == 'local-test-only' and session.sn == 'pinned-sn'
    assert session.device_id == 'pinned-device'
    async with SessionLocal() as db:
        saved = await db.get(Portal, pid)
        assert saved.name == 'Saved' and saved.base_url == 'http://saved.invalid/c/'
        assert saved.resolved_url == 'http://saved.invalid/c/portal.php'
        assert saved.portal_version == 'Old version' and saved.identity_mode == 'minimal'
        assert saved.tls_insecure is False
        assert len(list(await db.scalars(select(MacAddress)))) == 1


@pytest.mark.parametrize('change', [
    {'name':''}, {'name':None}, {'base_url':''}, {'base_url':42},
    {'base_url':'ftp://invalid/'}, {'base_url':'http://host:bad/c/'},
    {'macs':''}, {'macs':'not a mac'}, {'identity_mode':'invalid'},
    {'portal_id':'invalid'},
])
async def test_incomplete_or_invalid_drafts_are_rejected_before_network_requests(probes, change):
    response = await request({**DRAFT, **change})
    assert response.status_code == 400, response.text
    probes[0].assert_not_awaited()


async def test_missing_portal_is_not_created_by_resolve(probes):
    response = await request({**DRAFT, 'portal_id':999999})
    assert response.status_code == 404
    probes[0].assert_not_awaited()


async def test_resolve_failure_returns_attempts_without_saving(probes):
    probes[0].return_value = ResolveResult(ok=False, error='unreachable', attempts=['failed path'])
    response = await request(DRAFT)
    assert response.status_code == 200
    assert response.json()['attempts'] == ['failed path']
    assert response.json()['ok'] is False and response.json()['saved'] is False
    assert not probes[2]


async def test_capability_failure_is_nonfatal_and_closes_temporary_client(probes):
    probes[1].ensure_auth.side_effect = RuntimeError('test failure')
    response = await request(DRAFT)
    assert response.status_code == 200 and response.json()['ok']
    assert response.json()['modules_error'] == 'RuntimeError'
    probes[1].close.assert_awaited_once()
