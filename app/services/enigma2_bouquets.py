"""
Enigma2 bouquet rendering: playlist rows -> `userbouquet.*.tv` files.

This is the pure half of the Enigma2 output (the transfer to the box lives in
E3): a profile goes in, a list of files comes out. No I/O beyond the database
reads, so the whole layout is unit-testable and the GUI can preview exactly
what would be written.

WHAT A BOUQUET LOOKS LIKE

    #NAME SPM - Live
    #SERVICE 4097:0:1:2A:0:0:0:0:0:0:http%3a//nas%3a8880/play/live/42.ts?u=box&p=pw:Das Erste HD
    #DESCRIPTION Das Erste HD
    #SERVICE 1:64:0:0:0:0:0:0:0:0::Sport
    #DESCRIPTION Sport

The details that bite:

* Enigma2 splits a service reference on ':', so every colon INSIDE the URL has
  to be `%3a` (lower case is what every existing tool writes) and the display
  name after the last colon must not contain one at all.
* The 4th field is the SID. We put the SPM playlist id there: it is stable
  across regeneration, which is what keeps the user's own favourites, picon
  names and (later) the EPGImport channel map pointing at the same services.
* Each `#SERVICE` needs its own `#DESCRIPTION`; several images show an empty
  name without it.
* `1:64:...::Label` is a MARKER - a non-playable separator line, which is how a
  flat bouquet still gets per-group / per-series / per-season headings.
* The leading number is the PLAYER: 1 = DVB pipeline (live TS, native DVB
  subtitles), 4097 = servicemp3, 5001 = ServiceApp/gstplayer,
  5002 = ServiceApp/exteplayer3 (the one that shows text subtitles from a
  Matroska stream - see docs/ENIGMA2-INTEGRATION-OPTIONS.md).

`bouquets.tv` is EDITED, never overwritten: our lines are replaced, everybody
else's are kept, because that file also lists the user's satellite bouquets.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import quote

from sqlalchemy import select

from ..database import SessionLocal
from ..models import (
    Enigma2Profile, FFmpegTemplate, LivePlaylist, LocalFile, LocalPlaylist,
    SerieEpisode, SeriePlaylist, SeriePlaylistSeason, SerieSeason, User,
    VodPlaylist, VodSource,
)
from .ffmpeg_templates import REDIRECT_COMMAND
from .playlist_gen import _allowed, _chunked, _groups
from .titles import best_title

# Service reference leading numbers we offer (see module docstring).
PLAYERS = {
    "1": "DVB pipeline (raw TS, native DVB subtitles, lowest latency)",
    "4097": "servicemp3 / gstreamer (the generic default)",
    "5001": "ServiceApp - gstplayer",
    "5002": "ServiceApp - exteplayer3 (text subtitles, multi-audio)",
}
CONTAINERS = ("ts", "mkv")
# How the URL alias (and the player) is chosen for each item:
#   auto  - from the item's own ffmpeg template, per item (default)
#   fixed - always the profile's per-kind container/player
CONTAINER_MODES = ("auto", "fixed")
# Players that can read an arbitrary container over HTTP (they demux with
# gstreamer/ffmpeg). Service type 1 hands the bytes to the DVB pipeline
# unchanged, so it can only ever play raw MPEG-TS.
FFMPEG_PLAYERS = ("4097", "5001", "5002")
LAYOUTS = ("group_markers", "per_series", "flat")
DELIVERY_MODES = ("template", "proxy", "redirect")
TRANSPORTS = ("ftp", "download", "ssh")
# Where the files belong on the receiver.
E2_DIR = "/etc/enigma2"
BOUQUETS_TV = "bouquets.tv"
# A bouquets.tv entry for one of our files.
_BOUQUET_LINE = ('#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "{file}" ORDER BY bouquet')
MARKER_REF = "1:64:0:0:0:0:0:0:0:0:"


# --------------------------------------------------------------------------- #
#  primitives
# --------------------------------------------------------------------------- #
def new_token() -> str:
    """Opaque id for the public pull endpoints."""
    return secrets.token_urlsafe(24)


def escape_url(url: str) -> str:
    """URL as an Enigma2 service-reference field.

    Only two characters may not survive: ':' ends the field, and whitespace
    ends the line. Everything else (including '?' and '&') is passed through -
    the box hands the string to its player as-is.
    """
    return url.replace(":", "%3a").replace(" ", "%20")


def clean_name(name: str) -> str:
    """Display name for a service line: single line, no ':' (it would be read
    as another reference field), collapsed whitespace."""
    out = re.sub(r"\s+", " ", (name or "").replace(":", " -")).strip()
    return out or "Unnamed"


def bouquet_filename(prefix: str, slug: str) -> str:
    return f"userbouquet.{slugify(prefix)}_{slugify(slug)}.tv"


def slugify(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return out or "x"


def service_line(player: str, sid: int, url: str, name: str,
                 service_type: int = 1) -> list[str]:
    """`#SERVICE` + its `#DESCRIPTION`, as two lines."""
    nm = clean_name(name)
    ref = (f"{player}:0:{service_type}:{sid:X}:0:0:0:0:0:0:"
           f"{escape_url(url)}:{nm}")
    return [f"#SERVICE {ref}", f"#DESCRIPTION {nm}"]


def marker_line(label: str) -> list[str]:
    nm = clean_name(label)
    return [f"#SERVICE {MARKER_REF}:{nm}", f"#DESCRIPTION {nm}"]


@dataclass
class Delivery:
    """What one playlist item will actually send, and how the box must read it.

    A library is mixed on purpose: one movie is assigned the redirect template
    (302 to the panel's CDN - original container, its own subtitle tracks, and
    seeking), the next one an MKV remux, a 4K/HEVC one the VAAPI Matroska
    transcode, and live TV stays MPEG-TS. The bouquet line has to match the
    item, not the profile, or the box is handed a `.ts` URL that answers with
    Matroska (or worse, a service type that cannot demux it at all).
    """
    kind: str                # ts | mkv | direct
    container: str           # the URL alias to use (.ts / .mkv)
    player: str              # leading number of the service reference
    template: str = ""       # template name, for the preview
    note: str = ""           # why the player was changed, if it was


class _Resolver:
    """Maps `ffmpeg_template_id -> Delivery` for one profile.

    `direct` is the redirect template (`@redirect`): SPM answers 302 and the
    receiver fetches the panel's own file. Its container is whatever the panel
    serves - unknowable from here - so the URL alias stays cosmetic and only
    the PLAYER matters: service type 1 would hand an MP4/MKV/HLS body to the
    DVB pipeline and show a black screen, so it is upgraded to 4097.
    """

    def __init__(self, templates: dict, default_tpl, profile: Enigma2Profile,
                 any_tpl=None) -> None:
        self.tpl = templates
        self.default = default_tpl
        self.any = any_tpl
        self.p = profile
        self.mode = profile.container_mode if profile.container_mode in CONTAINER_MODES else "auto"
        self.counts = {"ts": 0, "mkv": 0, "direct": 0}
        self.notes: set[str] = set()
        # (content kind, player) actually written with a .mkv url - the
        # "no text subtitles there" warning has to follow what was WRITTEN,
        # not what the templates would have produced
        self.mkv_players: set[tuple[str, str]] = set()

    def _row(self, template_id):
        row = self.tpl.get(template_id) if template_id else None
        if row is None or not row.get("enabled"):
            row = self.default or self.any   # same chain the streamer walks
        return row

    def for_item(self, template_id, kind: str) -> Delivery:
        """kind is the CONTENT kind (live/vod/series/local), not the container."""
        container = {"live": self.p.container_live, "vod": self.p.container_vod,
                     "series": self.p.container_series}.get(kind, self.p.container_vod)
        player = {"live": self.p.player_live, "vod": self.p.player_vod,
                  "series": self.p.player_series}.get(kind, self.p.player_vod)
        row = self._row(template_id)
        name = (row or {}).get("name", "")
        if (row or {}).get("command", "").strip() == REDIRECT_COMMAND:
            what = "direct"
        elif (row or {}).get("output_format") == "matroska":
            what = "mkv"
        else:
            what = "ts"
        self.counts[what] += 1

        if self.mode == "fixed":
            if container == "mkv":
                self.mkv_players.add((kind, player))
            return Delivery(what, container, player, name)

        note = ""
        if what == "ts":
            container = "ts"
        elif what == "mkv":
            container = "mkv"
            if player not in FFMPEG_PLAYERS:
                player, note = "4097", ("service type 1 cannot demux Matroska - "
                                        "player raised to 4097 (use 5002 for subtitles)")
        else:                                   # direct: the panel decides
            if player not in FFMPEG_PLAYERS:
                player, note = "4097", ("direct items deliver the panel's own container - "
                                        "service type 1 cannot play it, player raised to 4097")
        if note:
            self.notes.add(note)
        if container == "mkv":
            self.mkv_players.add((kind, player))
        return Delivery(what, container, player, name, note)


@dataclass
class BouquetFile:
    name: str                       # userbouquet.spm_live.tv
    title: str                      # #NAME value, as shown in the box
    lines: list[str] = field(default_factory=list)
    services: int = 0

    @property
    def text(self) -> str:
        return "\n".join([f"#NAME {self.title}", *self.lines]) + "\n"


@dataclass
class Bundle:
    files: list[BouquetFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # how the items split across delivery kinds (ts / mkv / direct) - shown in
    # the preview, because "why does THIS movie have no subtitles" is answered
    # by the item's template, not by the profile
    deliveries: dict = field(default_factory=lambda: {"ts": 0, "mkv": 0, "direct": 0})

    @property
    def services(self) -> int:
        return sum(f.services for f in self.files)

    def summary(self) -> dict:
        return {"bouquets": len(self.files), "services": self.services,
                "deliveries": self.deliveries,
                "files": [{"name": f.name, "title": f.title, "services": f.services}
                          for f in self.files],
                "warnings": self.warnings}


class _Writer:
    """Accumulates lines and splits them into numbered bouquets at `max_entries`.

    Enigma2 walks a bouquet linearly and redraws the whole list on every zap,
    so a single 12 000-entry file is what turns a channel list into a slideshow.
    Splitting keeps each file browsable; the marker lines are repeated at the
    top of a continuation so the user can still see where they are.
    """

    def __init__(self, prefix: str, slug: str, title: str, max_entries: int) -> None:
        self.prefix, self.slug, self.title = prefix, slug, title
        self.max_entries = max(50, int(max_entries or 1500))
        self.files: list[BouquetFile] = []
        self._start()

    def _start(self, continued: bool = False) -> None:
        n = len(self.files) + 1
        suffix = "" if n == 1 else f"_{n}"
        title = self.title if n == 1 else f"{self.title} ({n})"
        self.cur = BouquetFile(name=bouquet_filename(self.prefix, self.slug + suffix),
                               title=title)
        self.files.append(self.cur)

    def marker(self, label: str) -> None:
        self.cur.lines += marker_line(label)

    def service(self, player: str, sid: int, url: str, name: str) -> None:
        if self.cur.services >= self.max_entries:
            self._start(continued=True)
        self.cur.lines += service_line(player, sid, url, name)
        self.cur.services += 1

    def done(self) -> list[BouquetFile]:
        return [f for f in self.files if f.services]


# --------------------------------------------------------------------------- #
#  URL building
# --------------------------------------------------------------------------- #
def _creds(user: User | None) -> str:
    if not user:
        return ""
    return f"?u={quote(user.name)}&p={quote(user.password)}"


def stream_url(base_url: str, kind: str, item_id: int, container: str,
               user: User | None, delivery_mode: str = "template",
               ext: str | None = None) -> str:
    """One playable URL for a bouquet line.

    `container` only picks the URL alias (`.ts` / `.mkv`): what is actually
    muxed is decided by the item's ffmpeg template. They have to agree - a
    `.mkv` line on an item whose template says mpegts still plays, but its text
    subtitles do not exist in the first place. The GUI preview warns about it.
    """
    suffix = ext if ext is not None else "." + (container if container in CONTAINERS else "ts")
    url = f"{base_url.rstrip('/')}/play/{kind}/{item_id}{suffix}{_creds(user)}"
    if delivery_mode in ("proxy", "redirect"):
        url += ("&" if "?" in url else "?") + f"mode={delivery_mode}"
    return url


# --------------------------------------------------------------------------- #
#  the renderer
# --------------------------------------------------------------------------- #
def _profile_groups(profile: Enigma2Profile) -> dict:
    try:
        g = json.loads(profile.groups_json or "{}")
    except (json.JSONDecodeError, TypeError):
        g = {}
    if not isinstance(g, dict):
        g = {}
    return {k: list(g.get(k, [])) for k in ("live", "vod", "series", "local")}


def _visible(group_name: str | None, user_groups: list[str],
             profile_groups: list[str]) -> bool:
    """Both filters apply: the user's whitelist (what that account may see) and
    the profile's own (what this box should carry)."""
    return _allowed(group_name, user_groups) and _allowed(group_name, profile_groups)


