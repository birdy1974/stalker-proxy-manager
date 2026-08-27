# Stalker Proxy Manager — Phase 1: Requirements Analysis

Date: 2026-08-27
Status: **Awaiting your decisions on the open questions (§7) before Phase 2 starts.**

---

## 1. Verified technical findings (input for the design)

These were tested from this dev environment during analysis:

| # | Finding | Impact on design |
|---|---------|------------------|
| 1 | `http://line.cloud-ott.net:80/c/` accepts TCP but **drops every HTTP request from this sandbox** (cloud-IP blocking, extremely common for Stalker portals). Even MAG User-Agent, cookies and all known paths (`/c/`, `/client/`, `/stalker_portal/c/`, `/server/load.php`, root `/portal.php`) return *empty reply*. | The app must include a **built-in mock/simulated Stalker portal** for development and CI testing. Real portal testing will happen on your LAN. Portal resolution must never assume the first path works. |
| 2 | STB-Proxy's portal resolution parses `xpcom.common.js` per known path and evaluates `portal_protocol` / `portal_ip` / `portal_path` / `ajax_loader` indices to find `portal.php`. Works for many portals but breaks on obfuscated variants. | We will implement: (a) path probing (`/c/`, `/client/`, `/c_/`, `/stalker_portal/c/`, `/stalker_portal/c_/`, `/server/load.php`, `/portal.php`, root), (b) parse `xpcom.common.js` when it exists, (c) direct `portal.php` handshake trial per path as fallback, (d) user-provided explicit path override, (e) **first working path is cached per portal** so repeated calls are cheap. Resolution result is stored in DB. |
| 3 | Stalker API quirks (confirmed against STB-Proxy `stb.py` + your notes): token via `handshake`, `Authorization: Bearer`, `mac` cookie; VOD *and* Series both use `type=vod` for `get_ordered_list`; series items have **empty `cmd`** and ids like `56359:56359`; `is_series` is numeric (`0`/`1`) — string comparison bugs; VOD categories exist at `type=vod&action=get_categories`, series categories likewise but not all portals have them (then fetch unfiltered); pagination ~14 items/page → huge catalogs need **chunked background fetching with progress**; `sortby=added`, `not_ended=0` for series. | Portal adapter implements lenient parsing (loose types, multiple key spellings), per-genre fetch with configurable page budget, resumable background jobs with progress reporting. VOD/series/live detection uses `is_series`, `cmd` prefix (`ffmpeg`, `/media/`), `.mpg/.avi/.mkv` file hints, and VOD container presence — with **loose filters** as requested. |
| 4 | tv-logos GitHub repo: full tree API works — **10,777 PNG files, 3.4 MB JSON, not truncated**. `countries/netherlands/npo1-nl.png` exists (spec example valid). Per-country directory listing (155 files for NL) also works. | Logo matcher = **one cached fetch of the repo tree** (cached in DB/volume, refreshed weekly) + fuzzy filename matching per selected country. No brittle guesses/HEAD-probing. Output URL format `https://github.com/tv-logo/tv-logos/blob/main/<path>?raw=true` as you require (equivalent `raw.githubusercontent.com` also offered). |
| 5 | `raw.githubusercontent.com` and `xmltvepg.nl` HEAD are blocked/slow from this sandbox as well. | All external lookups run **server-side with caching**, never browser-side. |
| 6 | DS918+ = Intel Apollo Lake J3455: VAAPI via `/dev/dri/renderD128` is the reliable path (your example command). QSV (`h264_qsv`/`hevc_qsv`) is supported by current ffmpeg on this CPU and offered as an alternative. HEVC 8-bit encode via `hevc_vaapi` works on J3455. Container needs `devices: /dev/dri` + correct group (usually `video`, via `group_add` or PUID/PGID). | Compose maps `/dev/dri`, app auto-detects render nodes at startup and logs what it found (stdout → Portainer). Fallback to software (`libx264`) selectable per template. |

---

## 2. Inconsistencies & problems found (spec vs DB vs GUI)

### 2.1 Database ↔ functionality

