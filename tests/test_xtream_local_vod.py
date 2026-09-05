"""Local playlist files are exposed as Xtream Movies, never Series."""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import LocalFile, LocalPlaylist, LocalSource, User
from app.services.playlist_gen import (
    local_id_from_xtream, xtream_categories, xtream_local_id, xtream_series,
    xtream_vod, xtream_vod_info,
)


async def _seed(tmp_path):
    folder = tmp_path / "media"
    folder.mkdir()
    path = folder / "Holiday Film.mp4"
    path.write_bytes(b"local-video")
    stat = path.stat()
    async with SessionLocal() as session:
        user = User(name="android", password="pw", enabled=True,
                    m3u_enabled=False, xtream_enabled=True, max_connections=2)
        source = LocalSource(directory=str(folder), enabled=True)
        session.add_all([user, source])
        await session.flush()
        local_file = LocalFile(
            local_source_id=source.id, relative_path=path.name, filename=path.name,
            size_bytes=stat.st_size,
            mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            duration_s=125.0,
        )
        session.add(local_file)
        await session.flush()
        item = LocalPlaylist(local_file_id=local_file.id, custom_name="Holiday",
                             group_name="My local movies", enabled=True)
        session.add(item)
        await session.commit()
        return user, item.id, path


async def test_local_file_is_an_xtream_movie_with_collision_safe_id(tmp_path):
    user, local_id, _path = await _seed(tmp_path)
    categories = await xtream_categories(user, "vod")
    movies = await xtream_vod(user)
    series = await xtream_series(user)

    assert [c["category_name"] for c in categories] == ["My local movies"]
    assert len(movies) == 1
    movie = movies[0]
    assert movie["name"] == "Holiday Film.mp4"
    assert movie["stream_type"] == "movie"
    assert movie["stream_id"] == xtream_local_id(local_id)
    assert local_id_from_xtream(movie["stream_id"]) == local_id
    assert movie["container_extension"] == "mp4"
    assert series == []

    info = await xtream_vod_info(user, movie["stream_id"])
    assert info["movie_data"]["stream_id"] == movie["stream_id"]
    assert info["info"]["duration_secs"] == 125


async def test_xtream_movie_url_serves_mapped_local_file_head(tmp_path):
    _user, local_id, path = await _seed(tmp_path)
    stream_id = xtream_local_id(local_id)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        response = await client.head(f"/movie/android/pw/{stream_id}.mp4")
    assert response.status_code == 200
    assert response.headers["content-length"] == str(path.stat().st_size)
    assert response.headers["content-type"].startswith("video/mp4")