async def build_bundle(profile: Enigma2Profile, base_url: str) -> Bundle:
    """Render every bouquet file for one profile."""
    bundle = Bundle()
    async with SessionLocal() as s:
        user = await s.get(User, profile.user_id) if profile.user_id else None
    if profile.user_id and user is None:
        bundle.warnings.append("the profile's output user was deleted - the "
                               "generated URLs carry no credentials and will be "
                               "rejected with 403")
    ugroups = _groups(user) if user else {k: [] for k in ("live", "vod", "series", "local")}
    pgroups = _profile_groups(profile)
    prefix = profile.bouquet_prefix or "spm"
    layout = profile.layout if profile.layout in LAYOUTS else "group_markers"
    mode = profile.delivery_mode if profile.delivery_mode in DELIVERY_MODES else "template"

    async with SessionLocal() as s:
        rows = (await s.execute(select(FFmpegTemplate))).scalars().all()
        templates = {r.id: {"name": r.name, "command": r.command or "",
                            "output_format": r.output_format, "enabled": r.enabled}
                     for r in rows}
        default_tpl = next((templates[r.id] for r in rows if r.is_default), None)
        res = _Resolver(templates, default_tpl, profile,
                        templates[rows[0].id] if rows else None)

        if profile.include_live:
            bundle.files += await _live_files(s, profile, base_url, user, ugroups,
                                              pgroups, prefix, layout, mode, res)
        if profile.include_vod:
            bundle.files += await _vod_files(s, profile, base_url, user, ugroups,
                                             pgroups, prefix, layout, mode, res)
        if profile.include_series:
            bundle.files += await _series_files(s, profile, base_url, user, ugroups,
                                                pgroups, prefix, layout, mode, res)
        if profile.include_local:
            bundle.files += await _local_files(s, profile, base_url, user, ugroups,
                                               pgroups, prefix, mode, res)
    bundle.deliveries = dict(res.counts)
    bundle.warnings += sorted(res.notes)
    if res.counts["direct"]:
        bundle.warnings.append(
            f"{res.counts['direct']} item(s) use the redirect template: the box "
            "fetches the panel's own file, so it keeps the original container, "
            "its subtitle tracks AND seeking - the .ts/.mkv in our URL is only "
            "cosmetic there, but the player must be 4097/5001/5002")

    if not bundle.files:
        bundle.warnings.append("nothing to write: no enabled playlist items match "
                               "this profile's content types and group filters")
    for kind, player in sorted(res.mkv_players):
        if player not in FFMPEG_PLAYERS[1:]:      # 5001 / 5002 read text subs
            label = {"vod": "VOD", "series": "series"}.get(kind, kind)
            bundle.warnings.append(
                f"{label} uses the .mkv container but player {player}: text "
                "subtitles need ServiceApp/exteplayer3 (5002) on the box")
    return bundle


