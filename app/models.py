"""
Database model - revised schema from docs/PHASE1-REQUIREMENTS-ANALYSIS.md.

Phase-1 user decisions applied:
  Q1 = C  -> VOD/series DO have fallback sources (child tables with priority),
             logos are automatic (portal/TMDB) - column kept, no manual override.
  Q2 = A  -> untranscoded streams go through ffmpeg -c copy (uniform pipeline).
  Q3 = A  -> Postgres in production (sqlite allowed for dev only).
  Q5 = A  -> admin login for the GUI (session; not represented here).
  Q6 = A  -> series enablement stops at season level; episodes are stored for
             output generation but carry no per-episode UI toggle.

Naming: python class CamelCase, table snake_case. All booleans default False.
Every FK is indexed; heavy filter columns (enabled, name) are indexed too so
the large source tables stay fast on Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, BigInteger, String, Text, SmallInteger,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ===========================================================================
# PORTALS
# ===========================================================================
class Portal(Base):
    """One Stalker/Ministra portal. Genres live in their own tables (D1/D2)."""

    __tablename__ = "portals"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    base_url: Mapped[str] = mapped_column(String(500))                    # as typed by the user
    resolved_url: Mapped[str | None] = mapped_column(String(500))         # .../portal.php that works
    resolved_path: Mapped[str | None] = mapped_column(String(120))        # the path that won (/c/, ...)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    proxy_url: Mapped[str | None] = mapped_column(String(300))            # optional http proxy
    # What we tell the panel we are. "mag250" sends the device fingerprint a real
    # box sends (see app/portal/identity.py); "minimal" is the escape hatch for a
    # panel that rejects an identity it never enrolled - a WRONG fingerprint is
    # worse than none, so the user has to be able to switch without a code change.
    identity_mode: Mapped[str] = mapped_column(String(12), default="mag250")
    stb_timezone: Mapped[str | None] = mapped_column(String(64))          # cookie a MAG sends
    # Opt-out for panels with a broken/self-signed certificate chain. False by
    # default: verification is ON for every portal, and this only ever widens
    # trust for ONE portal the user explicitly says is misconfigured.
    tls_insecure: Mapped[bool] = mapped_column(Boolean, default=False)
    # What the panel says about *itself* (R6). Read from version.js, which needs
    # no token, and from get_modules, which does. Both are informational unless
    # `modules` is non-NULL: a portal that never answered has not told us it
    # lacks series, and gating on that would hide a working catalogue.
    #: R2: play a stored link when the channel's own flags say it is permanent,
    #: instead of asking for a new one on every open. On by default, and the
    #: switch exists because a panel that answers `use_http_tmp_link=0` and then
    #: 403s the URL is worth one checkbox, not a code change.
    direct_links: Mapped[bool] = mapped_column(Boolean, default=True)
    portal_version: Mapped[str | None] = mapped_column(String(120))
    modules: Mapped[str | None] = mapped_column(Text)        # JSON list, NULL = unknown
    capabilities_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    macs: Mapped[list["MacAddress"]] = relationship(
        back_populates="portal", cascade="all, delete-orphan",
        order_by="MacAddress.order",
    )


class MacAddress(Base):
    """A MAC that can authenticate against a portal. Order = preference (D13)."""

    __tablename__ = "mac_addresses"
    __table_args__ = (UniqueConstraint("portal_id", "mac", name="uq_mac_per_portal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portal_id: Mapped[int] = mapped_column(ForeignKey("portals.id", ondelete="CASCADE"), index=True)
    mac: Mapped[str] = mapped_column(String(17))
    password: Mapped[str | None] = mapped_column(String(120))             # rare, some portals need it
    order: Mapped[int] = mapped_column(Integer, default=0)                # try order within portal
    # `banned` (the panel disabled this MAC) and `expired` (its subscription
    # ended) come from the portal itself and take the MAC out of every fallback
    # chain; the rest are our own verdicts about transport and stay retryable.
    status: Mapped[str] = mapped_column(String(20), default="unknown")    # unknown/online/offline/unauthorized/expired/banned/error
    online: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    expire_date: Mapped[str | None] = mapped_column(String(40))           # as reported by the portal
    last_error: Mapped[str | None] = mapped_column(String(200))           # why, in the panel's own words
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The panel asks every link to be re-validated for this account (create_link
    # with force_ch_link_check=1). Stored so the stream path can honour it.
    force_ch_link_check: Mapped[bool] = mapped_column(Boolean, default=False)
    # Pinned STB identity: derived from the MAC when empty, kept stable forever
    # when the real box's values were captured (a re-generated serial is how a
    # working account gets flagged as a new device).
    sn: Mapped[str | None] = mapped_column(String(40))
    device_id: Mapped[str | None] = mapped_column(String(80))

    portal: Mapped[Portal] = relationship(back_populates="macs")


# ===========================================================================
# GENRES  (all fetched genres; user flips `enabled`)  (D2/D3/D18)
# ===========================================================================
class LiveGenre(Base):
    __tablename__ = "live_genres"
    __table_args__ = (UniqueConstraint("portal_id", "genre_portal_id", name="uq_live_genre"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portal_id: Mapped[int] = mapped_column(ForeignKey("portals.id", ondelete="CASCADE"), index=True)
    genre_portal_id: Mapped[str] = mapped_column(String(40))   # numeric id of the genre IN the portal
    name: Mapped[str] = mapped_column(String(300), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    adult: Mapped[bool] = mapped_column(Boolean, default=False)
    item_count: Mapped[int | None] = mapped_column(Integer)     # cached portal total (dashboard Y in "X of Y")
    channels_fetched: Mapped[bool] = mapped_column(Boolean, default=False)


class VodGenre(Base):
    __tablename__ = "vod_genres"
    __table_args__ = (UniqueConstraint("portal_id", "genre_portal_id", name="uq_vod_genre"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portal_id: Mapped[int] = mapped_column(ForeignKey("portals.id", ondelete="CASCADE"), index=True)
    genre_portal_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(300), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    item_count: Mapped[int | None] = mapped_column(Integer)
    items_fetched: Mapped[bool] = mapped_column(Boolean, default=False)


class SerieGenre(Base):
    __tablename__ = "serie_genres"
    __table_args__ = (UniqueConstraint("portal_id", "genre_portal_id", name="uq_serie_genre"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portal_id: Mapped[int] = mapped_column(ForeignKey("portals.id", ondelete="CASCADE"), index=True)
    genre_portal_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(300), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    item_count: Mapped[int | None] = mapped_column(Integer)
    items_fetched: Mapped[bool] = mapped_column(Boolean, default=False)


# ===========================================================================
# SOURCES - items fetched from enabled genres  (D4: cmd/id are critical!)
# ===========================================================================
class LiveSource(Base):
    """A live channel as offered by a portal."""

    __tablename__ = "live_sources"
    __table_args__ = (
        UniqueConstraint("portal_id", "portal_channel_id", name="uq_live_source"),
        Index("ix_live_sources_name_lower", "original_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portal_id: Mapped[int] = mapped_column(ForeignKey("portals.id", ondelete="CASCADE"), index=True)
    live_genre_id: Mapped[int | None] = mapped_column(ForeignKey("live_genres.id", ondelete="SET NULL"), index=True)
    portal_channel_id: Mapped[str] = mapped_column(String(60))            # 'id' in portal payload
    number: Mapped[str | None] = mapped_column(String(20))                # original channel number
    original_name: Mapped[str] = mapped_column(String(300), index=True)
    cmd: Mapped[str | None] = mapped_column(Text)                         # needed for create_link!
    logo_original: Mapped[str | None] = mapped_column(String(600))
    epg_original: Mapped[str | None] = mapped_column(String(200))         # tvg id hint from portal
    tv_archive: Mapped[bool] = mapped_column(Boolean, default=False)
    censored: Mapped[bool] = mapped_column(Boolean, default=False)
    #: the channel's own link flags (`use_http_tmp_link,disable_ad`), as the
    #: panel sent them. NULL means the panel said nothing - which is NOT the same
    #: as "" (it said nothing applies), and the difference is what decides
    #: whether a stream open costs a create_link. See app/portal/links.py.
    link_flags: Mapped[str | None] = mapped_column(String(60))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # user: include in output pool


class VodSource(Base):
    __tablename__ = "vod_sources"
    __table_args__ = (UniqueConstraint("portal_id", "portal_item_id", name="uq_vod_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portal_id: Mapped[int] = mapped_column(ForeignKey("portals.id", ondelete="CASCADE"), index=True)
    vod_genre_id: Mapped[int | None] = mapped_column(ForeignKey("vod_genres.id", ondelete="SET NULL"), index=True)
    portal_item_id: Mapped[str] = mapped_column(String(60))
    original_name: Mapped[str] = mapped_column(String(400), index=True)
    cmd: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int | None] = mapped_column(Integer)                 # original position in portal list
    poster: Mapped[str | None] = mapped_column(String(600))
    year: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(Text)                 # placeholder for TMDB merge (G3)
    genre: Mapped[str | None] = mapped_column(String(300))
    director: Mapped[str | None] = mapped_column(String(300))
    actors: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[str | None] = mapped_column(String(10))
    duration: Mapped[str | None] = mapped_column(String(20))
    added: Mapped[str | None] = mapped_column(String(40))
    link_flags: Mapped[str | None] = mapped_column(String(60))   # see LiveSource.link_flags
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class SerieSource(Base):
    __tablename__ = "serie_sources"
    __table_args__ = (UniqueConstraint("portal_id", "portal_item_id", name="uq_serie_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portal_id: Mapped[int] = mapped_column(ForeignKey("portals.id", ondelete="CASCADE"), index=True)
    serie_genre_id: Mapped[int | None] = mapped_column(ForeignKey("serie_genres.id", ondelete="SET NULL"), index=True)
    portal_item_id: Mapped[str] = mapped_column(String(60))               # may look like '56359:56359'
    original_name: Mapped[str] = mapped_column(String(400), index=True)
    poster: Mapped[str | None] = mapped_column(String(600))
    year: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[str | None] = mapped_column(String(10))
    category_name: Mapped[str | None] = mapped_column(String(300))
    raw_series: Mapped[str | None] = mapped_column(Text)                  # stored series/seasons metadata
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    seasons_fetched: Mapped[bool] = mapped_column(Boolean, default=False)

    seasons: Mapped[list["SerieSeason"]] = relationship(
        back_populates="serie", cascade="all, delete-orphan", order_by="SerieSeason.season_number"
    )


class SerieSeason(Base):
    """Season-level enablement (Q6=A). Episodes exist for output only."""

    __tablename__ = "serie_seasons"
    __table_args__ = (UniqueConstraint("serie_source_id", "season_number", name="uq_season"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    serie_source_id: Mapped[int] = mapped_column(ForeignKey("serie_sources.id", ondelete="CASCADE"), index=True)
    portal_season_id: Mapped[str | None] = mapped_column(String(60))
    season_number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(300))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    episodes_fetched: Mapped[bool] = mapped_column(Boolean, default=False)

    serie: Mapped[SerieSource] = relationship(back_populates="seasons")
    episodes: Mapped[list["SerieEpisode"]] = relationship(
        back_populates="season", cascade="all, delete-orphan", order_by="SerieEpisode.episode_number"
    )


class SerieEpisode(Base):
    __tablename__ = "serie_episodes"
    __table_args__ = (UniqueConstraint("serie_season_id", "episode_number", name="uq_episode"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    serie_season_id: Mapped[int] = mapped_column(ForeignKey("serie_seasons.id", ondelete="CASCADE"), index=True)
    portal_item_id: Mapped[str | None] = mapped_column(String(60))
    episode_number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(400))
    cmd: Mapped[str | None] = mapped_column(Text)                          # set on real portals, empty on pure series-rows
    duration: Mapped[str | None] = mapped_column(String(20))
    link_flags: Mapped[str | None] = mapped_column(String(60))    # see LiveSource.link_flags

    season: Mapped[SerieSeason] = relationship(back_populates="episodes")


# ===========================================================================
# LOCAL FILES  (D9: directory -> scanned files -> playlist rows)
# ===========================================================================
class LocalSource(Base):
    __tablename__ = "local_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    directory: Mapped[str] = mapped_column(String(600), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scan: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LocalFile(Base):
    __tablename__ = "local_files"
    __table_args__ = (UniqueConstraint("local_source_id", "relative_path", name="uq_local_file"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    local_source_id: Mapped[int] = mapped_column(ForeignKey("local_sources.id", ondelete="CASCADE"), index=True)
    relative_path: Mapped[str] = mapped_column(String(900))
    filename: Mapped[str] = mapped_column(String(400), index=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mtime: Mapped[str | None] = mapped_column(String(40))
    duration_s: Mapped[float | None] = mapped_column(Float)   # probed; drives M3U EXTINF
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


# ===========================================================================
# FFMPEG TEMPLATES  (D12: options-json + command text + last-edited side)
# ===========================================================================
class FFmpegTemplate(Base):
    __tablename__ = "ffmpeg_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)       # shipped preset (managed by the app)
    # ----- structured options (dropdown state; mirrored with `command`) -----
    hw_accel: Mapped[str] = mapped_column(String(10), default="vaapi")     # none|vaapi|qsv
    device: Mapped[str] = mapped_column(String(60), default="/dev/dri/renderD128")
    resolution: Mapped[str] = mapped_column(String(10), default="720p")    # 360p..2160p|source
    aspect: Mapped[str] = mapped_column(String(6), default="16:9")         # 4:3|16:9|21:9 (informational + setsar)
    video_codec: Mapped[str] = mapped_column(String(20), default="h264_vaapi")
    video_bitrate: Mapped[str] = mapped_column(String(10), default="1000k")
    maxrate: Mapped[str] = mapped_column(String(10), default="1200k")
    bufsize: Mapped[str] = mapped_column(String(10), default="1800k")
    fps: Mapped[str] = mapped_column(String(6), default="25")
    gop: Mapped[str] = mapped_column(String(8), default="50")
    profile: Mapped[str] = mapped_column(String(12), default="high")
    level: Mapped[str] = mapped_column(String(8), default="4.1")
    low_power: Mapped[bool] = mapped_column(Boolean, default=True)          # h264_vaapi EncSliceLP (DS918+ fixed-function encoder)
    rc_mode: Mapped[str] = mapped_column(String(10), default="VBR")         # AUTO|CQP|CBR|VBR|ICQ|QVBR|AVBR
    async_depth: Mapped[str] = mapped_column(String(4), default="4")        # VAAPI frames in flight
    audio_codec: Mapped[str] = mapped_column(String(12), default="aac")    # aac|ac3|mp3|copy|none
    audio_bitrate: Mapped[str] = mapped_column(String(10), default="128k")
    audio_channels: Mapped[str] = mapped_column(String(4), default="2")
    audio_rate: Mapped[str] = mapped_column(String(8), default="48000")
    output_format: Mapped[str] = mapped_column(String(10), default="mpegts")  # mpegts|hls
    # ----- two-way sync -----
    extra_input: Mapped[str | None] = mapped_column(Text)                  # raw extra input flags
    extra_output: Mapped[str | None] = mapped_column(Text)                 # raw extra output flags
    command: Mapped[str | None] = mapped_column(Text)                      # rendered/edited full command
    command_source: Mapped[str] = mapped_column(String(8), default="fields")  # fields|manual


# ===========================================================================
# PLAYLIST (final output)  (D6: fallback = ordered child rows, first = primary)
# ===========================================================================
class LivePlaylist(Base):
    __tablename__ = "live_playlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    custom_name: Mapped[str] = mapped_column(String(300), index=True)
    group_name: Mapped[str | None] = mapped_column(String(300), index=True)
    number: Mapped[int | None] = mapped_column(Integer)                    # optional custom channel number
    epg_id: Mapped[str | None] = mapped_column(String(200))                # tvg-id in final m3u
    logo: Mapped[str | None] = mapped_column(String(600))                  # user-overridable (live only)
    ffmpeg_template_id: Mapped[int | None] = mapped_column(ForeignKey("ffmpeg_templates.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    order: Mapped[int] = mapped_column(Integer, default=0, index=True)

    sources: Mapped[list["LivePlaylistSource"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="LivePlaylistSource.priority"
    )


class LivePlaylistSource(Base):
    """Ordered fallback chain for one custom live channel. priority 1 = primary."""

    __tablename__ = "live_playlist_sources"
    __table_args__ = (UniqueConstraint("live_playlist_id", "live_source_id", name="uq_lps"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    live_playlist_id: Mapped[int] = mapped_column(ForeignKey("live_playlist.id", ondelete="CASCADE"), index=True)
    live_source_id: Mapped[int] = mapped_column(ForeignKey("live_sources.id", ondelete="CASCADE"), index=True)
    priority: Mapped[int] = mapped_column(SmallInteger, default=1)

    item: Mapped[LivePlaylist] = relationship(back_populates="sources")
    live_source: Mapped[LiveSource] = relationship()


class VodPlaylist(Base):
    __tablename__ = "vod_playlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    vod_source_id: Mapped[int] = mapped_column(ForeignKey("vod_sources.id", ondelete="CASCADE"), index=True)
    custom_name: Mapped[str] = mapped_column(String(400))
    group_name: Mapped[str | None] = mapped_column(String(300), index=True)
    logo: Mapped[str | None] = mapped_column(String(600))                  # automatic only (Q1=C)
    ffmpeg_template_id: Mapped[int | None] = mapped_column(ForeignKey("ffmpeg_templates.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)
    poster: Mapped[str | None] = mapped_column(String(600))
    rating: Mapped[str | None] = mapped_column(String(10))
    year: Mapped[str | None] = mapped_column(String(10))

    vod_source: Mapped[VodSource] = relationship()
    sources: Mapped[list["VodPlaylistSource"]] = relationship(
        cascade="all, delete-orphan", order_by="VodPlaylistSource.priority"
    )


class VodPlaylistSource(Base):
    __tablename__ = "vod_playlist_sources"
    __table_args__ = (UniqueConstraint("vod_playlist_id", "vod_source_id", name="uq_vps"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vod_playlist_id: Mapped[int] = mapped_column(ForeignKey("vod_playlist.id", ondelete="CASCADE"), index=True)
    vod_source_id: Mapped[int] = mapped_column(ForeignKey("vod_sources.id", ondelete="CASCADE"), index=True)
    priority: Mapped[int] = mapped_column(SmallInteger, default=1)


class SeriePlaylist(Base):
    __tablename__ = "serie_playlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    serie_source_id: Mapped[int] = mapped_column(ForeignKey("serie_sources.id", ondelete="CASCADE"), index=True)
    custom_name: Mapped[str] = mapped_column(String(400))
    group_name: Mapped[str | None] = mapped_column(String(300), index=True)
    logo: Mapped[str | None] = mapped_column(String(600))                  # automatic only (Q1=C)
    ffmpeg_template_id: Mapped[int | None] = mapped_column(ForeignKey("ffmpeg_templates.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)
    poster: Mapped[str | None] = mapped_column(String(600))
    rating: Mapped[str | None] = mapped_column(String(10))
    year: Mapped[str | None] = mapped_column(String(10))

    serie_source: Mapped[SerieSource] = relationship()
    sources: Mapped[list["SeriePlaylistSource"]] = relationship(
        cascade="all, delete-orphan", order_by="SeriePlaylistSource.priority"
    )
    seasons: Mapped[list["SeriePlaylistSeason"]] = relationship(cascade="all, delete-orphan")


class SeriePlaylistSource(Base):
    __tablename__ = "serie_playlist_sources"
    __table_args__ = (UniqueConstraint("serie_playlist_id", "serie_source_id", name="uq_sps"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    serie_playlist_id: Mapped[int] = mapped_column(ForeignKey("serie_playlist.id", ondelete="CASCADE"), index=True)
    serie_source_id: Mapped[int] = mapped_column(ForeignKey("serie_sources.id", ondelete="CASCADE"), index=True)
    priority: Mapped[int] = mapped_column(SmallInteger, default=1)


class SeriePlaylistSeason(Base):
    """Which seasons of a playlist-series land in the output."""

    __tablename__ = "serie_playlist_seasons"
    __table_args__ = (UniqueConstraint("serie_playlist_id", "serie_season_id", name="uq_spls"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    serie_playlist_id: Mapped[int] = mapped_column(ForeignKey("serie_playlist.id", ondelete="CASCADE"), index=True)
    serie_season_id: Mapped[int] = mapped_column(ForeignKey("serie_seasons.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class LocalPlaylist(Base):
    __tablename__ = "local_playlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    local_file_id: Mapped[int] = mapped_column(ForeignKey("local_files.id", ondelete="CASCADE"), index=True)
    custom_name: Mapped[str | None] = mapped_column(String(400))
    group_name: Mapped[str] = mapped_column(String(300), default="vod-local", index=True)
    ffmpeg_template_id: Mapped[int | None] = mapped_column(ForeignKey("ffmpeg_templates.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)

    local_file: Mapped[LocalFile] = relationship()


# ===========================================================================
# USERS / EPG / SETTINGS / LOGS / RUNTIME
# ===========================================================================
class User(Base):
    """Output consumer (M3U and/or Xtream). NOT the GUI admin (env-based login)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    password: Mapped[str] = mapped_column(String(120))
    m3u_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    xtream_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expire_date: Mapped[str | None] = mapped_column(String(25))            # ISO date, optional
    max_connections: Mapped[int] = mapped_column(Integer, default=1)       # enforced by stream manager
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    groups_json: Mapped[str | None] = mapped_column(Text)                  # {live:[],vod:[],series:[],local:[]}
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EpgSource(Base):
    __tablename__ = "epg_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(600), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fetch: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(120))
    channel_count: Mapped[int | None] = mapped_column(Integer)


