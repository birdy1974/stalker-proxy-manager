"""Group/template bulk assignment is set-based and never renumbers playlists."""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from app.database import SessionLocal, engine
from app.main import app
from app.models import (LivePlaylist, VodPlaylist, SeriePlaylist, LocalPlaylist,
    Portal, VodSource, SerieSource, LocalSource, LocalFile, FFmpegTemplate)


async def seed(kind, count):
    model = {'live':LivePlaylist,'vod':VodPlaylist,'series':SeriePlaylist,'local':LocalPlaylist}[kind]
    async with SessionLocal() as db:
        extra = {}
        if kind in ('vod','series'):
            portal = Portal(name='Fixture',base_url='http://invalid');db.add(portal);await db.flush()
            src = (VodSource if kind=='vod' else SerieSource)(portal_id=portal.id,portal_item_id='1',original_name='Input')
            db.add(src);await db.flush();extra['vod_source_id' if kind=='vod' else 'serie_source_id']=src.id
        if kind=='local':
            directory=LocalSource(directory='/fixture');db.add(directory);await db.flush()
            file=LocalFile(local_source_id=directory.id,relative_path='f.ts',filename='f.ts');db.add(file);await db.flush();extra['local_file_id']=file.id
        rows=[model(custom_name=f'Item {n}',order=n+10,group_name='Old',enabled=n%2==0,**extra) for n in range(count)]
        if kind=='live':
            for n,r in enumerate(rows):r.number=n+100;r.lock_number=n%3==0
        tpl=FFmpegTemplate(name='Fixture template');db.add_all([tpl,*rows]);await db.commit()
        return model,[r.id for r in rows],tpl.id


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['live','vod','series','local'])
async def test_bulk_metadata_bounded_queries_preserves_order_and_deduplicates(kind):
    model,ids,tid=await seed(kind,1003)
    selected=ids[:-1];statements=[]
    def capture(conn,cursor,sql,*args):statements.append(sql.lower())
    event.listen(engine.sync_engine,'before_cursor_execute',capture)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post(f'/api/playlist/{kind}/bulk',json={'ids':selected+[selected[0],999999], 'group_name':'New', 'ffmpeg_template_id':tid})
    finally:event.remove(engine.sync_engine,'before_cursor_execute',capture)
    assert response.status_code==200,response.text
    assert response.json()['count']==len(selected)
    assert not any(sql.startswith('select') and model.__tablename__ in sql for sql in statements)
    assert len([sql for sql in statements if sql.startswith('update')])==4  # reservation + three bounded batches
    async with SessionLocal() as db:
        rows=(await db.scalars(select(model).order_by(model.id))).all()
        for n,row in enumerate(rows):
            assert row.order==n+10 and row.enabled==(n%2==0)
            assert row.group_name==('New' if row.id in selected else 'Old')
            assert row.ffmpeg_template_id==(tid if row.id in selected else None)
            if kind=='live':assert row.number==n+100 and row.lock_number==(n%3==0)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post(f'/api/playlist/{kind}/bulk',json={'ids':[ids[0]], 'ffmpeg_template_id':0})).json()['count']==1
    async with SessionLocal() as db:assert (await db.get(model,ids[0])).ffmpeg_template_id is None


@pytest.mark.asyncio
async def test_invalid_template_does_not_partially_assign_group():
    model,ids,_=await seed('live',2)
    async with AsyncClient(transport=ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
        response=await client.post('/api/playlist/live/bulk',json={'ids':ids,'group_name':'Invalid change','ffmpeg_template_id':999999})
        assert response.status_code>=400
    async with SessionLocal() as db:
        assert all(row.group_name=='Old' and row.ffmpeg_template_id is None for row in (await db.scalars(select(model))).all())