async def _live_files(s, profile, base_url, user, ugroups, pgroups, prefix,
                      layout, mode, res: _Resolver) -> list[BouquetFile]:
    items = (await s.execute(select(LivePlaylist).where(LivePlaylist.enabled.is_(True))
                             .order_by(LivePlaylist.order, LivePlaylist.id))).scalars().all()
    items = [it for it in items if _visible(it.group_name, ugroups["live"], pgroups["live"])]
    if not items:
        return []
    out: list[BouquetFile] = []
    def add(w, it) -> None:
        d = res.for_item(it.ffmpeg_template_id, "live")
        w.service(d.player, it.id,
                  stream_url(base_url, "live", it.id, d.container, user, mode),
                  best_title(it.custom_name))

    if layout == "flat":
        w = _Writer(prefix, "live", "SPM - Live", profile.max_entries)
        for it in items:
            add(w, it)
        return w.done()
    # one bouquet per group (the channel list stays the shape of the panel)
    for group, rows in _by_group(items, "Live").items():
        w = _Writer(prefix, f"live_{group}", f"SPM - Live - {group}", profile.max_entries)
        for it in rows:
            add(w, it)
        out += w.done()
    return out


async def _vod_files(s, profile, base_url, user, ugroups, pgroups, prefix,
                     layout, mode, res: _Resolver) -> list[BouquetFile]:
    items = (await s.execute(select(VodPlaylist).where(VodPlaylist.enabled.is_(True))
                             .order_by(VodPlaylist.order, VodPlaylist.id))).scalars().all()
    items = [it for it in items if _visible(it.group_name, ugroups["vod"], pgroups["vod"])]
    if not items:
        return []
    names: dict[int, str] = {}
    wanted = [it.vod_source_id for it in items if it.vod_source_id]
    for batch in _chunked(wanted):
        for src in (await s.execute(select(VodSource).where(
                VodSource.id.in_(batch)))).scalars().all():
            names[src.id] = src.original_name

    def title(it) -> str:
        return best_title(it.custom_name, names.get(it.vod_source_id))

    out: list[BouquetFile] = []
    groups = {"VOD": items} if layout == "flat" else _by_group(items, "VOD")
    for group, rows in groups.items():
        rows = sorted(rows, key=lambda it: title(it).lower())
        slug = "vod" if layout == "flat" else f"vod_{group}"
        name = "SPM - VOD" if layout == "flat" else f"SPM - VOD - {group}"
        w = _Writer(prefix, slug, name, profile.max_entries)
        letter = None
        for it in rows:
            first = title(it)[:1].upper()
            if layout == "group_markers" and first != letter:
                letter = first
                w.marker(letter)
            d = res.for_item(it.ffmpeg_template_id, "vod")
            w.service(d.player, it.id,
                      stream_url(base_url, "vod", it.id, d.container, user, mode),
                      title(it))
        out += w.done()
    return out