class EpgChannel(Base):
    """Parsed <channel> entries of all enabled EPG sources (Phase 3)."""
    __tablename__ = "epg_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    epg_source_id: Mapped[int] = mapped_column(ForeignKey("epg_sources.id"), index=True)
    tvg_id: Mapped[str] = mapped_column(String(200), index=True)         # XMLTV channel id
    name: Mapped[str] = mapped_column(String(300))                       # display-name
    icon: Mapped[str | None] = mapped_column(String(600))
    __table_args__ = (Index("ix_epg_channels_uniq", "epg_source_id", "tvg_id", unique=True),)


class EpgProgramme(Base):
    """Programme rows for the channels we actually output (bounded on purpose:
    only tvg_ids referenced by live_playlist.epg_id are ingested)."""
    __tablename__ = "epg_programmes"

    id: Mapped[int] = mapped_column(primary_key=True)
    tvg_id: Mapped[str] = mapped_column(String(200), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    stop_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    title: Mapped[str] = mapped_column(String(400))
    sub_title: Mapped[str | None] = mapped_column(String(400))
    desc: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(200))
    icon: Mapped[str | None] = mapped_column(String(600))
    __table_args__ = (
        Index("ix_epg_prog_tvg_start", "tvg_id", "start_ts"),
        UniqueConstraint("tvg_id", "start_ts", "title", name="uq_epg_prog_natural"),
    )


class Log(Base):
    """Persistent info/warning/error feed (dashboard messages pane)."""

    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), index=True)              # INFO/WARNING/ERROR/DEBUG
    module: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str] = mapped_column(Text)


class ActiveStream(Base):
    """Runtime mirror of the stream manager (purged at boot; D15)."""

    __tablename__ = "active_streams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)          # uuid
    kind: Mapped[str] = mapped_column(String(10))                          # live|vod|episode|local|preview
    item_name: Mapped[str] = mapped_column(String(400))
    user_name: Mapped[str | None] = mapped_column(String(120), index=True)
    portal_name: Mapped[str | None] = mapped_column(String(200))
    mac: Mapped[str | None] = mapped_column(String(17))
    template_name: Mapped[str | None] = mapped_column(String(120))
    pid: Mapped[int | None] = mapped_column(Integer)
    started: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)


class Setting(Base):
    """Simple key/value store for everything persistent that isn't tabular (D17)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)                        # JSON-encoded
