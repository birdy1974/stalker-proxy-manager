"""Explicit group selections: defaults, empty lists, and all catalogue outputs."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import (Enigma2Profile, LivePlaylist, LocalFile, LocalPlaylist, LocalSource,
                        Portal, SerieEpisode, SeriePlaylist, SeriePlaylistSeason, SerieSeason,
                        SerieSource, User, VodPlaylist, VodSource)
from app.services import enigma2_bouquets as e2
from app.services.playlist_gen import _groups, xtream_local_id

GROUPS = {'live':['Live','News'], 'vod':['Action','VOD'],
          'series':['Drama','Series'], 'local':['Files','Local files']}


async def seed():
    async with SessionLocal() as db:
        portal = Portal(name='Groups test', base_url='http://groups.invalid')
        directory = LocalSource(directory='/tmp/group-tests')
        db.add_all([portal, directory]); await db.flush()
        ids = {kind:[] for kind in GROUPS}
        for i in range(2):
            live = LivePlaylist(custom_name=f'Live title {i}', group_name='News' if i else None)
            vod_src = VodSource(portal_id=portal.id, portal_item_id=str(i), original_name=f'Film {i}')
            serie_src = SerieSource(portal_id=portal.id, portal_item_id=str(i), original_name=f'Show {i}')
            local_file = LocalFile(local_source_id=directory.id, relative_path=f'{i}.mp4', filename=f'{i}.mp4', duration_s=60)
            db.add_all([live, vod_src, serie_src, local_file]); await db.flush()
            vod = VodPlaylist(vod_source_id=vod_src.id, custom_name=f'Film {i}', group_name='Action' if i else None)
            series = SeriePlaylist(serie_source_id=serie_src.id, custom_name=f'Show {i}', group_name='Drama' if i else None)
            local = LocalPlaylist(local_file_id=local_file.id, custom_name=f'File {i}', group_name='Files' if i else '')
            season = SerieSeason(serie_source_id=serie_src.id, season_number=1)
            db.add_all([vod, series, local, season]); await db.flush()
            db.add(SeriePlaylistSeason(serie_playlist_id=series.id, serie_season_id=season.id, enabled=True))
            db.add(SerieEpisode(serie_season_id=season.id, episode_number=1, name='Episode'))
            for kind, row in [('live',live),('vod',vod),('series',series),('local',local)]:
                ids[kind].append(row.id)
        await db.commit()
        return ids


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def create(c, **extra):
    r = await c.post('/api/users', json={'name':'viewer','password':'pw', 'm3u_enabled':True, 'xtream_enabled':True, **extra})
    assert r.status_code == 200, r.text
    return r.json()['id']


async def xtream(c, action, **extra):
    return await c.get('/player_api.php', params={'username':'viewer','password':'pw','action':action, **extra})


async def test_new_user_defaults_to_all_available_groups_including_ungrouped():
    await seed()
    async with client() as c:
        uid = await create(c)
        data = (await c.get('/api/users')).json()
        assert data['groups_available'] == GROUPS
        assert data['items'][0]['groups'] == GROUPS
        text = (await c.get('/playlist.m3u?u=viewer&p=pw')).text
        assert text.count('#EXTINF:') == 8
        for kind in ('live','vod','series'):
            rows = (await xtream(c, f'get_{kind}_categories')).json()
            wanted = GROUPS[kind] + (GROUPS['local'] if kind == 'vod' else [])
            assert {r['category_name'] for r in rows} == set(wanted)
        # Non-group edits must not silently change the selections.
        await c.put(f'/api/users/{uid}', json={'max_connections':4})
        assert (await c.get('/api/users')).json()['items'][0]['groups'] == GROUPS


@pytest.mark.parametrize('cleared', GROUPS)
async def test_deselecting_a_type_hides_only_that_type(cleared):
    ids = await seed()
    async with client() as c:
        uid = await create(c)
        groups = {k: ([] if k == cleared else list(v)) for k,v in GROUPS.items()}
        assert (await c.put(f'/api/users/{uid}', json={'groups':groups})).status_code == 200
        assert (await c.get('/api/users')).json()['items'][0]['groups'] == groups
        text = (await c.get('/playlist.m3u?u=viewer&p=pw')).text
        assert text.count('#EXTINF:') == 6
        for kind, path in [('live','live'),('vod','vod'),('series','episode'),('local','local')]:
            assert (f'/play/{path}/' in text) == (kind != cleared)
        assert len((await xtream(c,'get_live_streams')).json()) == (0 if cleared == 'live' else 2)
        assert len((await xtream(c,'get_series')).json()) == (0 if cleared == 'series' else 2)
        assert len((await xtream(c,'get_vod_streams')).json()) == (2 if cleared in ('vod','local') else 4)
        if cleared in ('vod','local'):
            vid = ids[cleared][0] if cleared == 'vod' else xtream_local_id(ids['local'][0])
            assert (await xtream(c,'get_vod_info',vod_id=vid)).status_code == 404
        elif cleared == 'series':
            assert (await xtream(c,'get_series_info',series_id=ids['series'][0])).status_code == 404
        xml = (await c.get('/xmltv.php?username=viewer&password=pw')).text
        assert ('<channel ' in xml) == (cleared != 'live')


async def test_clear_all_invalidates_warm_m3u_and_enigma2_output_and_can_be_reselected():
    await seed()
    async with client() as c:
        uid = await create(c)
        async with SessionLocal() as db:
            profile = Enigma2Profile(name='Receiver', token='test-group-token', user_id=uid, include_live=True,
                                     include_vod=True, include_series=True, include_local=True)
            db.add(profile); await db.commit()
        assert (await c.get('/playlist.m3u?u=viewer&p=pw')).text.count('#EXTINF:') == 8
        assert (await e2.build_bundle(profile, 'http://test')).files
        await c.put(f'/api/users/{uid}', json={'groups':{k:[] for k in GROUPS}})
        for url in ('/playlist.m3u?u=viewer&p=pw', '/get.php?username=viewer&password=pw'):
            assert (await c.get(url)).text.strip() == '#EXTM3U'
        assert not (await e2.build_bundle(profile, 'http://test')).files
        for action in ('get_live_categories','get_live_streams','get_vod_categories','get_vod_streams','get_series_categories','get_series'):
            assert (await xtream(c,action)).json() == []
        await c.put(f'/api/users/{uid}', json={'groups':GROUPS})
        assert (await c.get('/playlist.m3u?u=viewer&p=pw')).text.count('#EXTINF:') == 8
        assert (await e2.build_bundle(profile, 'http://test')).files


@pytest.mark.parametrize('selection', [{}, None, {k:[] for k in GROUPS}])
async def test_explicit_empty_create_never_uses_default_all(selection):
    await seed()
    async with client() as c:
        await create(c, groups=selection)
        assert (await c.get('/playlist.m3u?u=viewer&p=pw')).text.strip() == '#EXTM3U'


@pytest.mark.parametrize('stored', [None, '{}', '{"live":[]}', 'null', 'broken-json'])
def test_legacy_empty_missing_or_invalid_selections_fail_closed(stored):
    user = User(name='legacy', password='pw', groups_json=stored)
    assert _groups(user) == {k:[] for k in GROUPS}


async def test_new_groups_are_not_implicitly_granted_to_existing_users():
    await seed()
    async with client() as c:
        await create(c)
        async with SessionLocal() as db:
            db.add(LivePlaylist(custom_name='New restricted channel', group_name='New group'))
            await db.commit()
        assert 'New restricted channel' not in (await c.get('/playlist.m3u?u=viewer&p=pw')).text
        data = (await c.get('/api/users')).json()
        assert 'New group' in data['groups_available']['live']
        assert 'New group' not in data['items'][0]['groups']['live']
