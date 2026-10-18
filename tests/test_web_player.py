"""Browser player login, playlist filtering and playback-pipeline selection."""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    FFmpegTemplate, LivePlaylist, LocalFile, LocalPlaylist, LocalSource, Portal,
    SerieEpisode, SeriePlaylist, SeriePlaylistSeason, SerieSeason, SerieSource,
    User, VodPlaylist, VodSource,
)
from app.routers import output
from app.services.stream_manager import FFmpegTemplateError, MANAGER

BASE = "http://testserver"


async def _seed_player_data() -> tuple[int, int, int, int]:
    async with SessionLocal() as s:
        s.add(User(name="viewer", password="watch-me", enabled=True,
                   m3u_enabled=True, xtream_enabled=False,
                   groups_json=json.dumps({"live": ["News"], "vod": ["Movies"],
                                           "series": ["Drama"], "local": ["Family"]})))
        portal = Portal(name="series-portal", base_url="http://portal.invalid/c/")
        s.add(portal)
        await s.flush()
        vod_source = VodSource(portal_id=portal.id, portal_item_id="movie-1",
                               original_name="Movie One", enabled=True)
        s.add(vod_source)
        await s.flush()
        news = LivePlaylist(custom_name="News One", group_name="News", enabled=True, order=1)
        hidden = LivePlaylist(custom_name="Sports Two", group_name="Sports", enabled=True, order=2)
        disabled = LivePlaylist(custom_name="Disabled News", group_name="News", enabled=False, order=3)
        movie = VodPlaylist(vod_source_id=vod_source.id, custom_name="Movie One",
                            group_name="Movies", enabled=True, order=1)
        hidden_movie = VodPlaylist(vod_source_id=vod_source.id, custom_name="Hidden Movie",
                                   group_name="Action", enabled=True, order=2)
        s.add_all([news, hidden, disabled, movie, hidden_movie])

        serie_source = SerieSource(portal_id=portal.id, portal_item_id="show-1",
                                   original_name="Drama Show", enabled=True)
        s.add(serie_source)
        await s.flush()
        season = SerieSeason(serie_source_id=serie_source.id, season_number=1,
                             enabled=True)
        s.add(season)
        await s.flush()
        episode = SerieEpisode(serie_season_id=season.id, episode_number=1,
                               name="Pilot", cmd="ffmpeg http://cdn/episode.ts")
        s.add(episode)
        playlist_series = SeriePlaylist(serie_source_id=serie_source.id,
                                        custom_name="Drama Show", group_name="Drama",
                                        enabled=True, order=1)
        s.add(playlist_series)
        await s.flush()
        s.add(SeriePlaylistSeason(serie_playlist_id=playlist_series.id,
                                  serie_season_id=season.id, enabled=True))

        secret_source = SerieSource(portal_id=portal.id, portal_item_id="show-secret",
                                    original_name="Secret Show", enabled=True)
        s.add(secret_source)
        await s.flush()
        secret_season = SerieSeason(serie_source_id=secret_source.id,
                                    season_number=1, enabled=True)
        s.add(secret_season)
        await s.flush()
        secret_episode = SerieEpisode(serie_season_id=secret_season.id,
                                      episode_number=1, name="Hidden", cmd="secret")
        secret_series = SeriePlaylist(serie_source_id=secret_source.id,
                                     custom_name="Secret Show", group_name="Secret",
                                     enabled=True, order=2)
        s.add_all([secret_episode, secret_series])
        await s.flush()
        s.add(SeriePlaylistSeason(serie_playlist_id=secret_series.id,
                                  serie_season_id=secret_season.id, enabled=True))

        local_source = LocalSource(directory="/not/a/real/path")
        s.add(local_source)
        await s.flush()
        local_file = LocalFile(local_source_id=local_source.id,
                               relative_path="local.mp4", filename="Local Movie.mp4")
        s.add(local_file)
        await s.flush()
        local = LocalPlaylist(local_file_id=local_file.id, custom_name="Local Movie",
                              group_name="Family", enabled=True, order=1)
        s.add(local)
        secret_file = LocalFile(local_source_id=local_source.id,
                                relative_path="secret.mp4", filename="Secret Movie.mp4")
        s.add(secret_file)
        await s.flush()
        secret_local = LocalPlaylist(local_file_id=secret_file.id,
                                     custom_name="Secret Movie", group_name="Secret",
                                     enabled=True, order=2)
        s.add(secret_local)
        ts = FFmpegTemplate(name="Browser MPEG-TS", enabled=True, is_default=True,
                            output_format="mpegts", command="ffmpeg -i <url> -c copy -f mpegts pipe:1")
        transcode = FFmpegTemplate(name="Browser Transcode", enabled=True, is_default=False,
                                   output_format="mpegts", command="ffmpeg -i <url> -c:v libx264 -f mpegts pipe:1")
        mkv = FFmpegTemplate(name="Matroska only", enabled=True, is_default=False,
                             output_format="matroska", command="ffmpeg -i <url> -c copy -f matroska pipe:1")
        disabled_tpl = FFmpegTemplate(name="Disabled MPEG-TS", enabled=False, is_default=False,
                                      output_format="mpegts", command="ffmpeg -i <url> -c copy -f mpegts pipe:1")
        s.add_all([ts, transcode, mkv, disabled_tpl])
        await s.commit()
        return news.id, hidden.id, disabled.id, ts.id


async def _login(client: AsyncClient) -> None:
    response = await client.post("/player/login", json={
        "username": "viewer", "password": "watch-me",
    })
    assert response.status_code == 200, response.text