| # | Problem | Why it hurts | Proposed fix |
|---|---------|--------------|--------------|
| D1 | `portals` table holds `portal-org-live-genres`, `portal-org-vod-genres`, `portal-org-serie-genres` **and** separate genre tables also exist | Two sources of truth for genres; the portal-table copies will go stale | Remove the three `portal-org-*` columns. The genre tables store **all** fetched genres with `*-enabled-genre` flags (they already have them). |
| D2 | Genre tables have **no portal-side genre id** | You cannot fetch channels "of enabled genres" — `get_ordered_list` needs the portal's numeric genre id | Add `*-genre-portal-id` (the id as returned by the portal) to all three genre tables. |
| D3 | The placeholder *"all additional original information from portal, like channel number, channel name, epg-id, logo"* sits on the **genre** tables | That information belongs to channels/VOD (sources), not genres. Genres only have id/title | Move that information into the **source** tables. Genre tables keep only portal-id, name, enabled flag, (adult flag), and cached item count. |
| D4 | `live-source` is missing the fields actually needed to play a stream: **portal channel id and `cmd`** (needed for `create_link`), number, portal logo, tvg/epg-id | Without `cmd`/id the proxy cannot request a stream URL at playback time | Add `live-portal-id` (channel id), `live-cmd`, `live-number`, `live-logo-original`, `live-epg-original`. Same for vod-source (`vod-portal-id`, `vod-cmd`, `vod-poster`, `vod-year`, `vod-description`…) and serie-source. |
| D5 | **No season/episode tables anywhere** | Series are containers (empty `cmd`). Xtream `get_series_info` and the M3U both need episode stream references. The spec's own workflow says seasons are enabled, then episodes fetched | Add `serie-season` (per serie-source: season number, enabled) and `serie-episode` (season, episode nr, name/overview/still, portal id/cmd, enabled). See decision Q6 about depth of control. |
| D6 | `*-fallback-channels` = comma-separated list of source ids, "fuzzy-matched" | Ordering inside a CSV is fragile; can't be indexed/joined; "replace fallback portal everywhere" and "delete portal cleanup" become string surgery; fuzzy matching is a **UI suggestion tool**, it should not define stored data | Replace with child tables: `live-playlist-source (id, live-playlist-ID, live-source-ID, priority)` — first row = primary. Same pattern for vod/series if fallback is kept there (see Q1). Portal-delete cleanup + "replace portal everywhere" become single UPDATEs. |
| D7 | `live-playlist` has **no direct link to its source** (vod/serie-playlist do) | Inconsistent model; primary source implied only by fuzzy CSV | Solved by D6 child table (priority=1 row is the primary source). |
| D8 | **`serie-playlist.serie-source-ID` links to `serie-genres` table** — wrong FK | Broken reference | Link to `serie-source`. |
| D9 | `local-files` (genres group), `local-source` (source group), `local-playlist` all mix directory/file levels; GUI text says "enable *series*" in the local tab (copy-paste) | Unclear who enables what; files can't be toggled individually as the local *builder* requires | Clean 3-level model: `local-source` = directory (enabled → scanned), `local-files` = scanned video file per directory, `local-playlist` links to `local-files` and enables per file + order + template. (Relocation, not new data.) |
| D10 | VOD/series **contradiction**: functional text says "The selected vod/series will only have a transcoding template", but `vod-playlist`/`serie-playlist` tables contain `vod-logo`, `vod-fallback-channels`, `serie-logo`, `serie-fallback-channels` | Design can't be finalized until decided | **→ Question Q1.** |
| D11 | `users.user-groups` is a single column but groups exist per type (live / vod / serie / local); same-named groups across types must be separable | Can't express "user may watch NL live group but not NL VOD group" | Store as JSON map `{live:[...], vod:[...], series:[...], local:[...]}` or relation table `user-groups(user-ID, group-type, group-name)`. Also propose adding `user-max-connections` (default 1, Xtream-style) and `user-enabled`. |
| D12 | `ffmpeg` table columns (resolution, ratio, video codec, audio codec, bitrate) can't express the features you require: hw accel type (none/QSV/VAAPI), device path, fps, GOP, profile/level, maxrate/bufsize, audio bitrate/channels/sample-rate, output format, input/reconnect flags, full custom command, **2-way sync between fields and command text** | Your own reference command needs ~20 parameters; 5 columns can't represent it | Store `ffmpeg-options` (JSON, the dropdown state) **+** `ffmpeg-command` (full text) **+** `ffmpeg-command-source` (`fields`/`manual` — which side was edited last). UI keeps both in sync exactly as you specified. |
| D13 | MAC table: no ordering, no status detail | The global option "try all MACs of a portal first" needs an order; STB-Proxy moves failing MACs to the end; spec wants per-MAC online/offline + why | Add `mac-order` (drag-sortable), `mac-status` (`unknown/online/offline/unauthorized/expired/error`), `mac-last-checked`. Occupancy stays a **runtime** structure (active-smanager), not schema. |
| D14 | Missing table: **logs/messages** | Dashboard "messages pane with filter (info/warning/error)" needs persistence across restarts | Add `logs(id, ts, level, module, message)` with retention setting + stdout mirroring (Portainer). |
| D15 | Missing table: **active streams / runtime registry** | Dashboard active-streams pane, MAC-occupancy tracking, kill-stream buttons | In-memory registry mirrored to `active-streams(id, playlist-type, item, user, portal/mac, template, ffmpeg pid, started, bytes)` for visibility/survive-restart cleanup (rows purged at boot). |
| D16 | Missing: EPG source management + caches | Spec wants multiple external EPG sources, matching, and Xtream/xmltv output | Add `epg-sources(id, url, enabled, last-fetch, status)`; parsed cache on volume; per-channel match stored in `live-playlist.live-epg`. App serves merged/filtered `/epg.xml` + Xtream `xmltv.php` (external sources can't be referenced directly by Xtream clients). |
| D17 | `settings` table described as rows with `<all persistent settings>` | Vague | Single-row-per-key table(`key`, `value JSON`): playlist URL format(s), fallback strategy (MACs-first vs portal-first — your global option), TMDB API key, EPG refresh schedule, log retention, portal request timeout/UA, time zone, output base URL override. |
| D18 | Genre tables lack per-genre item counts | Dashboard wants "enabled X of Y available" | Cache `*-genre-item-count` (and total per portal/type) after each fetch; exact totals come from portal `total_items`. |

### 2.2 GUI ↔ functionality

| # | Problem | Fix |
|---|---------|-----|
| G1 | "Add custom channel": fuzzy-match against "**live-playlist** custom channel names" is circular — the list is empty for the first channel (your spec even patches around it with "if empty show all enabled channels") | Always fuzzy-match against **enabled `live-source` channels (all portals)**, case-insensitive. The same match list doubles as the fallback-candidate list — this naturally finds the same channel on multiple portals. |
| G2 | Portal popup subtabs (channels/vod/series per portal) duplicate the Input Source tabs and contradict the workflow (inside the popup only *genres* are fetched; sources arrive via background fetch after Save) | Popup subtabs show the **DB-stored** items of that portal (filter + enable toggles, no fetching), with a "fetch in progress" state. Source fetching happens after Save as a background job. |
| G3 | Local tab text: "Toggle directories selection to enable **series** in final output" (copy-paste) — and the builder enables *files* | Two explicit levels: directory enable = include in scan; file enable = include in output. Texts fixed in mockup. |
| G4 | Export/import appears in 5 places (portals tab, 3 builders, ffmpeg, settings) without defined scope/format | One versioned JSON format; each section can export/import its slice; global = full backup. Import modes: merge (default) / replace; duplicate-name strategy: rename with suffix. |
| G5 | Web player says "play the streams (HLS)" but Stalker live streams are usually **MPEG-TS over HTTP** which browsers can't play natively and hls.js won't handle | Player auto-detects: HLS → hls.js; HLS-in-TS→mpegts.js (pure JS remux, works for the common Stalker `ffmpeg …/hls/…` links too); plain TS → mpegts.js with `-c copy` preview pipe as fallback. Preview of *final playlist* entries goes through your template anyway (already playable). Note: browsers block `http://` video if GUI is served via `https://` — on your LAN everything is `http://`, so fine. |
| G6 | Drag&drop ordering vs column sorting conflict | Reordering (drag) is enabled only when the list is sorted by the Order column — otherwise the drop target is ambiguous. A bulk "renumber from 1" action is provided. |
| G7 | Xtream output needs more than a flag: `player_api.php`, `get.php`, `xmltv.php`, `/live/u/p/id.m3u8`, `/movie/…`, `/series/…`, categories, VOD/series metadata incl. episodes | Backend implements the full minimal Xtream Codes API subset; episodes come from D5 tables; per-user group filtering from D11. |
| G8 | Case-insensitive filtering everywhere | DB queries use `ILIKE`/case-folded collation; UI comparisons normalized. Explicitly applied to: filters, fuzzy match, EPG/logo matching. |
| G9 | Dashboard counters need fetch totals (D18) and a definition of "active since" for streams (D15) | Covered by schema fixes. |
| G10 | "Resolve URL" button behavior not defined (a portal can have multiple working paths) | Resolves via §1-2 strategy; shows candidates found; user may pin one. Result cached per portal to avoid re-probing on every request. |

### 2.3 Things that were ambiguous but decided-by-reasoning (confirm if you disagree)

- **Single port 8880** serves GUI, M3U, Xtream API, streams, XMLTV and player (like STB-Proxy's single port). Output URLs are built relative to the incoming request host, with a settings override (`output-base-url`) for reverse proxies/NAT.
- Non-Stalker input (pure M3U lists) is out of scope for v1 — the spec centers on Stalker portals; the architecture leaves room to add M3U as an additional source type later.
- HDHomeRun emulation (STB-Proxy feature for Plex) is **excluded v1** — not in your spec. Easy to add later.
- Timer/scheduling: EPG refresh daily by default; logo tree refresh weekly; both configurable.

---

## 3. Proposed revised database structure (target for Phase 2, pending Q-answers)

Group naming cleaned up; `BIGINT` surrogate keys `id`; all `*-enabled` = boolean; all name fields case-insensitively searchable; composite indexes on `(portal_id, enabled)` and on every FK; Postgres `citext` or functional indexes for name search.

**Portals**
- `portals(id, name, base-url, resolved-portal-url, resolved-path, enabled, proxy-url?, fallback-strategy-override?, created, updated)`
- `mac-addresses(id, portal-id→portals, mac, password?, expire-date, status, online bool, order, last-checked, fail-count)`

**Genres** (all fetched genres, enabled subset flagged)
- `live-genres(id, portal-id→, genre-portal-id, name, enabled, adult, item-count)`
- `vod-genres(id, portal-id→, genre-portal-id, name, enabled, item-count)`
- `serie-genres(id, portal-id→, genre-portal-id, name, enabled, item-count)`

**Sources** (fetched items of enabled genres)
- `live-sources(id, portal-id→, live-genre-id→, portal-channel-id, number, original-name, cmd, logo-original, epg-original, enabled, tv-archive?)`
- `vod-sources(id, portal-id→, vod-genre-id→, portal-item-id, cmd, original-name, poster, year, description, genre, director, actors, rating, duration, added, enabled)`
- `serie-sources(id, portal-id→, serie-genre-id→, portal-item-id, original-name, poster, year, description, rating, enabled)`
- `serie-seasons(id, serie-source-id→, season-number, name?, enabled)`
- `serie-episodes(id, serie-season-id→, episode-number, name, overview?, still?, portal-item-id, cmd, duration?, enabled)`
- `local-sources(id, directory, enabled, recursive bool, last-scan)`
- `local-files(id, local-source-id→, relative-path, filename, size, duration?, mtime, enabled)`

**Playlist** (final output items)
- `live-playlist(id, custom-name, group-name, epg-id?, logo?, ffmpeg-id→, enabled, order, number?)`
- `live-playlist-sources(id, live-playlist-id→, live-source-id→, priority)` ← primary = min(priority); *replaces the fuzzy CSV*
- `vod-playlist(id, vod-source-id→, custom-name, group-name, ffmpeg-id→, logo?, enabled, order, tmdb-id?, overview, poster, rating, year, …)` (+ `vod-fallback(bool)` design per Q1: `vod-playlist-sources(id, vod-playlist-id→, vod-source-id→, priority)`)
- `serie-playlist(id, serie-source-id→, custom-name, group-name, ffmpeg-id→, logo?, enabled, order, tmdb-id?, overview, poster, rating, year, …)` (+ optional `serie-playlist-sources` per Q1)
- `serie-playlist-seasons(id, serie-playlist-id→, serie-season-id→, enabled)`
- `local-playlist(id, local-file-id→, custom-name?, group-name default 'vod-local', ffmpeg-id→, enabled, order)`

**Settings & runtime**
- `ffmpeg-templates(id, name, enabled, resolution, aspect, hw-accel ('none'|'vaapi'|'qsv'), device?, video-codec, video-bitrate, maxrate, bufsize, fps, gop, profile, level, audio-codec, audio-bitrate, audio-channels, audio-rate, output-format, input-flags-json, options-json, command, command-source('fields'|'manual'))` → **the two-way-sync lives on `options-json` + `command`**
- `users(id, name, password, m3u-enabled bool, xtream-enabled bool, expire-date?, max-connections, enabled, groups-json {live:[],vod:[],series:[],local:[]}, last-active?)`
- `epg-sources(id, url, enabled, last-fetch, status, channel-count)`
- `logs(id, ts, level, module, message)`
- `active-streams(id, type, item-id, item-name, user-id?, portal-name?, mac? , template?, pid?, started, bytes)` *runtime mirror, purged at boot*
- `settings(key PK, value JSON)`

---

## 4. Architecture (proposal, finalized with Q3/Q4 answers)

```
┌───────────────────────────── docker compose: stalker-proxy-manager ─────────────────────────────┐
│  app (FastAPI, port 8880)                postgres:16-alpine (internal only, no host port)        │
│  ├─ Web GUI (bootstrap, no build step)   └─ volume: pgdata                                      │
│  ├─ Portal adapter (multi-path resolve, token cache, lenient parsing, mock-portal for dev)      │
│  ├─ Fetch jobs (genres → sources → seasons/episodes, chunked, resumable, progress in DB/logs)   │
│  ├─ Stream manager (MAC occupancy locks, fallback chain, ffmpeg supervisor, kill/kill-all)      │
│  ├─ Outputs: /playlist.m3u(8)?user=…, Xtream API, /epg.xml, /play/…(ts|hls), /stream preview    │
│  ├─ Matcher (fuzzy names: EPG ids, logos via cached tv-logos tree, TMDB optional with key)      │
│  └─ /dev/dri mapped (VAAPI), auto-detect renderD128/D129; logs → stdout + logs table            │
│  volumes: config, epg-cache, logo-cache, media (your local video dirs, read-only)               │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Phase plan:
- **Phase 2**: core engine working end-to-end against the **mock portal** (resolve→handshake→fetch→playlist→stream with VAAPI templates + fallback), full DB schema created, complete clickable web GUI with live data where core exists and clearly-marked demo data where Phase 3 features (EPG matcher, TMDB, logos) will land.
- **Phase 3**: EPG ingestion/matching, logo matcher, TMDB popups, import/export, users/Xtream polish, hardening, GitHub build workflow.

---

## 5. Performance notes (large catalogs)

- Chunked background fetching with page-budget per genre (default 30 pages) + visible progress + "fetch more" action; `total_items` from portal recorded.
- Indexes on all FKs and on `lower(name)`; pagination always server-side.
- Source tables carry their own `enabled` so the Input Source tabs are pure DB reads (workflow steps 8/11 confirmed).
- Streams: one ffmpeg process per client (or shared pipeline later — see Q2 note); zomb­ie reaper; idle-timeout disconnects.
- EPG/logo caches on disk, never re-downloaded per request.

---

## 6. Risks / call-outs

1. **Real portal couldn't be tested from this environment** (§1-1): handshake/fetching is implemented against the documented STB-Proxy behavior + your notes + mock portal; first real-portal verification happens on your NAS. The resolver logs every probe attempt so we can adapt quickly (send me the log lines if a portal fails).
2. **DS918+ Container Manager UI** historically can't map `/dev/dri`; use SSH (`docker-compose up -d`) or Container Manager "Project" (which does support devices). Documented in README.
3. **Mixed content**: keep everything on `http://<nas-ip>:8880` (no TLS in LAN) so the player can open `http://` streams.
4. **MAC concurrency**: portals typically allow 1 stream per MAC; exceeding it can get the MAC banned. Default = never reuse a busy MAC, always fall back — configurable per portal later.

---

## 7. Open questions — see the selection UI (mirrors what I'm asking there)

- **Q1 VOD/series scope**: (A) only transcoding template (drop logo+fallback from vod/serie-playlist) · (B) keep fallback + logo override, same as live · (C) fallback yes, logo automatic only (no manual override)
- **Q2 Untranscoded delivery**: (A) ffmpeg `-c copy` pipe — uniform, exact MAC-occupancy, tiny CPU *(recommended)* · (B) HTTP 302 redirect — zero CPU, no occupancy control · (C) raw byte proxy — lowest CPU, occupancy approximated
- **Q3 Database**: (A) Postgres 16, separate container *(as proposed, recommended)* · (B) SQLite single-container — simplest, less RAM · (C) pluggable both (extra work)
- **Q4 GUI/backend stack**: (A) FastAPI + Bootstrap/vanilla JS, no build step *(recommended for DS918+)* · (B) Next.js full stack · (C) FastAPI + pre-built Vue SPA
- **Q5 GUI authentication**: (A) admin login *(recommended)* · (B) none (LAN only)
- **Q6 Series depth**: (A) season-level enable only *(as spec, recommended)* · (B) also per-episode enable