async def _series_files(s, profile, base_url, user, ugroups, pgroups, prefix,
                        layout, mode, res: _Resolver) -> list[BouquetFile]:
    """Series are the reason `layout` exists.

    per_series      one bouquet per series - clean, but 400 series means 400
                    bouquets in the box's list;
    group_markers   one bouquet per group, a marker per series and per season
                    (the default: browsable, and the bouquet count stays sane);
    flat            everything in one bouquet, markers per series.
    """
    series = (await s.execute(select(SeriePlaylist).where(SeriePlaylist.enabled.is_(True))
                              .order_by(SeriePlaylist.order, SeriePlaylist.id))).scalars().all()
    series = [sp for sp in series
              if _visible(sp.group_name, ugroups["series"], pgroups["series"])]
    if not series:
        return []

    season_rows: list = []
    for batch in _chunked([sp.id for sp in series]):
        season_rows += (await s.execute(
            select(SeriePlaylistSeason.serie_playlist_id, SerieSeason.id,
                   SerieSeason.season_number)
            .join(SerieSeason, SerieSeason.id == SeriePlaylistSeason.serie_season_id)
            .where(SeriePlaylistSeason.serie_playlist_id.in_(batch),
                   SeriePlaylistSeason.enabled.is_(True))
            .order_by(SerieSeason.season_number))).all()
    eps_by_season: dict[int, list] = {}
    for batch in _chunked([r[1] for r in season_rows]):
        for season_id, ep_id, ep_num in (await s.execute(
                select(SerieEpisode.serie_season_id, SerieEpisode.id,
                       SerieEpisode.episode_number)
                .where(SerieEpisode.serie_season_id.in_(batch))
                .order_by(SerieEpisode.episode_number))).all():
            eps_by_season.setdefault(season_id, []).append((ep_id, ep_num))

    def episodes(sp):
        for _sp_id, season_id, season_number in [r for r in season_rows if r[0] == sp.id]:
            for ep_id, ep_num in eps_by_season.get(season_id, []):
                yield season_number, ep_num, ep_id

    def add(w, sp, with_series_marker: bool) -> None:
        show = best_title(sp.custom_name)
        if with_series_marker:
            w.marker(show)
        season = None
        for season_number, ep_num, ep_id in episodes(sp):
            if season_number != season:
                season = season_number
                w.marker(f"{show} - Season {season_number}")
            d = res.for_item(sp.ffmpeg_template_id, "series")
            w.service(d.player, ep_id,
                      stream_url(base_url, "episode", ep_id, d.container, user, mode),
                      f"{show} S{season_number:02d}E{ep_num:02d}")

    out: list[BouquetFile] = []
    if layout == "per_series":
        for sp in series:
            show = best_title(sp.custom_name)
            w = _Writer(prefix, f"series_{show}", f"SPM - {show}", profile.max_entries)
            add(w, sp, with_series_marker=False)
            out += w.done()
        return out
    if layout == "flat":
        w = _Writer(prefix, "series", "SPM - Series", profile.max_entries)
        for sp in series:
            add(w, sp, with_series_marker=True)
        return w.done()
    for group, rows in _by_group(series, "Series").items():
        w = _Writer(prefix, f"series_{group}", f"SPM - Series - {group}",
                    profile.max_entries)
        for sp in rows:
            add(w, sp, with_series_marker=True)
        out += w.done()
    return out


