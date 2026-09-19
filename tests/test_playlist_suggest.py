"""Optional broader source matching in the channel editor."""
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models import LivePlaylist, LiveSource, Portal
from app.routers.api_playlist import fuzzy

QUERY = 'NPO 1'
LOOSE = 'NL NPO1 Netherlands HEVC 1080p'


def test_looser_cutoff_adds_weaker_matches_without_changing_normal_ranking():
    candidates = [(1, QUERY), (2, 'NPO 1 HD'), (3, LOOSE), (4, 'zzzzzz')]
    normal = fuzzy(QUERY, candidates)
    relaxed = fuzzy(QUERY, candidates, min_score=0.25)
    assert [cid for cid, _ in normal] == [1, 2]
    assert [cid for cid, _ in relaxed] == [1, 2, 3]
    assert relaxed[:2] == normal
    assert fuzzy('npo 1', candidates) == normal
    assert len(fuzzy(QUERY, candidates, 1, min_score=0.25)) == 1


async def seed(names):
    async with SessionLocal() as db:
        portal = Portal(name='Test portal', base_url='http://portal.invalid/c/')
        db.add(portal)
        await db.flush()
        for i, (name, enabled) in enumerate(names):
            db.add(LiveSource(portal_id=portal.id, portal_channel_id=str(i),
                              original_name=name, enabled=enabled, cmd='ffmpeg http://example.invalid/live'))
        await db.commit()


async def test_empty_normal_results_can_be_broadened_without_including_disabled_sources():
    await seed([(LOOSE, True), (QUERY, False), ('zzzzzz', True)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        default = await c.get('/api/playlist/suggest', params={'kind':'live', 'q':QUERY})
        assert default.status_code == 200
        assert default.json()['items'] == []
        relaxed = await c.get('/api/playlist/suggest', params={'kind':'live', 'q':QUERY, 'relaxed':'true'})
        assert relaxed.status_code == 200
        assert [row['name'] for row in relaxed.json()['items']] == [LOOSE]
        strict = await c.get('/api/playlist/suggest', params={'kind':'live', 'q':QUERY, 'relaxed':'false'})
        assert strict.json() == default.json()
    async with SessionLocal() as db:
        assert list(await db.scalars(select(LivePlaylist))) == [], 'search must not change the playlist'


async def test_empty_query_and_sixty_result_limit_still_apply_in_both_modes():
    await seed([(f'NPO 1 variant {i}', True) for i in range(65)] + [('NPO 1 disabled', False)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        for q in ('', QUERY):
            results = []
            for relaxed in ('false', 'true'):
                response = await c.get('/api/playlist/suggest', params={'q':q, 'relaxed':relaxed})
                assert response.status_code == 200
                items = response.json()['items']
                assert len(items) == 60
                assert all(row['name'] != 'NPO 1 disabled' for row in items)
                results.append(items)
            assert results[0] == results[1]


async def test_invalid_matching_mode_is_rejected():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        response = await c.get('/api/playlist/suggest?relaxed=invalid')
        assert response.status_code == 422


async def test_show_all_bypasses_name_matching_and_pages_through_every_enabled_source():
    await seed([(f'Unrelated source {i:03}', True) for i in range(125)] + [('Disabled', False)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        items = []
        offset = 0
        for expected_size in (60, 60, 5):
            response = await c.get('/api/playlist/suggest', params={
                'q':'ZZZZZZZ', 'show_all':'true', 'offset':offset})
            assert response.status_code == 200
            data = response.json()
            assert data['total'] == 125
            assert len(data['items']) == expected_size
            items.extend(data['items'])
            offset = data['next_offset']
        assert offset is None
        assert len({item['id'] for item in items}) == 125
        assert [item['name'] for item in items] == [f'Unrelated source {i:03}' for i in range(125)]
        beyond = await c.get('/api/playlist/suggest', params={'show_all':'true', 'offset':999})
        assert beyond.json()['items'] == []
        assert beyond.json()['next_offset'] is None
        invalid = await c.get('/api/playlist/suggest', params={'show_all':'true', 'offset':-1})
        assert invalid.status_code == 422


async def test_show_all_free_text_filter_is_independent_case_insensitive_and_literal():
    await seed([(f'Unrelated {i:03}', True) for i in range(65)] + [
        ('Zebra News HD', True), ('Zebra News SD', True), ('Zebra News HD disabled', False),
        ('100%_channel', True), ('100XXchannel', True)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        for text, expected in [
            ('  NEWS  hd TEST  ', ['Zebra News HD']),
            ('%_', ['100%_channel']),
            ('unknownword', []),
        ]:
            response = await c.get('/api/playlist/suggest', params={
                'q':'NPO 1', 'show_all':'true', 'relaxed':'true', 'source_filter':text})
            assert response.status_code == 200
            assert [item['name'] for item in response.json()['items']] == expected
            assert response.json()['total'] == len(expected)
            assert response.json()['next_offset'] is None
        # The all-sources filter must not accidentally change normal matching.
        response = await c.get('/api/playlist/suggest', params={
            'q':'Zebra News HD', 'source_filter':'nomatch', 'show_all':'false'})
        assert response.json()['items'][0]['name'] == 'Zebra News HD'


async def test_show_all_includes_sources_already_assigned_to_playlist():
    from app.models import LivePlaylistSource
    await seed([('Existing linked source', True)])
    async with SessionLocal() as db:
        source_id = await db.scalar(select(LiveSource.id))
        channel = LivePlaylist(custom_name='Custom name')
        db.add(channel)
        await db.flush()
        db.add(LivePlaylistSource(live_playlist_id=channel.id, live_source_id=source_id, priority=1))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        response = await c.get('/api/playlist/suggest', params={'show_all':'true', 'q':'zzzzz'})
        assert [item['id'] for item in response.json()['items']] == [source_id]
    async with SessionLocal() as db:
        assert (await db.scalar(select(LivePlaylist))).custom_name == 'Custom name'
        assert len(list(await db.scalars(select(LivePlaylistSource)))) == 1