async def test_consumer_player_login_lists_visible_output_playlist_and_ts_templates():
    await _seed_player_data()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as client:
        page = await client.get("/player")
        assert page.status_code == 200
        assert 'id="login-form"' in page.text
        assert "/api/dashboard" not in page.text  # the consumer page does not need admin APIs

        before_login = await client.get("/player/api/playlist")
        assert before_login.status_code == 401
        assert before_login.json()["detail"] == "not authenticated"

        bad = await client.post("/player/login", json={
            "username": "viewer", "password": "wrong",
        })
        assert bad.status_code == 403
        await _login(client)

        playlist = await client.get("/player/api/playlist")
        assert playlist.status_code == 200, playlist.text
        assert playlist.json()["user"] == "viewer"
        assert [(item["kind"], item["name"]) for item in playlist.json()["items"]] == [
            ("live", "News One"),
            ("vod", "Movie One"),
            ("episode", "Drama Show S01E01"),
            ("local", "Local Movie.mp4"),
        ]

        templates = await client.get("/player/api/templates")
        assert templates.status_code == 200, templates.text
        names = [item["name"] for item in templates.json()["items"]]
        assert names == ["Browser MPEG-TS", "Browser Transcode"]
        assert "command" not in templates.json()["items"][0]

        logout = await client.post("/player/logout")
        assert logout.status_code == 200
        after_logout = await client.get("/player/api/templates")
        assert after_logout.status_code == 401


async def test_manager_resolves_only_the_preselected_enabled_template():
    await _seed_player_data()
    async with SessionLocal() as s:
        selected_id = (await s.execute(select(FFmpegTemplate.id).where(
            FFmpegTemplate.name == "Browser Transcode"))).scalar_one()
        disabled_id = (await s.execute(select(FFmpegTemplate.id).where(
            FFmpegTemplate.name == "Disabled MPEG-TS"))).scalar_one()
        mkv_id = (await s.execute(select(FFmpegTemplate.id).where(
            FFmpegTemplate.name == "Matroska only"))).scalar_one()

    name, command = await MANAGER._template_for(
        None, kind="live", user_name="viewer", template_id=selected_id)
    assert name == "Browser Transcode"
    assert "libx264" in command
    with pytest.raises(FFmpegTemplateError, match="missing or disabled"):
        await MANAGER._template_for(
            None, kind="live", user_name="viewer", template_id=disabled_id)
    with pytest.raises(FFmpegTemplateError, match="must use MPEG-TS"):
        await MANAGER._template_for(
            None, kind="live", user_name="viewer", template_id=mkv_id)


async def test_player_stream_rechecks_group_access_and_forwards_saved_template(monkeypatch):
    allowed_id, hidden_id, disabled_id, template_id = await _seed_player_data()
    async with SessionLocal() as s:
        vod_id = (await s.execute(select(VodPlaylist.id).where(
            VodPlaylist.custom_name == "Movie One"))).scalar_one()
        hidden_vod_id = (await s.execute(select(VodPlaylist.id).where(
            VodPlaylist.custom_name == "Hidden Movie"))).scalar_one()
        episode_id = (await s.execute(select(SerieEpisode.id).where(
            SerieEpisode.name == "Pilot"))).scalar_one()
        hidden_episode_id = (await s.execute(select(SerieEpisode.id).where(
            SerieEpisode.name == "Hidden"))).scalar_one()
        local_id = (await s.execute(select(LocalPlaylist.id).where(
            LocalPlaylist.custom_name == "Local Movie"))).scalar_one()
        hidden_local_id = (await s.execute(select(LocalPlaylist.id).where(
            LocalPlaylist.custom_name == "Secret Movie"))).scalar_one()
    calls = []

    async def fake_stream(kind, ref_id, user, label, request, mode="", media_type="video/mp2t",
                          template_id=None):
        calls.append((kind, ref_id, user.name, mode, template_id))
        from fastapi.responses import Response
        return Response(content=b"pipeline", media_type=media_type)

    monkeypatch.setattr(output, "_stream_response", fake_stream)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as client:
        await _login(client)

        hidden = await client.get(f"/player/stream/live/{hidden_id}.ts?tpl={template_id}")
        assert hidden.status_code == 404
        disabled = await client.get(f"/player/stream/live/{disabled_id}.ts?tpl={template_id}")
        assert disabled.status_code == 404
        hidden_vod = await client.get(f"/player/stream/vod/{hidden_vod_id}.ts?tpl={template_id}")
        assert hidden_vod.status_code == 404
        hidden_episode = await client.get(
            f"/player/stream/episode/{hidden_episode_id}.ts?tpl={template_id}")
        assert hidden_episode.status_code == 404
        hidden_local = await client.get(
            f"/player/stream/local/{hidden_local_id}.ts?tpl={template_id}")
        assert hidden_local.status_code == 404
        unknown_template = await client.get(f"/player/stream/live/{allowed_id}.ts?tpl=99999")
        assert unknown_template.status_code == 404
        assert calls == []

        response = await client.get(f"/player/stream/live/{allowed_id}.ts?tpl={template_id}")
        assert response.status_code == 200
        assert response.content == b"pipeline"
        expected_calls = [("live", allowed_id, "viewer", "proxy", template_id)]
        assert calls == expected_calls

        for kind, item_id in (("vod", vod_id), ("episode", episode_id),
                              ("local", local_id)):
            response = await client.get(
                f"/player/stream/{kind}/{item_id}.ts?tpl={template_id}")
            assert response.status_code == 200, response.text
            expected_calls.append((kind, item_id, "viewer", "proxy", template_id))
            assert calls == expected_calls

        head = await client.head(f"/player/stream/live/{allowed_id}.ts?tpl={template_id}")
        assert head.status_code == 200
        assert head.headers["content-type"].startswith("video/mp2t")
        assert calls == expected_calls