async def _local_files(s, profile, base_url, user, ugroups, pgroups, prefix,
                       mode, res: _Resolver) -> list[BouquetFile]:
    items = (await s.execute(select(LocalPlaylist).where(LocalPlaylist.enabled.is_(True))
                             .order_by(LocalPlaylist.order, LocalPlaylist.id))).scalars().all()
    items = [it for it in items
             if _visible(it.group_name, ugroups["local"], pgroups["local"])]
    if not items:
        return []
    files: dict[int, LocalFile] = {}
    for batch in _chunked([it.local_file_id for it in items if it.local_file_id]):
        for f in (await s.execute(select(LocalFile).where(
                LocalFile.id.in_(batch)))).scalars().all():
            files[f.id] = f
    w = _Writer(prefix, "local", "SPM - Local files", profile.max_entries)
    for it in items:
        lf = files.get(it.local_file_id)
        if not lf:
            continue
        d = res.for_item(it.ffmpeg_template_id, "local")
        # Progressive MP4 over HTTP is audio-only on Enigma2 (no picture), and
        # a live Matroska pipe is the same. Local items are always advertised
        # as MPEG-TS; /play/local/{id}.ts remuxes with h264_mp4toannexb even
        # when the template would otherwise FileResponse the original file.
        # Service type 1 cannot demux a remuxed AAC/H.264 TS from an MP4.
        player = d.player if d.player in FFMPEG_PLAYERS else "4097"
        w.service(player, it.id,
                  stream_url(base_url, "local", it.id, "ts", user,
                             mode, ext=".ts"),
                  best_title(it.custom_name, lf.filename))
    return w.done()


def _by_group(items: list, default: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for it in items:
        out.setdefault(it.group_name or default, []).append(it)
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


# --------------------------------------------------------------------------- #
#  bouquets.tv (the index the box reads)
# --------------------------------------------------------------------------- #
def merge_bouquets_tv(existing: str, filenames: list[str], prefix: str) -> str:
    """Put our bouquets into the receiver's `bouquets.tv` without touching the
    user's own.

    Idempotent by construction: every line referencing a `userbouquet.<prefix>_*`
    file is removed first (including files we no longer generate), then the
    current set is appended in order. Satellite bouquets, favourites and any
    other IPTV bouquet keep their position.
    """
    header = "#NAME Bouquets (TV)"
    ours = re.compile(rf'FROM BOUQUET "userbouquet\.{re.escape(slugify(prefix))}_[^"]*"')
    kept = [ln for ln in (existing or "").splitlines()
            if ln.strip() and not ln.startswith("#NAME") and not ours.search(ln)]
    lines = [header, *kept, *[_BOUQUET_LINE.format(file=f) for f in filenames]]
    return "\n".join(lines) + "\n"


BOUQUET_ADD_FILE = "bouquets.spm.add"


def bouquets_add_file(filenames: list[str]) -> str:
    """The `#SERVICE ... FROM BOUQUET ...` lines for our files, as shipped in the
    tarball. The installer merges them into the receiver's own `bouquets.tv`
    (we never ship that file itself - it lists the user's satellite bouquets,
    which only the box knows)."""
    return "\n".join(_BOUQUET_LINE.format(file=f) for f in filenames) + "\n"


def install_script(base_url: str, token: str, prefix: str = "spm") -> str:
    """A self-contained installer the box can run (pull delivery).

    Deliberately plain BusyBox shell + wget: that is all an OpenPLi image is
    guaranteed to have. It downloads the tarball, backs `bouquets.tv` up,
    merges our lines into it (dropping the ones from a previous run, keeping
    every other bouquet), copies the files into /etc/enigma2 and asks OpenWebif
    to reload the service list.
    """
    pfx = slugify(prefix)
    url = f"{base_url.rstrip('/')}/enigma2/{token}/bouquets.tar.gz"
    return f"""#!/bin/sh
# Stalker Proxy Manager - install/refresh the SPM bouquets on this receiver.
# Run it ON THE BOX:
#   wget -qO- {base_url.rstrip('/')}/enigma2/{token}/install.sh | sh
# (or from cron, to keep the bouquets in sync)
set -e
DIR={E2_DIR}
TMP=/tmp/spm-bouquets.$$
mkdir -p "$TMP"
echo "* downloading bouquets"
wget -q -O "$TMP/bouquets.tar.gz" "{url}"
tar xzf "$TMP/bouquets.tar.gz" -C "$TMP"
echo "* backing up $DIR/{BOUQUETS_TV}"
cp "$DIR/{BOUQUETS_TV}" "$DIR/{BOUQUETS_TV}.spm-backup" 2>/dev/null || true
echo "* removing bouquets from a previous run"
rm -f "$DIR"/userbouquet.{pfx}_*.tv
cp "$TMP"/userbouquet.*.tv "$DIR"/
echo "* merging {BOUQUETS_TV}"
grep -v 'userbouquet\.{pfx}_' "$DIR/{BOUQUETS_TV}" > "$TMP/{BOUQUETS_TV}" 2>/dev/null || \
  echo '#NAME Bouquets (TV)' > "$TMP/{BOUQUETS_TV}"
cat "$TMP/{BOUQUET_ADD_FILE}" >> "$TMP/{BOUQUETS_TV}"
cp "$TMP/{BOUQUETS_TV}" "$DIR/{BOUQUETS_TV}"
rm -rf "$TMP"
echo "* reloading the service list"
wget -qO- "http://127.0.0.1/api/servicelistreload?mode=2" >/dev/null 2>&1 || \
  echo "  (OpenWebif not reachable - reload the bouquets from the box menu)"
echo "done - {BOUQUETS_TV} backup: $DIR/{BOUQUETS_TV}.spm-backup"
"""


def tarball_bytes(bundle: Bundle, prefix: str = "spm") -> bytes:
    """The bundle as a .tar.gz the receiver can unpack.

    Contents: every `userbouquet.*.tv`, plus `bouquets.spm.add` (the index
    lines the installer merges into the box's own `bouquets.tv`) and a copy of
    the installer for people who unpack it by hand.
    """
    import io
    import tarfile
    import time

    names = [f.name for f in bundle.files]
    payload = {f.name: f.text for f in bundle.files}
    payload[BOUQUET_ADD_FILE] = bouquets_add_file(names)
    buf = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in payload.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = now
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
