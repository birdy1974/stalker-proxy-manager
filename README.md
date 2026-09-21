# Stalker Proxy Manager

Turn MAC-based **Stalker/Ministra portal** accounts into clean, stable **M3U playlists and Xtream Codes API** output — with an ordered fallback chain across portals & MAC addresses per channel, optional **Intel Quick Sync hardware transcoding** (tuned for the Synology DS918+), a persistent config database, and a modern single-port web GUI.

> Phase 3 delivered: Phase-2 engine + GUI, plus real EPG ingestion/matching with merged `/xmltv.php`, tv-logos auto-matching, TMDB metadata popups, and final Xtream output polish.

---

## Quick start (Docker Compose, recommended)

```bash
cp .env.example .env          # set SPM_ADMIN_PASSWORD
mkdir -p media                # optional: drop local video files here
docker compose up -d --build
```

- GUI: **http://<host>:8880** (login admin / your password)
- Postgres 16 runs in its own container next to the app (state in named volumes).
- Quick Sync: `/dev/dri` is passed through by default (DS918+).
- Optional TMDB metadata: set `SPM_TMDB_API_KEY` in `.env` before the first start.
  `docker-compose.yml` passes it to the app to initialize **Settings → TMDB API key**.
  Use your TMDB **API key (v3)**, not the API Read Access Token. Existing saved
  settings, including an empty key, are never overwritten on restart; for an
  existing installation, change the key in the GUI. Keep your real key out of Git.
- Binding `./media` from the host? Set `PUID`/`PGID` in `.env` to that folder's owner, otherwise the app cannot list it — see [Permissions](#permissions-running-as-your-own-user-puid--pgid).

Pre-built image (built by GitHub Actions on every release):

```bash
docker run -d --name stalker-proxy-manager \
  -p 8880:8880 \
  --device /dev/dri:/dev/dri \
  -e SPM_ADMIN_PASSWORD='change-me' \
  -v spm-data:/config -v spm-media:/media \
  ghcr.io/birdy1974/stalker-proxy-manager:latest
```
(The plain `docker run` form uses the embedded **SQLite** database — fine for small setups. Compose + Postgres is the reference deployment.)

Named volumes are owned by the image user, so no `PUID`/`PGID` is needed here — mount a host directory instead (`-v /volume1/video:/media`) and you must add `-e PUID=$(stat -c %u /volume1/video) -e PGID=$(stat -c %g /volume1/video)`.

---

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SPM_DATA_DIR` | `/config` | config DB + state volume |
| `SPM_MEDIA_ROOT` | `/media` | local video files mount |
| `SPM_DATABASE_URL` | sqlite | `postgresql+asyncpg://user:pass@host:5432/dbname` to switch to Postgres; if that Postgres cluster exists but `dbname` does not yet, the app creates it automatically when the credentials allow `CREATE DATABASE` |
| `SPM_ADMIN_USERNAME` / `SPM_ADMIN_PASSWORD` | `admin` / *(required)* | GUI login |
| `SPM_TMDB_API_KEY` | empty | Initial TMDB API key (v3) when the database setting is missing; saved GUI settings take precedence, including an explicitly empty value |
| `SPM_VAAPI_DEVICE` | `/dev/dri/renderD128` | Intel Quick Sync render node |
| `SPM_PROBE_TIMEOUT` | `30` | seconds a stream probe may take before reporting a timeout (network streams use the MAG player identity) |
| `SPM_FFPROBE_BIN` | companion to FFmpeg | Optional ffprobe executable override for detailed technical probes; included by the Docker image’s FFmpeg package |
| `SPM_PORTAL_WARM_INTERVAL` | `600` | seconds between background pre-authentication passes for resolved portal/MAC sessions |
| `SPM_ROUTE_AFFINITY_TTL` | `1800` | seconds to prefer the source/MAC that most recently produced stream bytes |
| `SPM_STREAM_START_BUDGET` | `75` | seconds the fallback engine may spend looking for a first byte before it gives up (0 = no cap). Covers `candidates × SPM_STREAM_START_TIMEOUT × passes`; the output guard waits for this budget plus `SPM_START_BUDGET_SLACK` before answering 502, so the engine is never cut off mid-chain |
| `SPM_START_BUDGET_SLACK` | `10` | seconds added to the engine's budget before the first-chunk guard declares the pipe dead (portal round trips and killing a stalled ffmpeg happen outside the start windows) |
| `SPM_MAC_PROBE_READ_TIMEOUT` | `10` | seconds the *Test* button / `POST /api/portals/{id}/macs/{mac}/probe` waits for the panel's link to deliver its first bytes |
| `SPM_STREAM_START_TIMEOUT` | `12` | seconds one MAC/source attempt may stay silent before the chain moves on |
| `SPM_FIRST_CHUNK_TIMEOUT` | `25` | floor for the first-chunk guard (the `produced no data -> 502` line). A real stream open extends it to the engine's budget + `SPM_START_BUDGET_SLACK` |
| `SPM_REDIRECT_LEASE_S` | `180` | how long a MAC stays "probably still watching" after a 302 (the player is on the panel's CDN and we cannot see it stop) |
| `SPM_ZAP_RETRY` / `SPM_ZAP_RETRY_DELAY` | `1` / `2.5` | one delayed second pass for a zap whose panel slot is still held |
| `SPM_BUSY_WAIT_S` / `SPM_BUSY_POLL_S` | `3` / `0.25` | how long a start waits for a MAC our own bookkeeping calls busy (and how often it looks) before moving to the next candidate. `0` restores "refuse immediately" |
| `SPM_BUSY_BACKOFF` | `0.5,1.0,2.0` | waits between re-asking the *same* MAC while the panel answers `limit` / `account_is_in_use` / 456 (the zap overlap). Empty disables the ladder |
| `SPM_FAILURE_DEMOTE_S` | `120` | seconds a (source, MAC) that just failed is tried *last* on the retry pass, outranking `SPM_ROUTE_AFFINITY_TTL`. `0` disables |
| `SPM_LINK_CACHE_S` | `90` | seconds a resolved live link may be replayed for the next 302 (zap-back costs no `create_link`). `0` disables; `SPM_LINK_CACHE_KINDS` picks the kinds (default `live`) |
| `SPM_FFMPEG_USE_STORED_LINK` | `1` | `0` restores "the ffmpeg path always asks for a link", even for a channel whose flags say its link is permanent |
| `SPM_STREAMS_PER_MAC` | `1` | default concurrent streams per MAC for portals whose row says nothing (the GUI setting is per portal) |
| `SPM_TIMING_HISTORY` | `200` | starts kept in memory for the dashboard's timing view (phase timings per play, failures by reason, per-portal latency) |
| `SPM_JANITOR_MINUTES` | `60` | how often the janitor drops expired in-memory state (route affinity/breaker tables, redirect handoffs, probe verdicts) and trims the log table. `0` disables. Everything else in SPM is bounded by configuration; these three grow with history |
| `SPM_JOB_HISTORY` | `100` | finished fetch jobs kept visible in the GUI |
| `SPM_PLAYBACK_PACE` | `1` | while a stream is live on a portal, background jobs (catalogue sync, MAC health sweep, the per-channel EPG fallback) slow down instead of competing for the same panel budget. `0` disables |
| `SPM_PLAYBACK_PACE_S` | `0.4` | how long each background request waits while a play is running on that portal. Nothing is cancelled or starved - the job finishes a little later, the play gets the panel |
| `SPM_LINK_PROBE_TTL` | `20` | how long a link-liveness verdict is trusted (0 disables). The probe is a TLS + RTT to the **media host** on the path the player waits for; a zap back replays the same URL, so without this even the cheapest zap paid it again. Dead verdicts expire much sooner (`SPM_LINK_PROBE_DEAD_TTL`, 3 s) because a link can be re-minted |
| `SPM_BUSY_WAIT_S` | `7.0` | how long a start waits for a MAC that our own bookkeeping (or the panel's) says is busy. A real panel kept its slot counted for ~6.5 s after the previous connection died, so a 3 s wait was spent *and* the user still got a channel error |
| `SPM_STREAM_STALL_TIMEOUT` | `25` | seconds without a byte before a pipe counts as finished |
| `SPM_STREAM_STALL_TIMEOUT_LIVE` | `10` | the same for a *restartable* live stream: silence after it has been flowing means the source dropped, and the response continues by re-resolving, so 25 s of tolerance only freezes the picture for 25 s |
| `SPM_RATE_LIMIT_COOLDOWN` | `30` | when a portal answers HTTP 429, every request to that **host** stops for this long (`Retry-After` from the panel wins when it sent one). A rate limit is per portal, not per MAC: walking into it with the next MAC in the chain, the next EPG page or a health probe is how an IP gets banned - and the ban takes every MAC down with it |
| `SPM_RATE_LIMIT_COOLDOWN_MAX` | `600` | ceiling for the pause, so a hostile `Retry-After: 99999` cannot park a portal for a day |
| `SPM_STREAM_START_TIMEOUT_REST` | `5` | first-byte window for every candidate *after* the first. The first one gets the full `SPM_STREAM_START_TIMEOUT` (a panel may legitimately be slow to open the media path); after that a silent source is usually dead, not slow, and waiting the full window for each MAC is how a two-MAC chain became 24 s of black screen. Measured: a source that produces bytes does so in ~550 ms |
| `SPM_LINGER_S` | `8` | how long a live pipe is *held* after its client disappears, so a zap back attaches to the running stream instead of paying `create_link` + ffmpeg start + first byte again. `0` disables (the pipe is killed on disconnect, as before) |
| `SPM_LINGER_BUFFER_KB` | `2048` | bytes of the parked stream kept for the returning client (handed over first, so the player sees no gap) |
| `SPM_LINGER_KINDS` | `live` | kinds that may be parked; a VOD that ended is finished, not held |
| `SPM_MIDSTREAM_RESTARTS` | `3` | how many times a live stream that died mid-play is restarted inside the same client response (fresh `create_link`, MAC rotation); `0` disables. A VOD that finished is never restarted |
| `SPM_MIDSTREAM_RESTART_DELAY` | `1.5` | seconds between those restarts |
| `SPM_MIDSTREAM_RESTART_KINDS` | `live` | kinds allowed to restart after a mid-stream end |
| `SPM_PORTAL_KEEPALIVE_S` | `90` | how long a pooled portal session keeps its HTTP connection while idle. httpx's own default (5 s) meant every `create_link` after a quiet moment paid a fresh TCP+TLS handshake — pure zap latency the portal never asked for. `0` = never expire |
| `SPM_PORTAL_MAX_CONNECTIONS` | `32` | connections per portal session (one session = one portal + MAC), shared by playback and background fetches |
| `SPM_PREFER_FREE_MAC` | `1` | a start takes the first MAC nothing holds (walking the chain) instead of taking back the MAC this user just used. `0` = prefer the MAC that just played; GUI setting **Zap takes a free MAC first** overrides this |
| `SPM_SOURCE_BREAKER_FAILURES` / `SPM_SOURCE_BREAKER_COOLDOWN` | `2` / `45` | source-specific failures before temporarily skipping it, and seconds before a half-open retry |
| `SPM_MEDIA_CMD_REPAIR` | `1` | `0` stops the `/media/<id>` → `/media/file_<id>` retry — for a panel that rate-limits every extra request (see *Fallback engine semantics*) |
| `SPM_REDIRECT_VALIDATE` | `1` | redirect mode only: `0` hands the 302 out **unprobed**. The validator probes each candidate link (HEAD → ranged GET → plain GET) and skips a link it *proved* dead; set `0` to compare behaviour when a channel only fails in redirect mode |
| `SPM_REDIRECT_VALIDATE_TIMEOUT` | `2.0` | seconds per probe request (the ladder is capped at twice that per link, and running out of budget counts as alive) |
| `SPM_REOPEN_DEMOTE` / `SPM_REOPEN_DEMOTE_WINDOW` | `1` / `300` | redirect mode only: when the same channel is re-asked inside the window, the source/MAC the last 302 pointed at moves to the back of the chain — a reopen usually means "that link did not play" |
| `SPM_MOCK_PORTAL` | `0` | `1` boots a built-in demo portal (test data, busy-MAC emulation) |
| `SPM_LOG_LEVEL` | `INFO` | Python log level (all records go to container stdout) |
| `SPM_SKIP_LOGIN` | `0` | **Mockup/preview only**: bypass admin login (`*** LOGIN DISABLED ***` banner in log). Never set on a real deployment |
| `PUID` / `PGID` | `2000` / `2000` | uid/gid the app runs as — must match the owner of your `./media` bind mount (see below) |
| `SPM_SKIP_CHOWN` | `0` | `1` = never chown `/config` at boot (read-only or root-squashed NFS/SMB mounts) |
| `SPM_CHOWN_MEDIA` | `0` | `1` = also chown the media mount point, `recursive` = the whole media tree (slow on big libraries) |
| `SPM_CHOWN_EXTRA` | *(empty)* | extra paths to chown at boot, space separated |
| `SPM_AUTO_DRI_GROUP` | `1` | join the group that owns `/dev/dri/renderD128`, so Quick Sync keeps working with a custom `PUID` |
| `SPM_EXTRA_GROUPS` | *(empty)* | extra group ids for the app user, comma separated (e.g. `44,989`) |

Everything else (portals, MACs, channels, templates, users, EPG sources, settings) is configured in the GUI and persisted in the database.

---

## Permissions: running as your own user (`PUID` / `PGID`)

The image ships a built-in unprivileged account `spm` (**uid/gid 2000**). A
host folder bind-mounted into the container keeps its **host** ownership, so if
`./media` belongs to your NAS/desktop user the app cannot list it — the Sources
→ *Local* directory browser then dies with:

```
PermissionError: [Errno 13] Permission denied: '/media'
```

Fix: tell the container which ids to run as (same idea as the linuxserver.io
images). The entrypoint moves the `spm` account to those ids, chowns the state
volume and joins the group owning the VAAPI render node before it drops
privileges and execs uvicorn.

```bash
stat -c '%u:%g' ./media          # -> e.g. 1026:100   (or: id your-user)
```

```yaml
# docker-compose.yml
services:
  app:
    environment:
      PUID: 1026        # or from .env:  PUID: ${PUID:-2000}
      PGID: 100
```

```bash
docker compose up -d             # entrypoint re-applies ids + ownership
```

Notes:

- Values are numeric (a user/group **name** that exists *inside* the image also
  works). Invalid values fall back to `2000` with a warning in the log.
- `PUID=0` runs the app as **root** (privileges are not dropped) — debugging
  only, never for a real deployment.
- The container starts as root for a few milliseconds, then execs the app as
  `PUID:PGID` (PID 1 stays the app, so `docker stop` and signals work). With a
  `user:` / `--user` override you are not root, so ids are applied by Docker
  instead and the entrypoint simply execs the CMD. Because the container's
  default user is root again, `docker exec` gives you a **root** shell — add
  `-u spm` (or `-u $PUID:$PGID`) to poke around as the app itself.
- `/config` is chowned recursively at boot (it is the container's own state).
  The **media mount is never chowned** unless you ask for it: rewriting the
  ownership of your media library is not something a container should do
  silently. Use `SPM_CHOWN_MEDIA=1` (mount point only) or
  `SPM_CHOWN_MEDIA=recursive` if the tree itself has the wrong owner, and
  `SPM_SKIP_CHOWN=1` to skip chowning entirely.
- Boot diagnostics go to stdout, so `docker logs stalker-proxy-manager | grep
  entrypoint` tells you exactly what the app can see:

```
[entrypoint] spm: uid 2000 -> 1026
[entrypoint] joining group 44 (owner of /dev/dri/renderD128)
[entrypoint] chown -R 1026:100 /config
[entrypoint] running as uid=1026 gid=100 groups=100,44; /config -> read+write
[entrypoint] /media -> read+write
```

and, when the ids do not match:

```
[entrypoint] WARNING: the app user cannot read /media
[entrypoint] WARNING: fix: set PUID/PGID in docker-compose.yml to the owner of that mount
[entrypoint] WARNING:      (on the host:  stat -c '%u:%g' <media dir>   or   id <your user>)
```

---

## The workflow

1. **Portals** – add each Stalker portal base URL and its MAC addresses (optionally per-MAC password). *Check Portal* resolves the real endpoint (`/c/`, `/client/`, `/portal.php`, …) and verifies each MAC online (busy-ness and subscription expiry included); the per-MAC result now carries **why** a failure happened (`code` + the panel's own wording), not just "failed". Two per-portal network switches live in the same editor: **HTTP proxy** and **Allow broken TLS** (certificate verification is ON for every portal unless that box is ticked — a `TLS unverified` badge then marks the portal in the list, because it is a deliberate exception, not a setting to forget). *Delete* offers a replacement-dialog cleanup for playlists that reference it.

   **Multi-MAC health.** Portals with two or more MACs get a background sweep (Settings → *Multi-MAC status refresh*, default every 60 min; `0` pauses it) that handshakes every MAC and refreshes `status` / `online` / `expire_date` / `last_checked` — the same work *Check Portal* does, kept honest overnight. MACs currently occupied are skipped so a viewer is never kicked: that covers both ffmpeg-proxied plays (hard `mac_locks`) and redirect/direct plays (a soft lease after the 302, because once the player is sent to the panel CDN we no longer hold the socket). The Portals toolbar *Refresh MAC health* button runs the same sweep on demand. On a multi-MAC portal, **Compare genres across MACs** first lets the operator select which online accounts to contact, then renders Live/VOD/Series genre-by-MAC matrices with text, difference, coverage, MAC and package filters. Exact matching signatures are grouped as packages; selected or visible stored genres can be enabled/disabled in bulk. Successful Live/VOD/Series genre counts and the comparison time are persisted per MAC and shown directly on the portal list (`Genres L … · V … · S …`); failed content-kind requests retain their previous count instead of being recorded as zero. The comparison **upserts the selected MACs' union into the portal's genre tables** (existing `enabled` flags are kept; brand-new genres land disabled). Useful when a "secondary" MAC is actually a different package from a shared-login reseller. Removing a MAC or deleting a portal also drops its runtime leftovers (mac locks, redirect leases, pooled Stalker sessions) — DB cascades already wipe the durable rows.

   **Is a MAC available right now?** Two answers, and the GUI shows both. *Our* view is the badge next to each MAC: `free here` (this proxy holds nothing), `streaming · user` (an ffmpeg pipe is on it right now) or `leased 143s` (the player was 302'd to the panel CDN and we cannot see it stop — the badge tooltip names the user and the channel). The other answer belongs to the panel, which has no "who uses this MAC" call: the only moment it tells you is when you ask it for a link, and then only as a refusal code. The **Test** (broadcast) button per MAC does exactly that — handshake, `create_link` for one channel, then read the first bytes:

   | Result | What it means |
   |---|---|
   | `available` + bytes | the panel built a link **and** streamed it: nobody else holds this MAC |
   | `in-use` (`limit` / `account is in use`) | another device is watching on that MAC, or the panel has not timed out its last stream yet |
   | `unusable` (`access_denied`, token codes) | the MAC itself is refused — fix it in Portals, retrying will not help |
   | `no-data` | the link was built but sent nothing: the slot is held mid-flight (a zap, another device, a panel timeout pending) |
   | `busy-ours` | this proxy is using the MAC — answered from local state without touching the panel |

   In **Add/Edit portal**, **Resolve** enables as soon as a name, URL and valid MAC are entered. It tests the current (including unsaved) URL/MAC, proxy, TLS, identity and timezone settings without creating or updating the portal. **Save** remains explicit. The portal-list Resolve action still resolves and stores metadata for the saved portal.

   The test costs one portal request and a few seconds of that MAC's connection slot, which is why it is a button and never automatic; `POST /api/portals/{id}/macs/probe` runs it for every MAC of a portal (sequential on purpose — a panel that rate-limits dislikes four concurrent slot tests).
2. **Fetch Sources** – background job pulls genres → channels/movies/series → seasons/episodes with progress logging. Enable/disable **per genre** what enters the catalog; series enablement is per season. In the **Edit portal** popup this is a two-step flow: *Fetch genres* loads the live/VOD/series genre lists (all disabled by default — including the synthetic *(All VOD)* / *(All series)* a portal without categories gets), you tick the genres you want (the filter box narrows the list as you type), and **Save** then fetches the items of exactly those enabled genres.
3. **Playlist Builder** – three tabs (Live, VOD, Series, Local). Every output item keeps its own **ordered fallback chain** (source × portal × MAC as needed), an optional **ffmpeg template**, group, epg id and logo. Drag & drop reorders channels. The **channel number** of a live channel *is* its position in the final playlist, kept in sync both ways: saving a new "Channel number (opt)" in the Edit-channel popup moves the channel to that position (the others push down), and reordering, deleting or toggling channels re-derives every number from its position — so `tvg-chno` and the row order can never disagree. A live channel's number can also be **locked** (the lock toggle in the Live table's *Number* column, or *lock number* in the Edit-channel popup, which makes the number field read-only): a locked channel keeps that number through reordering, deletes, adds and toggles — the other channels renumber around it and skip the locked number — and its row is no longer draggable (the grip becomes a lock icon, other rows still drag past it). Clicking a **VOD** or **Series** row (or its ⓘ button) opens the same detail popup as Input Sources — stored portal metadata, a lazy **stream probe** (codec/resolution/bitrate) and **TMDB** enrichment. The ▶ *test stream* buttons (here and in Input Sources) open the preview player, which closes via its header **×** or the **Stop & Close** button.
4. **Users** – each user gets `username/password` and can receive **M3U** and/or **Xtream** URLs (copy-buttons in the GUI). Per-user active-connection caps enforced.
5. **Dashboard** – counters, active streams with kill buttons, quick actions (fetch, retry-busy), messages pane.

### Web preview and technical stream information

Use **▶ / Play test** to open a browser preview. Playlist previews stay on the
same-origin proxy, including when Redirect is the configured default; Local
files are remuxed instead of returning a raw MP4 under a `.ts` URL. Series
previews select the first playable episode in the first enabled linked season.
This does not change delivery to M3U/Xtream clients.

**Probe stream** stops only this preview, releases its connection, and runs a
fresh technical inspection. **Replay** resumes playback; **Enable sound**
unmutes the initially muted player. The report includes all reported video,
audio and subtitle tracks: codecs/profiles, resolution, frame rate, aspect
ratio, bitrates, pixel format/bit depth, color information, sample rate,
channel layout, container and duration. Expand **All technical metadata
(JSON)** for the remaining ffprobe stream, format, program and chapter fields.
Unavailable fields are marked *Not reported*, not estimated.

The report describes the **original input before FFmpeg processing**, not the
transcoded preview output. Playlist probes inspect the **primary source**, which
may differ from a fallback used for playback. They skip occupied portal MACs,
reserve the chosen MAC while probing and release it on completion, cancellation
or timeout. With all connections busy, retry later; other viewers are not
stopped. Browser help icons explain this without adding permanent help text.

Detailed probing uses ffprobe (bundled in the Docker image); installations
without it show an explicitly labelled FFmpeg summary instead. The authenticated
`GET /api/playlist/probe?scope=source|playlist&kind=…&id=…` API accepts stored item
IDs, not arbitrary URLs or paths. For source Series previews the ID identifies
an **episode**; for playlist Series it identifies a **series playlist**. The
raw input filename/URL field is omitted from the returned ffprobe format data.

Browser playback still requires browser-supported codecs and an MPEG-TS preview
template. H.264/AAC is the safest choice; unsupported source codecs require a
transcode template rather than Copy. Source previews offer **Retry with** to test
another template without changing the saved settings.

### Playlist source health

Only **Settings** always shows both **Playlist source health** (Live, VOD, Series
and Local) and **EPG guide health**, refreshed every 30 seconds. On **Dashboard**
and **Playlist**, each panel appears only when it has an alert or its health
check fails, and disappears again when resolved. Unverified playlist inputs alone
do not trigger an alert. Hidden panels keep checking in the background; both
summaries remain visible in Settings even when everything is healthy. Only enabled playlist
items are counted. Expand the Playlist details to filter by type/status/name or
group, inspect individual source reasons, and open the relevant item editor—even
when it is not on the current table page.

- **No usable source:** missing playback links, empty commands, missing/disabled
  portals, no eligible MAC accounts, missing local files, or recent unsuccessful
  media probes on every otherwise eligible input.
- **Needs attention:** a bad fallback, partial Series coverage, busy accounts,
  recorded connection/authentication problems, or recent playback failures.
  Playback failure can also be caused by output/FFmpeg settings; it is not proof
  that the input is broken.
- **Unverified:** structurally usable input without recent media evidence. A
  successful redirect/link resolution alone is **not** proof of working media.
- **Available / verified:** a readable, nonempty local file, or a portal input
  recently confirmed by a media probe or actual FFmpeg output bytes. File
  availability alone does not verify decoding or the output template.

Automatic checks are **passive**: batched database reads and bounded, off-thread
filesystem checks, with no stream opens or portal requests. **Source details →
Probe source** explicitly checks that input before FFmpeg, respects busy MACs,
and cancels when the dialog closes. Testing a primary does not test its fallbacks.
Successful media evidence and explicit probe failures expire after 15 minutes;
repeated playback failures are short-lived warnings using the playback breaker
cooldown. Evidence is bounded and process-local (reset by a restart), not a new
persistent setting or backup table. Failed checks are retryable, not permanent
blacklists. Filesystem results are cached for up to 15 seconds; a stalled mount
is reported as unverified rather than missing.

Series checks cover **every exported episode** in enabled playlist seasons and
match fallback sources by season/episode number. Detail dialogs show up to 40
candidate inputs, prioritizing problems; totals cover all episodes. The checks
follow the configured MAC-first/portal-first playback policy. Input-catalogue
selection flags are warnings, not playback gates for explicit existing links.

Admin API: `GET /api/playlist/health` (`kind`, `status`, `q`, `page`, `per_page`).
No credentials, stream commands or raw probe errors are included in this report.

### EPG setup and matching

The three requested Rytec NL Basic feeds are enabled once on upgrade. Settings
now includes **Check portal EPG**, **Use source portal EPG**, and a selectable
refresh interval (1–168 hours; **0 pauses** automatic refresh). Portal guide
checks use authenticated Stalker bulk/short EPG APIs, skip busy MACs and never
open a video stream. Availability and guide horizon depend on the provider.

Use **Edit channel → Match EPG** or **Settings → Match channels** for fuzzy
matching. Ambiguous IDs open a chooser rather than being guessed; existing
assignments are preserved unless explicitly approved. **Review existing EPG
assignments too** includes already-matched channels. Matching distinguishes
channel numbers and `+` variants, deduplicates mirror IDs, and reprocesses cached
programmes after selections are saved.

**Edit channel → Guide priority & timing** adds ordered per-source guide IDs,
optional gap filling, and channel/per-mapping minute corrections. Apply stages
changes; the channel's Save commits them. Each source now retains its own
programmes, so refresh order cannot override priority.

Each EPG source's **Schedule & timing** button selects an inherited interval, a custom
1–168-hour interval, or manual-only mode, plus a stale threshold. Global **0**
still pauses all automatic refreshes. **Dashboard → EPG guide health** and
**Settings → Guide alerts** report missing/current-gap/stale guides and failed
sources. Alerts use the same corrected, priority-resolved schedules as XMLTV.

Source timing offers **Automatic**, **missing-offset timezone**, or **override
supplied timezone** modes using named IANA zones with daylight-saving rules,
plus a source-wide ±1440-minute correction. Corrections add to channel/mapping
offsets without compounding. An offline preview shows original and corrected
times before saving. Timezone changes reprocess cached originals; pending changes
are flagged until the guide is successfully reinterpreted. Older portal caches
need one fresh download to support timezone overrides.

**Users → output URLs → Copy EPG URL** provides the authenticated XMLTV URL for
external applications, filtered to that user's enabled Live channels/groups.
M3U `tvg-id`, Xtream `epg_channel_id`, and XMLTV channel/programme IDs agree.
See [EPG operation, verified-source research and improvement options](docs/EPG.md)
for details, limitations and free Viaplay guide recommendations.

### Client URLs (per user)

```
M3U:      http://<host>:8880/get.php?username=USER&password=PASS&type=m3u_plus&output=ts
Xtream:   http://<host>:8880/player_api.php?username=USER&password=PASS
Stream:   http://<host>:8880/play/live/{id}.ts?username=..&password=..
          http://<host>:8880/{user}/{pass}/{stream_id}.ts   (xtream short form)
xmltv:    http://<host>:8880/xmltv.php?username=USER&password=PASS
```

Xtream identity advertises only the implemented MPEG-TS output format, and its
`server_info` reports the externally visible scheme, host, and explicit/default
port correctly. Xtream-only users can play both `/live|movie|series/...` URLs
and the `/play/...` URLs returned by `get.php`; native M3U and Xtream catalogue
permissions remain independently gated. For Xtream API clients, enabled Local
playlist files are additionally mapped into **Movies/VOD** using their Local
group names, local group whitelist, effective Area template, and real file or
transcoded container extension. They are not exposed as Series; native M3U and
Enigma2 organization remain unchanged.

Users only ever talk to port **8880** — GUI, streams, playlists and APIs share it.

### Broader source matching in the channel editor

In **Playlist Builder → Add/Edit channel**, **Less strict matching** widens the
source-name search while keeping the closest matches first. Normal matching is
the default each time the editor opens. When there are no matches, click
**Try less strict matching** to turn it on. Both modes show up to 60 enabled
sources; neither changes the custom channel name or existing fallback chain.
For manual selection, turn on **Show all source channels** to ignore the custom
channel name entirely (including sources already used in other channels).
The separate **Filter source channels** field searches words in channel and
portal names, ignoring case; every word must match. Leave it blank to browse all
enabled sources, and use **Load more** to go beyond the first 60. Switching this
mode off restores name matching and the previous less-strict setting. Neither
search field nor browsing mode changes the channel name or chain automatically.
Help icons explain each option.

Regression tests: `pytest tests/test_playlist_suggest.py` and
`node tests/playlist_matching_gui.cjs` (requires `jsdom`).

### User group selections

**Users → Add/Edit user** offers **Select all** and **Deselect all** separately
for Live, VOD, Series, and Local, with a selected-count indicator. New users
start with all **currently available** groups selected in all four lists.
Only selected groups are included in their M3U/Xtream catalogues, XMLTV and
assigned Enigma2 bouquets. Clearing a list hides that entire content type;
clearing all four produces an empty catalogue. There is no empty-list wildcard.

**Existing users:** empty or missing lists now mean no groups too. To retain
visibility for a type previously using an empty list, edit the user, select all
for that type, and save. New groups added later must be explicitly selected.
Blank playlist group names are offered under Live, VOD, Series, or Local files.
The separate optional Enigma2 *receiver* filter is unchanged; it cannot expand
an assigned user's selections. This changes catalogue visibility, not the
separate stream URL authentication mechanism.

API creation without a `groups` field selects all current groups; an explicit
`groups: {}` or empty per-type lists selects none. Updates without `groups`
retain the user's existing selections.

Regression tests: `pytest tests/test_user_group_selection.py tests/test_group_whitelist.py`
and `node tests/user_groups_gui.cjs` (requires `jsdom`).

### Bulk enabling and playlist order

Enabling a selection in **Input Sources** appends new Live, VOD, Series, and
Local entries with distinct increasing **Ord** values, following the submitted
selection order. Existing entries keep their positions when re-enabled; Live
sources with the same channel name still join that channel's fallback chain.
Database transaction locks serialize concurrent additions, including across
app workers (PostgreSQL advisory locks; SQLite write reservations).

At startup, and before allocating new positions, historical duplicate or
nonpositive order values are repaired in their existing `(order, id)` sequence.
Disabled entries are included so their positions remain reserved. Names,
groups, templates, fallback chains and locked Live channel numbers are retained.
Drag reordering on later pages or filtered results reuses those rows' actual
positions rather than restarting at 1.

Drag/drop moves the row immediately and displays **Saving order…** with a
spinner, then **Order saved.** once the transaction completes. Conflicting table
controls are paused during the save. The response supplies confirmed order and
Live channel numbers directly, avoiding a second enriched playlist reload.
Failed saves restore the previous display with a visible retry message. Sort
by **Ord ascending** to drag; locked Live channels remain protected.

Regression tests: `pytest tests/test_bulk_source_order.py tests/test_playlist_order.py
 tests/test_live_number_order_sync.py`, `node tests/playlist_order_gui.cjs`, and
`node tests/playlist_drag_feedback_gui.cjs` (requires `jsdom`).

### Fast group and template assignment

In all four **Playlist** tabs, group and FFmpeg-template changes show **Saving…**,
then **Saved**, directly beside the control. A failed save restores the previous
displayed value and shows a retry/reload message. Bulk assignment dialogs show
progress, prevent duplicate submissions and stay open on failure.

Ordinary assignments update the visible row and its cached editor values without
reloading the entire page. Group-sorted/filtered views still refresh when needed.
Bulk group/template assignments use bounded SQL updates rather than loading each
selected playlist row; channel order, numbers and number locks are unchanged.

Group fields select their text on first focus/click, including keyboard focus.
A second click places the caret normally, and mouse dragging can select text.
Row dragging is disabled over editable controls; use the grip or non-editable
part of the row to reorder. Reordering continues to show **Saving order…**, and
is blocked while an assignment is being saved to avoid conflicting changes.

### Fast playlist and stream startup

Generated M3Us and Enigma2 bundles are cached until an output-relevant database
write invalidates them; repeated player refreshes no longer recalculate a full
catalogue or run revision queries. Playlist Builder live/VOD source chains and
local-file metadata are loaded in batches rather than one query per row.

VLC and Enigma2 commonly issue `HEAD` before `GET`. Every portal stream alias
answers that authenticated probe with metadata only—it does not resolve a
portal, occupy a MAC, or launch FFmpeg. Actual GET startup writes an
`[output] startup timing` log and a `Server-Timing` response header separating
prepare/resolve, first-byte, and total time. Direct local playback additionally
reports path, template, and stream-registration timings.

Local directory scans also persist container, codec, and subtitle metadata next
to each file's size and modification time. Local remux/subtitle startup gates
reuse that metadata after a restart instead of launching duplicate FFmpeg
probes; changed or unsuccessfully probed files safely fall back to runtime
probing. The managed Enigma2 VOD/remux, Vu+ live, and Dreambox presets bound
FFmpeg input analysis to one second/megabyte (`-analyzeduration`/`-probesize`)
to reduce time-to-first-byte without changing area or template selection.

Resolved portal/MAC sessions are pre-authenticated in the background, so the
first play normally reuses a live token and HTTP connection. Once a source/MAC
actually produces bytes it is preferred for later plays of that same playlist
item. A short process-local circuit breaker suppresses repeatedly failing
sources while alternatives exist, then automatically half-opens after the
cooldown; configured playlist priority and database rows are never rewritten.

---

## ffmpeg templates & transcoding (DS918+ Quick Sync)

Templates are full editable ffmpeg commands with GUI field ↔ command **2-way sync**: the option fields (encoder, bitrate, resolution, fps, GOP, audio, container, rate control + QP, extra args) rebuild the command text, and editing the text parses back into the fields. Two rules make that loop safe:

* **Your flags win.** The resilience options (`-reconnect …`, `-rw_timeout`, `-fflags`, `-err_detect`) and the container options (`-mpegts_flags`, `-hls_time`, `-hls_list_size`, `-hls_flags`) are defaults, not policy: if the command already sets one, the renderer leaves it alone instead of adding a second occurrence — so `-reconnect 0` in the extra args means 0.
* **Looking at a template does not change it.** Parsing is a fixed point (no flag piles up on the second pass), and it is deliberately *partial*: the editor sends the row's own fields along as the base, so a CQP command — which carries no bitrate by design — does not reset the template's tuning, and a command with no `-rc_mode` at all stays `AUTO` rather than inheriting the shipped default.

### Help throughout the GUI

Explanatory text on main pages and in dialogs is available through the **? help**
icons instead of permanent paragraphs. Hover, focus with the keyboard, or tap/click
an icon to read it. **Escape**, another tap, or clicking outside dismisses it without
closing the dialog. Status, errors, results, destructive-action warnings and
confirmation checkboxes remain visible. Help is refreshed for dynamically loaded
forms and changing FFmpeg dependencies.

GUI contributors: mark explanation nodes with `data-help="Subject"` and optionally
`data-help-for="control-id"`; do not mark containers that contain controls, results
or warnings. Existing field-label `title` hints are upgraded automatically. Source
nodes are retained for updates and accessibility; tooltip content is rendered as
plain text. Regression checks: `pytest -n 0 tests/test_help_tooltips.py` and
`node tests/help_tooltips_gui.cjs` (requires `jsdom`).

### Editing parameters

Click a template (or **New**) to edit it directly in the main settings pane.
There is no separate editor popup. Use **Save template** to save the draft.

Use **Default template → Set default** above the template list to select the
fallback for items without an explicit item/area template. A new installation
starts with **Redirect (bypass ffmpeg)**; later choices (including built-in
presets) survive restarts. Changing the default does not save or discard the
current editor draft. The default must be enabled; select another default before
disabling or deleting it.

- Every form field has a keyboard/touch-accessible **? help** button with units,
  scope, and relevant limitations.
- Dropdowns provide sensible presets. **Custom value…** is available for device
  paths, encoder names, bitrates, resolution/aspect, frame rate, GOP, profile/level,
  quantizer, encoder queue depth, and audio parameters. Known app modes (hardware
  path, filter preset, subtitle mode, output path and VAAPI rate-control mode)
  retain fixed choices rather than pretending arbitrary values are supported.
- Custom dimensions such as `1600x1000` or an even height such as `900p` work in
  structured mode. Explicit dimensions override Aspect. Fractional frame rates
  such as `23.976`, `29.97` and `59.94` are offered. Form and API range validation
  catches invalid numeric settings before saving (for example CQP outside 0–51).
- Controls that the current configuration ignores are **disabled**, with the reason in their help tooltip.
  Both preset and custom inputs retain their saved values and become editable again
  when relevant. This covers video/audio copy, CPU/device selection, resize/aspect,
  codec-specific tuning, lossless audio bitrate, and all VAAPI rate-control modes.
  CQP/ICQ disable bitrate/maxrate/buffer; AVBR disables maxrate/buffer; CBR disables
  maxrate when an explicit buffer is set (otherwise maxrate can supply its buffer
  fallback). Quality applies to CQP/ICQ/QVBR. Invalid inactive values do not block
  saving another mode; they are validated on reactivation. Storage limits still apply.
  Incompatible filter/subtitle and guided advanced choices are also disabled, without
  locking the selector itself. Unknown custom codecs, runtime input protocols and
  arbitrary raw extra flags are not guessed: raw flags remain an expert escape hatch
  and are never automatically removed when changing modes.
- **Advanced FFmpeg options** offers 26 common input/output parameters, including
  read timeout, reconnect behavior, probe size, analysis duration, encoder preset,
  CRF, threads, packet queues, mux delay, and HLS settings. Choose a suggested value
  or type one. **Add / replace option** edits the corresponding extra flags; it
  does not save until **Save template** is clicked. Remove a flag directly from
  the extra-flags text to return to the generated default. The read timeout has
  one owner (`-rw_timeout` in Extra input flags), so it is no longer silently
  replaced by a second control.
- **Other FFmpeg option…** accepts a build-specific flag, value and input/output
  placement. For form-owned flags such as `-vf`, `-map` and codecs, or arbitrary
  filter graphs, use the **full command**. FFmpeg has many codec/build-specific
  parameters; the GUI suggestions are not an exhaustive list of FFmpeg features.
  Quoted values, custom encoder presets and explicit dimensions are preserved
  through command parsing; a manual command remains authoritative until you
  explicitly switch back to fields mode.

Preset suggestions and numeric checks do **not** guarantee encoder/hardware
compatibility. The GUI warns about common CPU/VAAPI/QSV mismatches. Use **Validate
syntax** and **Demo** on the actual host to check its FFmpeg build, drivers, codecs,
container and player combination. No new database columns are required by this editor.
Regression checks: `pytest -n 0 tests/test_ffmpeg_editor.py tests/test_ffmpeg_applicability.py tests/test_ffmpeg_defaults.py`; optional DOM checks:
`node tests/ffmpeg_editor_gui.cjs` (install `jsdom` as described in the backup section).

Shipped presets (stored as rows in the database and **re-seeded on every boot** — see below):

| Template | Use |
|---|---|
| VAAPI 720p ~1M (DS918+ reference) | hardware H.264 via `/dev/dri/renderD128`, CQP 26 |
| VAAPI 1080p ~2.5M | hardware, full HD |
| QSV 720p ~1M | Quick Sync via `-hwaccel qsv` (alternative syntax) |
| Software 720p (libx264) | no-GPU fallback |
| Copy / passthrough | remux only (`-c copy`); also the automatic fallback when no GPU device is mapped |
| **Dreambox DM800se (Enigma2 / MPEG2-SD)** | downmix to an MPEG-2 transport stream the ancient Enigma2/openpli box can play (see below) |
| **Enigma2 VOD - remux + subtitles (MKV)** | container swap only (`-c copy`) into **Matroska**, copying *every* subtitle track (SRT/ASS/PGS/DVB) — the way VOD & series get subtitles without any transcoding (see *Subtitles for VOD & series* below) |
| **Enigma2 VOD - VAAPI 1080p H.264 + AC3 + subtitles (MKV)** | the 4K/HEVC rescue path: video re-encoded **on the GPU** to H.264 High@4.0 1080p with AC3 audio, subtitles copied through untouched |
| **Vu+ Duo2 live (Enigma2 / H.264 1080p MPEG-TS)** | live TV for a Vu+ Duo2: H.264 High@4.0 1080p, **VBR**, **source FPS (`src`)**, AC3 in MPEG-TS with DVB bitmap subtitles (service reference `1`/`4097`) |
| **Redirect (bypass ffmpeg)** | not an ffmpeg command at all — the player is 302-redirected straight to the portal's CDN. **The default template**: any item without an explicit template assignment redirects (see below) |

**Redirect (bypass ffmpeg) is the default.** The old global *proxy vs redirect* switch in Settings is gone: redirect is now a built-in template **and the default**. An item without an explicit template assignment is 302-redirected straight to the portal's CDN — instant start and zero CPU, but no transcode, no transport-stream rewriting and no mid-stream fallback. Assign any other template (inline *FFmpeg tpl* dropdown, the edit dialog, or bulk *Assign template…* in the Playlist Builder) to switch that channel back to ffmpeg proxying/transcoding. The `?mode=redirect` / `?mode=proxy` query parameter still works as a per-URL override.

**Fast local playback in VLC.** A local item whose effective template is Redirect, Copy, or otherwise
unassigned is advertised with the file's real on-disk extension and served directly with HTTP Range
support—never through the `.ts` FFmpeg remux path. Its M3U entry includes
`#EXTVLCOPT:network-caching=500` by default, configurable under **Settings → VLC local-file network
cache** (`0` leaves caching to the player). Direct responses also send `X-Accel-Buffering: no`, so an
nginx-compatible reverse proxy does not hold back the first bytes. A genuinely transcoding local
item continues to use the template's `.ts` or `.mkv` output extension.

**Default templates are persistent (stored in the database).** The shipped presets are real `ffmpeg_templates` rows marked `is_builtin`. On every boot the app reconciles them by name, so:

* they survive deletion (delete one, restart → it is back),
* they pick up fixes/tuning shipped in new releases,
* your edits win — a built-in whose command you changed by hand keeps your text,
* **Redirect (bypass ffmpeg)** is the initial default. The selected enabled default is retained on every boot, whether built-in or user-created; if no valid default remains, Redirect is preferred as the fallback.

> Deleting a built-in template is therefore always safe — the next restart restores it, and the DS918+ reference preset stays available as a fallback.

**VAAPI tuning (what the `low-power` / `rate-control` / `async-depth` fields do).** The VAAPI presets are tuned for the Intel iHD driver on Apollo Lake (the DS918+'s J3455). The reference 720p template renders to:

```text
ffmpeg -rw_timeout 10000000 -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1
       -reconnect_delay_max 5 -fflags +genpts+discardcorrupt -err_detect ignore_err
       -init_hw_device vaapi=intel:/dev/dri/renderD128 -hwaccel vaapi
       -hwaccel_device intel -hwaccel_output_format vaapi -i <url>
       -vf scale_vaapi=w=1280:h=720:format=nv12,fps=25,setsar=1
       -map 0:v:0 -map 0:a:0? -dn -sn
       -c:v h264_vaapi -profile:v high -level 4.1
       -g 50 -r 25 -low_power 1 -rc_mode CQP -global_quality 26 -async_depth 4
       -c:a aac -b:a 128k -ac 2 -ar 48000
       -f mpegts -mpegts_flags +resend_headers pipe:1
```

**Subtitles: bitmap tracks are kept as DVB (`subs=dvb`, hardware-safe) — text tracks are dropped.** The proxy's output is an MPEG-TS/HLS pipe, and that pipe can only carry **bitmap DVB subtitles**; the text formats found in VOD/local containers (SRT/ASS/SSA) die at the `dvbsub` re-encode (`Subtitle encoding currently only possible from text to text or bitmap to bitmap`) or at the mpegts muxer, and bitmap PGS/vobsub have no text converter. Any such track aborts ffmpeg **before the first output byte** — which is why, with the old unguarded `-map 0:s? -c:s dvbsub` mapping, *every movie file with a text subtitle track* looked like "ffmpeg templates don't work for VOD/local files" while live MPEG-TS (DVB subs or none) kept playing. The 23.976 fps vs 50 fps difference people notice in the same breath is only the fingerprint of that split: film content in files vs broadcast TV over UDP/TS — the frame rate itself transcodes fine either way.

**Transcoding is hardware-only here, and the subtitle support is chosen to match.** The template editor's **Subtitles** field has three values:

| Mode | What it does | Guarantees |
|---|---|---|
| **Drop** (`-sn`) | no subtitle track in the output | the safe default; never interacts with the source |
| **Copy all** (`subs=keep`, **Matroska output only**) | maps *every* subtitle track and copies it **byte for byte** into a Matroska (`.mkv`) output — text (SRT/ASS/SSA) *and* bitmap (PGS/DVD/DVB). This is the only way to deliver VOD/series subtitles from this proxy, because the container, not the pipeline, is what MPEG-TS lacks | **hardware-safe by construction**: `-c:s copy` is a byte copy, there is no subtitle *encoder* in the pipe at all, and the video path stays pure VAAPI/QSV. Asked for on an MPEG-TS/HLS output it degrades to *Keep as DVB* (and the editor says so) |
| **Keep as DVB** (`subs=dvb`) | maps the source's subtitle track and re-encodes **bitmap** subs (PGS / DVD / DVB) into a **DVB subtitle track** in the output TS — players that decode DVB subs (VLC, Kodi, Enigma2, most MAG boxes) show them in their subtitle menu. A remux template (`-c copy`) carries already-DVB tracks through with `-c:s copy` | **hardware-safe by construction**: the subtitle track is demuxed and re-encoded *independently of the video*, so the VAAPI/QSV pipeline (`-hwaccel …` → `scale_vaapi/qsv` → hardware encode) never touches it — the only CPU work is a few kbit/s of palletised bitmap, which no GPU encoder does anyway |

There is deliberately **no burn-in mode**: rendering text into the picture (libass `subtitles=` filter) requires CPU video frames, which would defeat hardware-only transcoding. A stored command that still contains a `subtitles=` filter has it stripped at spawn time, and the parser flags it (`subtitles= burn filter dropped: software-only`). Text subtitles therefore have exactly two supported routes: keep them in the *original file* (local items served with the Copy/Redirect templates play the untouched MKV/MP4, and the player loads them as usual), or pre-burn them into the file outside this proxy. Sources whose only tracks are text get their mapping **automatically degraded to `-sn` at spawn** — the spawn gate probes the file/link once (8 s cap; the 10-min cache is keyed without the per-play token, so a second play of the same movie skips the probe) and logs `no convertible (bitmap) subtitle track -> subtitles dropped` instead of letting ffmpeg die. Live plays are never probed (zapping stays instant; live TS carries DVB subs natively). Every shipped MPEG-TS preset (VAAPI 720p/1080p, QSV, Copy, Dreambox, Vu+ Duo2 live) ships with `subs=dvb`, and the two Matroska presets ship with `subs=keep`; set the field back to *Drop* on any template if you prefer no subtitle track at all.

### Subtitles for VOD & series: change the container, not the pipeline

MPEG-TS has **no slot for text subtitles**, and ffmpeg cannot convert text to a bitmap track without *rendering* it into the picture — which needs CPU video frames and would throw away hardware-only transcoding. So the fix is not a smarter subtitle mode, it is a different **container**: set the template's **Output** to **Matroska / MKV** and its **Subtitles** field to **Copy all**, and every track the source carries is copied straight through:

```text
ffmpeg … -i <url> -map 0:v:0 -map 0:a:0? -map 0:s? -dn
         -c:v copy -c:a copy -c:s copy -f matroska -live 1 pipe:1
```

* `-live 1` is what a pipe needs: the Matroska muxer must not try to seek back and patch cues/duration at the end (it cannot, on a pipe).
* The same mode works **with** hardware transcoding: `-c:v h264_vaapi … -c:s copy` re-encodes the video on the GPU (4K/HEVC → H.264 1080p for a box that cannot decode it) while the subtitle tracks ride along untouched. That is the *Enigma2 VOD - VAAPI 1080p* preset.
* At spawn time the gate probes the source only to route **around** the handful of codecs Matroska cannot hold (teletext, EIA-608/708); everything else is kept, and a source with no subtitles at all costs nothing (`-map 0:s?` is optional).

Play those items through the **`.mkv` URL aliases**, which exist next to the `.ts` ones for every kind:

```text
/play/vod/{id}.mkv?u=…&p=…        /movie/{user}/{pass}/{id}.mkv
/play/episode/{id}.mkv?u=…&p=…    /series/{user}/{pass}/{id}.mkv
/play/local/{id}.mkv?u=…&p=…
/play/live/{id}.mkv?u=…&p=…
```

The URL extension does not change the pipeline (the template's `output_format` does) — it sets the `Content-Type` (`video/x-matroska`), and set-top boxes do sniff it.

**On an Enigma2 box (Vu+ / OpenPLi)** this is the difference between "no subtitles ever" and a working subtitle menu for VOD and series. Install ServiceApp + exteplayer3 on the box (`opkg install enigma2-plugin-systemplugins-serviceapp exteplayer3 ffmpeg`) and give those bouquet entries the service reference **`5002`** (exteplayer3) instead of `4097`; live TV keeps `4097` (or `1`) with the MPEG-TS/DVB-subtitle presets. Example bouquet line:

```text
#SERVICE 5002:0:1:0:0:0:0:0:0:0:http%3a//nas%3a8880/play/vod/42.mkv?u=box&p=secret:Some Movie
#DESCRIPTION Some Movie
```

Note that a proxied stream is a live pipe: there is no HTTP Range, so **no seeking** inside a `.mkv` proxied play. When the source codec already fits the box, the *Redirect (bypass ffmpeg)* template is the better choice — the player gets the original file from the CDN with subtitles *and* seeking, at zero CPU. See `docs/ENIGMA2-INTEGRATION-OPTIONS.md` for the full picture (bouquet generation and pushing to the box are the next phases).

### Enigma2 receivers: generated bouquets (the *Enigma2* tab)

An Enigma2 box reads plain-text bouquet files, so SPM writes them. A **receiver profile** (Enigma2 tab) turns the playlist into `userbouquet.<prefix>_*.tv` files and decides, per content kind:

| Setting | Meaning |
|---|---|
| **Player** | the leading number of the service reference: `1` (DVB pipeline — live TS, native DVB subtitles, lowest latency), `4097` (servicemp3/gstreamer, the generic default), `5001` (ServiceApp → gstplayer), **`5002`** (ServiceApp → exteplayer3 — text subtitles and multi-audio) |
| **Container choice** | `auto` (default) resolves the URL alias **per item, from the ffmpeg template that item is assigned**; `fixed` uses the two *Container* dropdowns for everything |
| **Container** | which URL alias the line points at in `fixed` mode: `ts` or `mkv`. It has to match the item's ffmpeg template — the preview warns when `mkv` is combined with a player that cannot show text subtitles |
| **Delivery** | `template` (whatever each item is assigned in the Playlist Builder), or `proxy`/`redirect` appended as `?mode=` for this box only |
| **Layout** | `group_markers` (one bouquet per group, marker lines per series and season — the default), `per_series` (one bouquet per show), `flat` (one per content kind). Bouquets are auto-split into numbered parts above *Max per bouquet* (default 1500) because Enigma2 redraws the whole list on every zap |
| **Output user** | whose credentials and group whitelist the URLs carry; the profile can narrow the group filter further, never widen it |

Defaults are the Vu+ Duo2 recipe: live = `4097` + `.ts`, VOD and series = **`5002` + `.mkv`** so the copied SRT/ASS tracks actually reach the box.

**A real library mixes deliveries, so the bouquet does too.** Every playlist row carries its own ffmpeg template (falling back to the default one), and the template decides what actually comes out of the pipe — one movie is remuxed to Matroska, the next is a plain MPEG-TS transcode, and untouched rows usually sit on the *Redirect* preset, where SPM answers `302` and the box fetches the panel's file itself. Announcing all three as `.mkv` would be a lie the player notices. In `auto` mode each line is therefore resolved on its own:

| The item's template | Line gets | Why |
|---|---|---|
| `@redirect` (bypass ffmpeg) | the profile's alias, player ≥ `4097` | the container is the panel's, not ours — the alias is cosmetic because the box follows the redirect and sniffs the body. Best case for VOD: original subtitles **and** seeking survive |
| `output_format = matroska` | `.mkv` | the remux carries text subtitles |
| anything else | `.ts` | MPEG-TS out of ffmpeg |

This per-item rule also applies to **Local** bouquets. A local MP4/AVI assigned
**Enigma2 VOD - remux + subtitles (MKV)** is written as `/play/local/{id}.mkv`,
not forced back to `.ts`; the response is announced as `video/x-matroska` and
FFmpeg keeps `-f matroska`. Regenerate and push/pull the bouquet after changing
a local item's template.

Service type `1` hands the bytes straight to the DVB demuxer, which only understands raw TS: items that are MKV or direct are automatically raised to `4097` and the preview says so (use `5002` if you want their subtitles). The summary line counts the split — *114 services · 0 ts · 0 mkv · 114 direct* — so you can see at a glance which delivery your library is really on. Set *Container choice* to `fixed` for the old profile-wide behaviour.

**Preview before anything leaves the server.** *Preview* renders the exact file contents (`#SERVICE` / `#DESCRIPTION` / marker lines) plus a summary — *13 bouquets · 114 services* — and flags the classic mistakes: URLs pointing at `localhost` (a receiver cannot reach that — set the public base URL in Settings), a `.mkv` container under player `4097`, or a deleted output user.

**Pushing from SPM (transport `ftp`).** Set the receiver's host, keep the login on `root` (the bouquet directory is root-owned; a stock OpenPLi enables the root FTP account) and the tab gets four buttons:

* **Test connection** — logs in, looks at `/etc/enigma2`, counts the bouquets a previous push left there and pings OpenWebif. Writes nothing.
* **Dry run** — connects and reports exactly what *would* happen: how many files, which stale bouquets would go, whether `bouquets.tv` changes. Still writes nothing.
* **Push now** — backs up, uploads, removes stale SPM bouquets, merges `bouquets.tv`, reloads the box.
* **Restore backup** — puts `bouquets.tv.spm-backup` back and deletes the bouquets SPM installed, i.e. the box as it was before the last push.

Three safety rules are built into the push, because a half-written `bouquets.tv` is a receiver that boots into an empty channel list:

1. **Nothing is written in place.** Every file goes to a temporary name *in the same directory* and is then `RNFR`/`RNTO`'d over the target — a rename inside one directory is atomic, so enigma2 always reads either the old file or the new one. (Uploading to `/tmp` and renaming across would fail: `/tmp` is tmpfs, `/etc/enigma2` is flash, and `rename(2)` cannot cross filesystems.)
2. **One restore point, always.** The box's `bouquets.tv` is copied to `bouquets.tv.spm-backup` before anything changes, every push, overwriting the previous one — the last known-good state, without a pile of dated files on a 512 MB flash.
3. **Only our own files are deleted.** `userbouquet.<prefix>_*.tv` and nothing else; your satellite bouquets and favourites are merged back into `bouquets.tv` untouched, and a profile that renders *no* services refuses to push instead of clearing the box.

Then SPM calls **`GET /api/servicelistreload?mode=2`** — bouquets only; modes 0/1 would also re-read `lamedb` and throw away the tuner's service cache for a change that never touched it. *Web interface auth* is per profile: `none` (a stock OpenPLi answers its API without credentials) or `basic` with the user/password you set on the box. A failed reload is a warning, not a failed push — the files are already there, and the box menu can reload them.

**Getting the files onto the box the other way (pull).** Each profile has an opaque token and a one-liner to run on the receiver:

```sh
wget -qO- http://nas:8880/enigma2/<token>/install.sh | sh
```

It downloads the tarball, backs `bouquets.tv` up, **merges** our entries into it (`grep -v userbouquet.<prefix>_` — your satellite bouquets and favourites are kept), copies the files into `/etc/enigma2` and reloads the service list via OpenWebif (`/api/servicelistreload?mode=2`). Re-run it (or put it in cron) after changing the playlist. *Download .tar.gz* gives you the same files by hand, and *rotate token* invalidates the old URLs. Pull needs nothing configured on the SPM side, which makes it the fallback when the box's FTP is disabled.

Service references use the SPM playlist id as the SID (`4097:0:1:2A:…`), so regenerating a bouquet does not renumber anything — the box's own favourites, picon names and the (later) EPG channel map keep pointing at the same services.

**VOD/episode/local inputs are paced (`-re`), live never is.** A file input would otherwise be drained at *encode* speed — ffmpeg pushes a whole movie through the pipe as fast as the encoder allows, the player's buffer fills, ffmpeg hits EOF long before the viewer reaches the end, and the stream stops mid-playback. For `vod`/`episode`/`local` plays the manager therefore inserts `-re` in front of `-i` (unless the template sets its own `-re`/`-readrate`), so the file streams at its own frame rate and lasts exactly as long as the content. Live inputs are already paced by their encoder and are never throttled.



**Most VAAPI presets ship on `-rc_mode CQP` — constant quantiser.** The **Vu+ Duo2 live** preset instead ships with **VBR** and **source FPS (`src`)**. Quality is pinned at the QP beside the mode dropdown (default 26) and the bitrate floats with the content: a hard scene does not get smeared into mush to protect a rate target, and a static news card does not burn bandwidth it does not need. CQP is the price of that: the encoder ignores `-b:v`/`-maxrate`/`-bufsize` in this mode, so **the renderer leaves them out of the command entirely** (a command that carries flags the encoder ignores is a command that lies — this text is also what the GUI shows and what you paste into a shell). The numbers stay filled in the template's fields: switch the mode to `VBR` or `CBR` and the tuning below is what you get back. The quality field is rendered for `CQP`, `ICQ` and `QVBR`, only for VAAPI encoders, and empty (`AUTO`) means "leave the flag out and let the driver choose".

**Bitrate numbers are tuned for external (internet) streaming** — that is, for the rate-driven modes. On a LAN the NAS uploads as fast as it likes; over the internet a bursty stream underruns the viewer's download link and stalls. Every transcode preset therefore caps spikes close to the target (`maxrate` ≈ bitrate + 10 %) and carries a ~2-second VBV buffer (`bufsize` = 2× bitrate) so short-lived congestion is absorbed by the encoder instead of freezing the player. The shipped values (used as-is by QSV/software and by the VAAPI presets once the mode is VBR/CBR):

| Preset | `-b:v` | `-maxrate` | `-bufsize` |
|---|---|---|---|
| VAAPI 720p ~1M (reference) | 1000k | 1100k | 2000k |
| VAAPI 1080p ~2.5M | 2500k | 2750k | 5000k |
| QSV 720p ~1M | 1000k | 1100k | 2000k |
| Software 720p (libx264) | 1200k | 1300k | 2400k |
| Dreambox DM800se | 1200k | 1300k | 2400k |

* `-low_power 1` selects the **fixed-function H.264 encoder** (`VAEntrypointEncSliceLP` in `vainfo`) instead of the EU/3D path — faster, lower power, and it leaves the GPU's shader units free for more concurrent streams. On this silicon it only exists for **H.264**, so the flag is emitted for `h264_vaapi` only (an HEVC low-power entrypoint would fail).
* `-rc_mode CQP -global_quality 26` makes rate control **explicit** — VAAPI's implicit "auto" mode is driver-dependent, so the mode and its target are spelled out instead. `QVBR`/`VBR`/`CBR` are available when you need a rate target; `QVBR` also takes quality. Explicit CBR uses the target bitrate, not a separate peak. `ICQ` targets quality and omits bitrate limits; `AVBR` keeps target bitrate but omits peak/buffer limits. Driver support varies.
* `-global_quality` is ffmpeg's generic "encode at this quantiser" option; for `h264_vaapi`/`hevc_vaapi` in CQP it is the QP (0–51, lower = better picture and bigger stream). Live IPTV around 22–30 is the usable band — 26 is where a 720p downscale of broadcast material stops being visible at sane sizes.
* `-async_depth 4` keeps more frames in flight → higher throughput and a faster time-to-first-frame.

**Reading your `vainfo` output:** `VAEntrypointVLD` = hardware *decode*; `EncSlice`/`EncSliceLP` = hardware *encode*. On the DS918+ that means **H.264 encode is the sweet spot** (`EncSlice` + `EncSliceLP`), HEVC/VP8 encode is 8-bit only (`HEVCMain` has `EncSlice`, no `Main10` encode, no low-power), and VP9 is decode-only. Use `h264_vaapi` for live transcoding; avoid `hevc_vaapi` for realtime use.

Verify acceleration inside the container with `vainfo -a` (should list EGL/VA-API entrypoints for the iHD driver).

**Dreambox DM800se (Enigma2, old openpli).** That box's ancient gstreamer cannot demux modern HEVC/VP9 or AAC-in-TS cleanly, and its 400 MHz MIPS CPU cannot decode 1080p. The built-in *Dreambox* template therefore transodes on the NAS to exactly what it *can* play — H.264 **Main@3.1** in **576p** (16:9 anamorphic) with **MPEG-1 Layer II audio** (universally understood by Enigma2) inside an MPEG-2 transport stream:

```text
-vf scale_vaapi=w=1024:h=576:format=nv12,fps=25,setsar=1
-c:v h264_vaapi -b:v 1200k -maxrate 1300k -bufsize 2400k -profile:v main -level 3.1
-c:a mp2 -b:a 192k -ac 2 -ar 48000
-f mpegts -mpegts_flags +resend_headers pipe:1
```

Assign it to a channel/playlist in the Playlist Builder and point the Dreambox at that M3U (or the per-channel `/play/live/{id}.ts` URL). If the box still struggles, raise `gop` or drop the bitrate — the fields are all editable in the FFmpeg tab.

> **Upgrading an existing install:** built-in presets are re-seeded on every boot now, so pulling this change and restarting the container adds the Dreambox template and refreshes the built-in commands automatically (your own edits are kept).

**Identity of the outgoing ffmpeg request — two user-agents, like a real MAG box.** A MAG contains two HTTP clients with *different* identities: the stbapp **browser** that talks `portal.php` (the `Mozilla/5.0 (QtEmbedded…)` MAG200 UA), and its embedded **media player** — an old libav build that fetches the resolved `play/live.php` URL announcing exactly `Lavf53.32.100`. The proxy mirrors that split:

- portal/API calls (handshake, lists, `create_link`, link resolution) always carry the portal **browser** UA;
- for `http(s)` ffmpeg inputs the manager injects `-user_agent "Lavf53.32.100"` (the MAG **player** UA) and `-referer "<stream origin>/"` in front of `-i` — unless the template sets them itself;
- if the origin answers that first media request with a pre-first-byte HTTP 4xx (**456** is the classic Stalker/WAF "unrecoverable" answer; 403/429 too) and sends zero bytes, ffmpeg is respawned **once with the browser UA**, and whichever identity produced bytes is remembered per origin host from then on. A silent stall/timeout is *not* retried that way;
- **fast vs. slow 456:** a WAF that refuses the client shape answers in milliseconds (identity answer → UA rung); a Stalker backend that returns 456 because the MAC's single connection **slot is still held** (zap overlap, or a *playlist demo* run on that MAC in the last minute or two) thinks ~5–7 s before answering — the same request plays fine on another MAC (verified in production traces). A refusal slower than `SPM_UA_LADDER_FAST_FAIL_S` (default 5 s) is treated as slot-related, skips the UA rung and goes straight to the next MAC/source, so the ladder never delays the existing slot fallback by ~6 s per dead attempt.

Why the ladder instead of one hardcoded value: ffmpeg's own default `Lavf/61.x` (no referer) is refused with 403/405 by many panels, the browser UA is refused with **HTTP 456** by `play/live.php` origins and the anti-proxy WAFs in front of them ("redirect/direct plays, every ffmpeg template fails with rc=8 and 0 bytes"), and a few panels do the reverse and refuse any bare `Lavf` — the player→browser ladder plays all three. Set `SPM_STREAM_UA_LADDER=0` to restore the legacy browser-only behaviour; override the player value with `SPM_PLAYER_UA`. The detail-popup **stream probe**, the redirect liveness check and the FFmpeg-tab *playlist demo* walk the same ladder, so they report what the stream path actually gets. The *playlist demo* additionally resolves its link through a MAC that is **not streaming right now** (a MAC that holds a stream holds the panel's single connection slot and 456s a second concurrent link — running a demo beside a playing box used to end as a bare `rc=8, no output`): the result names the MAC it used (`mac=…`), and with no free MAC it says so instead of asking the busy one. A `-user_agent` you write into a template always wins and opts out of the ladder entirely.

At boot the app performs a **hardware sanity check**: if the selected default needs VAAPI/QSV but its device is absent, a warning is logged. Your default selection is not silently changed. Map the GPU device or explicitly select *Redirect*/*Copy* in the FFmpeg tab before using that fallback.

---

## Fallback engine semantics

- Every MAC streams **at most one channel at a time** (typical Stalker limit); occupancy is tracked centrally, busy MACs are skipped instantly.
- Per play request the ordered chain is walked (source priority → MAC order); a `global setting` decides whether *all MACs of a portal are tried before moving to the next portal*.
- **A redirect lease belongs to the user who took it.** A 302 play cannot see the player stop, so the MAC stays marked busy for `SPM_REDIRECT_LEASE_S` (default 180 s). That guess must not be held against the *same* user: Enigma2 zaps fast, and skipping "the channel this box just left" sent the next channel to a worse MAC (the `mac … busy -> skip` of the reported log). The same user now takes the lease over — logged as `taking over the redirect lease … (the channel this zap left)` — while a *different* user's MAC stays off-limits and an ffmpeg pipe (a real concurrent stream) is never taken over by anyone.
- No data within 12 s (configurable) or an ffmpeg exit → next step; when the chain exhausts, the client gets a clean end-of-stream and the GUI log shows every step. An ffmpeg that dies *before* the first byte (bad URL, 405, missing GPU) is detected immediately — the log then says `ffmpeg exited rc=8 before sending data`, and a *silent* stall now carries ffmpeg's own stderr tail (`| ffmpeg's last words: …`: VAAPI init, a 4xx on the media request, a panel slot check) instead of leaving the reason to guesswork.
- **The chain has a budget, and the 502 follows it.** `SPM_STREAM_START_BUDGET` (default 75 s) caps the whole walk; the first-chunk guard waits for that budget plus slack, so the engine is never cut off while it is still working. `candidates × 12 s × passes` is 50 s for a two-MAC chain (74 s for three) — the old fixed 25 s guard fired mid-chain, which is how a request ended in `produced no data within 25s -> 502` *and* never ran the zap retry. When the budget *is* spent, the log and the 502 name what was tried: `produced no data within 75s - start budget of 75s spent after 4 attempt(s) | nexus/00:1A:79:00:20:6D: silent 12s; …`.
- **Link repair:** some panels rebuild the `create_link` answer instead of echoing it and lose parameters on the way (`&stream=392166` → `&stream=`). The request is stripped of its stale `play_token` before asking, and the answer is repaired against the request (missing/blanked parameters restored, the fresh token always wins). See `dev/check-links.py`.
- **A `create_link` answer is read in every shape.** A panel that has to choose a storage — or that inserts an advertisement — answers with a *list* of candidates instead of one object. The first entry the panel did not label an ad is the stream, the `storage_id` it came with is kept for the log, and a list of nothing but ads is reported as "no link" *with that sentence in the error* — not as a dead channel and not by playing the advertisement. A reader that only knew the object shape reports every item such a panel serves as `no_url`, which is the failure that looks least like what it is.
- **Two cmd forms of one VOD file.** Some panels list a movie as `/media/1234.mpg` and answer `create_link` only for `/media/file_<id>.mpg`, where the id is what `get_ordered_list&movie_id=` reports — and they refuse the catalogue form with `nothing_to_play`, byte-for-byte what a dead item looks like. Only a refusal that *can* mean "wrong form" (`no_url`, `nothing_to_play`, `link_fault`) triggers the retry: the file id is resolved, the cmd rewritten, the request repeated once, and the form that worked is remembered on the source row (`media_cmd`), so the next play asks with it directly instead of paying for a refusal and a resolution again. A re-fetch never wipes it, the catalogue `cmd` stays the panel's truth, and it is the fallback when a learned form goes stale (a re-ingested movie gets a new file id) — the row then learns the new one. `SPM_MEDIA_CMD_REPAIR=0` switches the retry off; an absolute URL is never rewritten, because that is a link and not a storage reference.
- **Portal refusals keep their code.** A panel rarely answers a refusal with a 4xx: the usual shape is HTTP 200 + `{"js":{"error":"limit"}}`. `PortalError.code` carries it through to the fallback log, so `limit` / `account is in use` (→ *this MAC is busy over there, try the next one*) are never mistaken for `nothing_to_play` or `link_fault` (→ *this source is gone*). A bearer the panel expired — also a 200 + `{"error":"token"}` — triggers exactly one transparent re-handshake and retry, like a 401 does, instead of looking like a broken portal until the local token TTL runs out.
- **`%mac%` in a resolved link is filled in.** Portals that keep one link template for every box hand out `…/ch/%mac%/1234.ts` and expect the set-top box to substitute its own MAC; anything else is a 404 that reads as a dead channel.
- **HLS links get the input options they need.** When the resolved link is a `.m3u8` playlist, ffmpeg receives `-protocol_whitelist file,http,https,tcp,tls,crypto` and `-allowed_extensions ALL` (unless the template already sets them) — without those two, a valid portal playlist dies before the first byte.
- **Occupancy is a hint, never a veto.** Our own bookkeeping (an ffmpeg pipe, a post-302 lease) decides *which* MAC a start prefers; it must never be the reason a channel refuses to open. A player that zaps fast asks for the new channel while the old one is still being torn down, so `open()` waits `SPM_BUSY_WAIT_S` (3 s) for a MAC and takes back the *same user's* own previous stream (`taking over the ffmpeg pipe …` — the pipe path now does what the lease path always did; another user's stream is never touched). If everything is still busy the answer is **503 + `Retry-After: 2`**, not 404/502: the truth is "come back in a second", and players retry a 503. A candidate that just failed is tried last for `SPM_FAILURE_DEMOTE_S` (120 s), outranking route affinity, and a live link resolved in the last `SPM_LINK_CACHE_S` (90 s) is replayed for the next 302 (probed first) so zapping away and back costs no `create_link` at all. The same reasoning covers the panel's *own* slot: `limit` / `account_is_in_use` / HTTP 456 no longer trigger a re-handshake (the bearer is fine) and are re-asked on the same MAC after 0.5/1/2 s (`SPM_BUSY_BACKOFF`) before the chain moves on — on a single-MAC portal that is the only candidate there is. `Portals → Streams per MAC` raises the per-MAC concurrency for a panel that really allows two or three links on one account.
- **The connection to the panel is kept, not rebuilt.** A zap's `create_link` is a small request, but if the connection to the panel had to be built first it was paying a TCP+TLS handshake (two round trips on a WAN panel) before the panel saw anything — httpx closes an idle connection after 5 s by default. A pooled portal session now keeps it for `SPM_PORTAL_KEEPALIVE_S` (90 s), the way a real set-top box does. Same trust policy, no new sockets per zap, and it also takes load off a panel that counts connections.
- **A multi-MAC portal takes the free MAC first.** With several MACs on one portal the chain is walked like the reference proxy walks it: the first MAC nothing holds (no pipe, no post-302 lease) is used, and the MAC this user just left is the fallback. That matters because a panel still counts the connection it is letting go of: reusing the just-leased MAC costs a refused `create_link` (or a busy-ladder wait) while a second MAC's slot is free right now. Ranking is untouched MACs → this user's own lease → somebody else's stream, and within a rank nothing changes, so route affinity still decides between equals and the zap-back cache keeps working. `Settings → Zap takes a free MAC first` = off restores the older rule (take back the MAC that just played), which is the better choice when the portal's other MACs are the unreliable ones. `Settings → Fallback order strategy` has to be `macs_first` for this to be possible at all: `portal_first` uses one MAC per portal per pass, so a zap asks the same MAC every time — SPM logs a warning when that combination appears (`fallback strategy 'portal_first' uses 1 of this portal's N MAC(s)`).
- **`create_link` is asked only when it has to be asked.** A channel whose own flags (`use_http_tmp_link`, `use_load_balancing`) say its link is permanent, and whose stored `cmd` is a complete `http(s)` URL with no session token in it, is handed to the player as-is: a redirect play of such a channel costs the portal **zero** requests (no handshake, no token, no link). Every other case asks — and since R2b the ffmpeg path takes the same shortcut, it used to mean "ask, always", on the argument that the request doubles as ffmpeg's fresh `play_token` and as the liveness answer the chain walks. True for the channels that need it, and those are exactly the ones the rules above still send to `create_link` (temporary links, a template `cmd`, a URL that still carries a session token, no URL at all); for a permanent link it cost a portal round trip *and* a slot allocation on every play, which is what made a fast zap collide with the panel's connection table. `SPM_FFMPEG_USE_STORED_LINK=0` restores the old behaviour, which is also worth having for a panel that publishes permanent-looking links it then refuses. `Portal.direct_links` (on by default) turns the shortcut off for a panel whose flags lie. The stream log says which rule fired and why.
- **One trust policy for every outbound call.** Portal, EPG and logo fetches all go through `app/services/http_client.outbound_client()` (OS CA store, verification on). `Portal.tls_insecure` is the only opt-out, it is per portal, and it is part of the pooled-session key so flipping it cannot leave an old session behind.
- **The panel's own account state is honoured.** Per MAC the panel reports `blocked`, `status` and an expiry, and Check Portal / the multi-MAC health scheduler store that verdict: `banned` and `expired` MACs are dropped from fallback chains and from a fetch job's starting MAC, the Portals tab shows the badge (plus `last_checked`), and the *reason* the portal gave is on the MAC row (`last_error`, shown in the badge tooltip). `offline`/`error` — our transport verdicts — stay retryable, because a portal that timed out is not a portal that said no.
- Client disconnects (and the Dashboard *kill* button) deterministically free the MAC and kill ffmpeg via a disconnect watchdog.

---

## What the portal is told about the box (STB identity)

A Stalker portal does not authenticate a user, it authenticates a *set-top box* — so a
proxy that announces itself as `python-httpx/0.28` gets a token and a playlist, and then
either 403s on the stream or starts behaving in ways no box ever does. Every portal request
carries a box identity, per MAC and derived from the MAC itself (`md5(mac)` for the
serial, `sha256(mac)` for the device id, …) so it is **stable across restarts and unique
per MAC** — nothing is stored on disk, and two MACs never look like the same box. The
default is the minimal profile (bare `sn`/timestamp, empty device id, no signature); a
portal can opt into the full MAG250 fingerprint when its panel demands it:

- **The full handshake dance.** Some panels answer the first handshake with
  `{"js":{"msg":"missing"}}` plus a random seed and expect a *second* request carrying
  `mac=` and `prehash=sha1(<the bearer we just invented>)`. Both steps are performed, and the
  `Authorization: Bearer` header and the `token=` cookie are set together, as the stalker
  app does.
- **`get_profile` with the device fingerprint** (MAG250 mode: serial, device id, signature,
  hw versions, `api_signature=262`, a `metrics` blob quoting the random seed), and
  `not_valid_token` echoed from the handshake. The answer's `blocked` / `status` /
  `force_ch_link_check` are consumed (see above). A panel that answers nothing usable
  falls back to the minimal `sn`/`device_id`/`timestamp` request, because some panels 403
  the full one.
- **Headers and cookies on every request, including portal *discovery*:** the MAG200
  `User-Agent`, `X-User-Agent: Model: MAG250; Link: WiFi`, `Referer: <portal>/index.html`,
  and `mac=…; stb_lang=en; timezone=<yours>` cookies (plus `adid=<md5(sn+mac)>` for
  `/stalker_portal/` panels). A wrong or missing cookie MAC is a 403 on *any* action, so the
  `mac` cookie is always in colon form and never rewritten.

Per portal you can tune what it is told, in the portal dialog or via the API:

| Field | Default | why you would change it |
|---|---|---|
| `identity_mode` | `minimal` | `mag250` sends the full device fingerprint to `get_profile` — only for panels that withhold data until they see the box they enrolled |
| `stb_timezone` | `Europe/Amsterdam` | what the box's `timezone=` cookie says; some panels key content or sessions on it |
| per-MAC `sn` | derived | the box's **real** serial, if you captured one (see below) |
| per-MAC `device_id` | derived | same, for `device_id`/`device_id2`/`signature` |

To pin a real serial: **MACs → ⚙ → 🎩** on the MAC's row, then type
`SERIAL123, DEVICEID456` (device id optional). A panel that has seen the box's real serial
once will notice a changed one, so pinning it is what makes moving a subscription to this
proxy invisible.

Environment knobs, when a panel wants something different again: `SPM_STB_UA` (the portal
**browser** identity used by the portal calls and link resolution; it only reaches a
stream origin as the media ladder's second rung), `SPM_PLAYER_UA` (the MAG
**player** identity ffmpeg presents to stream origins first, default `Lavf53.32.100`),
`SPM_STREAM_UA_LADDER=0` (restore the legacy behaviour of sending the browser UA on the
media request only), `SPM_UA_LADDER_FAST_FAIL_S` (seconds; a 4xx arriving slower than
this is treated as a panel connection-slot refusal, not an identity refusal, so the UA
rung is skipped in favour of the next MAC/source; default 5),
`SPM_STB_MODEL`, `SPM_STB_IMAGE_VERSION`, `SPM_STB_HW_VERSION`, `SPM_STB_VER` (the whole
`ver=` ImageDescription block), `SPM_STB_PORTAL_VERSION`, `SPM_STB_LANG`, `SPM_STB_TIMEZONE`. `SPM_STB_PROFILE=0` stops the `get_profile` call
entirely (handshake and play links keep working) — for the panel that treats an unexpected
`get_profile` as a reason to be rude.

---

## What the portal says about itself (version, modules, links)

Pressing **Resolve** on a portal does three things beyond finding the working `portal.php` path,
and all three are stored on the portal so the answer survives a restart and reaches a backup:

- **`version.js` is scraped.** No token is needed for a static file, so it is read during discovery
  — the one moment we know for sure the panel is answering us — and the badge says
  `Ministra portal 5.4.2 image 0.2.20-r3-250`. It is the single most useful line in a bug report
  ("the playlist works but the EPG is empty" is a different question on 5.2 than on 5.4), and the
  resolve box shows the `Referer:` every later request will claim and the winning path prefix sits
  in the table next to it, so "it works but why" is answerable from the GUI instead of from tcpdump. A body that looks like
  HTML is *rejected*, not parsed: a captive portal or WAF serving that file has `ver = '…'` in its
  markup, and printing a fragment of it as a version would send you off debugging your panel.
- **`type=stb&action=get_modules` is asked** (it needs an authenticated session, so it happens after
  the handshake, from the same click). `all_modules − disabled_modules` is kept on the portal, and a
  module that is absent **gates the work that would fail anyway**: a portal with no `sclub` gets no
  series genre sync and no series item fetch — the fetch log records `series categories skipped: the
  panel says it has no sclub/series module (get_modules offered: tv, vclub)` instead of a progress
  bar that ends in an empty catalogue. A portal that never answered is treated as *unknown* and is
  fetched in full: skipping a catalogue because a cosmetic probe did not reply is how a proxy
  "loses" channels nobody removed.
- **Per-channel link flags are stored on each live/VOD/episode row** (`link_flags`:
  `use_http_tmp_link`, `use_load_balancing`, `disable_ad`, as a readable comma list rather than four
  bits nobody can interpret — so a `curl` of `/api/sources/live` answers "why does *this* channel
  skip the portal"), and the stream path reads them as described under *Fallback engine
  semantics*. A row fetched before this existed carries NULL = "never told", which asks every time —
  exactly the old behaviour — and the flags arrive with the next fetch.

| Field | Default | why you would change it |
|---|---|---|
| `direct_links` | on | off forces `create_link` before every play, on both paths: for a panel that reports its links as permanent and rotates them anyway |

The Portals table gained a **Panel** column for this (version badge · "N modules" with the list in
its tooltip · `no vclub` for what the panel switched off · "capabilities unknown" · "press Resolve"),
because "why is this portal's VOD empty" should be answerable without opening a container shell.

---

## When the portal is an Xtream account in disguise

Some Stalker panels are a thin front on an ordinary Xtream/ministrales account, and they admit it in
the one place nobody reads carefully: the `create_link` answer. Instead of a token URL the panel
returns `/live/john/s3cr3t/12345.ts` — the same credentials the panel's own `player_api.php` accepts.
An app that has those needs no MAC session to play a channel: no handshake reuse, no token, no
`create_link` per channel switch, no connection slot held by a fallback chain, no
`mac_locks` entry. That is a lot of failure surface removed — and it is also somebody's paid login,
so this app only ever **detects and offers**.

- **Detection is one request, on purpose.** `Portal → Check Portal` (or the *Xtream bridge* panel in
  the portal editor, "Detect") asks for a single stream link, reads the credential out of it and —
  only then — queries `player_api.php` for `status`, `exp_date`, `created_at`, `active_cons`,
  `max_connections`, `is_trial`. VOD is asked first and live second, like EStalker does, because a
  movie `cmd` is the plain file path panels sign. The base is the origin **plus whatever path prefix
  the panel put in front of the credential segment** (`http://h/xtream/live/u/p/1.ts` → ask
  `http://h/xtream/player_api.php`), because a server that streams from behind a prefix answers its
  API there too, and stripping to the origin — what EStalker does — sends the harvested password to a
  vhost that never issued it. The result is stored on the portal
  (`Portal.xtream` + `xtream_at`) with `player_api.php`'s `server_info.http_live_url` **outranking**
  the origin in the link: a panel that streams from `:8000` and serves the portal on `:80` is common,
  and a bridge that used the portal origin would write URLs that 404 forever.
- **Nothing changes until you press Adopt.** `xtream_adopted` is a separate column, and playback reads
  only that. Detecting an account and rewriting how a household watches TV are two decisions, and the
  second one is yours: some operators deliberately do not want a long-lived credential in the stream
  path, and a panel that hands out `/movie/<u>/<p>/` links today may rotate the account next month.
- **Adoption matches, it does not guess.** `get_live_streams` / `get_vod_streams` (one request each)
  are matched against the channels already in the database: **channel number first, then the title**,
  and a title that more than one stream shares is reported `ambiguous` and left alone rather than
  resolved by coin flip. Every row is rewritten by a re-adopt, including to *nothing* — a channel the
  panel dropped must lose its Xtream URL, or the next adopt looks like a fix while the user still
  gets yesterday's stream id. The API returns the counts (`matched`, `by_number`, `by_name`,
  `ambiguous`, `unmatched`) so "why is 1 of 40 channels still asking" is answerable.
- **The portal link is never destroyed.** Adoption writes `xtream_url` *beside* `cmd` and `link_flags`
  on the live/VOD row, so Detach is one flag — and it keeps the URLs by default (`…/xtream/detach`)
  so switching back does not cost the panel another catalogue walk; `?clear=1` drops them. Series are
  out of scope on purpose: a per-episode mapping needs `get_series_info` for every title, which is a
  different trade than "one request, whole catalogue".
- **An adopted play** skips `create_link` and the busy-MAC check entirely, takes no per-MAC lock, and
  shows up in *Active streams* as `Portal (xtream)` with an empty MAC. It is also the one documented
  **exception to "an ffmpeg template always asks"**: there is nothing left to ask for, since the
  harvested URL *is* the stream the panel would have built. If it fails, the chain moves to the next
  source instead of re-asking the panel through a MAC.
- **Adopt refuses a bad account**: `expired`, `banned`, or a `player_api.php` that would not confirm
  the credentials (including the classic `{"user_info": []}` answer to a wrong password). `?force=1`
  overrides it for a panel that lies about its own state — the same escape hatch `status` needs, and
  equally not a default.
- **The password is stored, masked everywhere else.** `Portal.xtream` keeps it in full, because a
  bridge whose secret was stored as `****` would restore from a backup as a silently dead portal;
  every API response, tooltip and log line masks it (`…/john/mo***…`), and `/api/export` carries it
  unmasked for the same reason — so that file is a secret-bearing backup, like the user list in it
  already is. A credential that looks like a 32-hex `play_token` is **refused**, not adopted: that is
  a link signature, and adopting it builds a playlist that dies at 3 a.m. and then re-asks a panel
  that is refusing it — which is how an IP gets banned.

| Endpoint | Effect |
|---|---|
| `POST /api/portals/{id}/xtream[?force=1]` | detect (one `create_link` + one `player_api.php`); stores, changes no playback |
| `POST /api/portals/{id}/xtream/adopt[?force=1]` | fetch both stream lists, match, write `xtream_url`, set the flag |
| `POST /api/portals/{id}/xtream/detach[?clear=1]` | clear the flag (and optionally the per-channel URLs) |

In the GUI this is one **Xtream** column in the Portals table (`offered` / `adopted` / `—`, with the
account line in its tooltip) and a panel in the portal editor with the Detect / Adopt / Detach
buttons and their two checkboxes. `xtream_adopted` is deliberately *not* a field of `PUT
/api/portals/{id}`: a boolean flipped on its own would leave playback depending on per-channel URLs
that endpoint cannot maintain.

---

## Input Sources → Live: the "Custom Channel Name" column

The old **Now** column on *Sources → Live* asked the panel (`get_short_epg`) once per visible
channel and made paging the list expensive. It is gone. In its place, between **Channel** and
**Portal**, sits the **Custom Channel Name** column (it was briefly headed just "Playlist", which
read like a link to the Playlist *tab* rather than an editable name):

- shown only for **enabled** channels (disabled rows stay blank);
- enabling a channel immediately adds it to the live playlist using the portal's original channel name;
  if that custom name already exists (case-insensitive), the source is appended to its fallback chain;
- the resulting primary/fallback name appears in this column as soon as the switch completes;
- edit the cell (blur / Enter) to set the custom name used in the final M3U / Xtream output:
  - **unique name** → a new custom live channel is created (or the channel this source already owns as primary is renamed);
  - **name already used** (case-insensitive) → this source is attached as a **fallback** on that existing custom channel.

It is also the **wide** column of the two: the portal's own **Channel** name next to it is
read-only reference and is kept narrow (ellipsised, full text in the cell tooltip), so the field
you actually type in gets the room.

Beside the channel name, **Custom Group** shows the same group as Playlist → Live. It starts with
the source genre when the channel is added and can be edited with the same first-click-select,
blur/Enter-to-save interaction. A custom channel has one shared group, so editing it from either a
primary or fallback source updates every source row on that channel and the Playlist tab.

The list payload carries the placement (`playlist_id`, `playlist_name`, `playlist_group`, and the
primary/fallback badge) in the same `/api/sources/live` response — no extra round trip per page.

---

## Backup, additive restore & data deletion

Open **Settings → Backup & restore**:

- **Download full backup** saves every column of all **31 database tables** in one
  version-2 JSON file. This includes every stored setting key (not just the visible
  form fields), EPG sources/channels/programmes, logs, and all catalog/configuration
  tables and relationship tables.
- **Choose tables & settings** supports any individual table, several selected tables,
  or one stored setting. Referenced parent rows accompany a table automatically, so
  foreign keys can be rebound on another installation with different database IDs.
- **Restore**: select a JSON file, choose everything in it, one table, or one setting,
  then click **Review restore**. The v2 dry run reports additions and existing records
  per table without saving changes. Tick the required confirmation that **only
  additional information is added**, then click **Add missing information**.
  Existing identities and setting values are **never overwritten or deleted**;
  missing child links can still be added. Repeating a restore does not duplicate
  records. Invalid references or database conflicts roll back the entire v2 restore.
- **Settings → Delete stored data** offers one table, one setting, or everything in
  the database. **Review deletion** lists dependent rows that will also be deleted
  and rows whose references will be cleared. Confirmation requires both a checkbox
  and the exact phrase `DELETE SELECTED` or `DELETE EVERYTHING`. This clears data,
  not the schema. Take a backup first; deletion is permanent.

**Boundaries:** these are database backups, not a filesystem/container image. Media,
custom uploaded favicon files, disk caches, Docker/environment configuration and the
GUI admin credentials configured in the environment are not included. Back up those
separately. Active-stream rows are exported for reference only and skipped on restore:
processes/streams cannot be resumed from JSON. Stored paths and device settings may
need adjusting after moving to a different host. The favicon *selection* is a stored
setting; its uploaded image file is separate.

**Maintenance:** stop streams and finish queued/running fetch jobs before deletion
(the API refuses deletion while either is active). Avoid concurrent catalog edits,
scans and refreshes during maintenance. Background tasks may repopulate logs/EPG data;
built-in templates and default settings are seeded again at startup. Deleting a setting
immediately makes its environment/application default apply. To restore a backed-up
value over a current/default value, deliberately delete that setting first, then restore
it—restore itself never overwrites it.

**Compatibility:** the GUI still accepts old v1 section backups, with an explicit
legacy warning. Their original omissions/non-portable references cannot be recovered
from the file. The legacy `GET /api/export?section=...` format remains for older clients;
use the new v2 interface for complete backups. `POST /api/import` now uses additive
semantics for sources, playlists and settings too; the old overwrite behavior is gone.

All data-management endpoints require the admin session. Backups contain **unmasked
passwords, MAC identities and tokens**: do not share or commit them.

| V2 endpoint | Purpose / JSON body |
| --- | --- |
| `GET /api/backup/catalog` | All table counts and stored setting keys (no setting values) |
| `POST /api/backup/export` | `{}` for full backup; or `{"tables":["settings"],"setting_keys":["logo_country"]}` |
| `POST /api/backup/preview` | `{"data": <backup>}`; optional `tables` / `setting_keys` narrow the scope |
| `POST /api/backup/restore` | Same body plus `"confirm_add_only": true` |
| `POST /api/backup/delete-preview` | `{"tables":["portals"]}`, individual settings, or `{"all":true}` |
| `POST /api/backup/delete` | Same selection plus `"confirm_delete":true` and the exact `confirmation` phrase |

Regression coverage: `tests/test_backup_complete.py` populates **every table and column**,
round-trips onto a different ID space, tests every table individually, validates additive
behavior and rollback, and checks deletion cascades and admin authorization. Run:

```sh
.venv/bin/python -m pytest -n 0 tests/test_backup_complete.py tests/test_backup_sources_playlists.py tests/test_favicon.py
# Optional DOM integration checks (development only; does not change runtime dependencies):
npm install --no-save --package-lock=false jsdom
node tests/backup_gui.cjs
```

---

## Browser tab icon (favicon)

Every page of the GUI — dashboard, portals, playlist, **and the login screen** — carries a tab
icon, and *which picture* it is, is a setting rather than a hard-coded file:

* **Settings → Browser tab icon (favicon)** shows seven built-in pictures (broadcast, satellite
  dish, TV screen, play, signal bars, antenna tower, minimal dot). Click one; it is live on the
  next page load, no restart.
* **Use my own picture** uploads a `.png` / `.svg` / `.ico` / `.jpg` / `.gif` / `.webp` (max
  512 kB). It is stored on the **config volume** (`/config/branding/`), not inside the image, so a
  `docker pull` of a new build keeps it. The trash button deletes it again and the selection falls
  back to a built-in.

Under the hood the choice is one `settings` row (`favicon`), so it rides along in a
*Settings* backup and is restored with it. `/favicon.ico` (and `/apple-touch-icon.png`) serve the
active picture and are deliberately public — a browser asks for them on the login page too,
before there is a session. The `<link>` tags carry a `?v=<fingerprint>` that changes with the
picture, because a browser otherwise keeps showing the previous favicon roughly forever.

Uploads are validated: unsupported extensions and oversized files are refused with a readable
message, and a **scripted SVG is rejected** (`<script>`, `on…=` handlers, external entities) —
a tab icon is reachable as a document at `/favicon.ico` on the admin origin, so it must not be
able to run anything. What is served is additionally sent with `nosniff` and a
`default-src 'none'; sandbox` CSP.

---

## Built-in mock portal (testing without a real subscription)
`SPM_MOCK_PORTAL=1` mounts a fake Stalker portal at `http://<host>:8880/mock/c/` with MACs `00:1A:79:AA:AA:01` / `…02`, expired `…BB:BB:01` and blocked `…CC:CC:01`, 3 live genres × 4 channels, 2 VOD genres × 12 movies, 2 series genres × 6 series × 3 seasons × 5 episodes. `POST /mock/_control {...}` emulates what the portal can do to you, so the client's behaviour is testable without a subscription: `offline`, `slow`, `max_per_mac`, `http_status`, `require_prehash` (demand the second handshake step), `fingerprint_required` (403 a `get_profile` with no serial), `profile_mode` (`full`/`no_id`/`none`), `not_valid` (short-lived token), `token_rejects` (answer 200 + `{"error":"token"}`), `js_error`, `empty_reply`, `corrupt_stream`, `require_tls`, `require_host`, `reject_no_cookie`, `reject_no_referer`. `version_mode` (`full`/`none`/`html` — the last one is a captive portal answering `version.js`), `modules`, `modules_disabled` and `no_modules` (404 the action, which means *we do not know*, not *it has nothing*) emulate what the panel *says about itself*, `xtream_mode=1` makes its `create_link` answers carry `/live/<user>/<pass>/…` (with `xtream_user`/`xtream_pass`, `xtream_status`, `xtream_exp_days` and `xtream_refuse` — the last answers `player_api.php` with `{"user_info": []}`, i.e. "wrong password"), `epg_mode` (`normal`/`empty`/`absent`/`flaky` — busy twice then fine) is what "no guide", "no such action" and "try again" look like separately, `create_link_list` (`one`/`ad`/`ads_only`) answers `create_link` with a LIST of candidates instead of one object — an advertisement first, or nothing but advertisements — and `media_form=file_only` (with `media_file_id`) is a panel that refuses the generic `/media/<id>.mpg` cmd its own catalogue lists and answers only `/media/file_<id>.mpg`, resolving the id through `get_ordered_list&movie_id=`. `GET /mock/player_api.php` serves the Xtream account, stream lists (with the two flaws the matcher needs: a channel that is not on the Xtream side, and a duplicated `Sky Sports` name) and media at `/mock/live/…`, `/mock/movie/…`, `/mock/series/…` **with the credentials enforced** — under `/mock/`, never at the root, because `/live/<u>/<p>/<id>.ts` and `/player_api.php` are *our own* Xtream output API and a mock route there would be shadowed by it (a lesson learned from the demo 403ing while every test passed), and `get_short_epg` renders its schedule in the timezone from the `timezone=` cookie so the identity→guide chain is exercised rather than assumed, and the live catalogue ships five deliberate link shapes — permanent, tmp-link, load-balanced, no flags at all, and a "permanent" URL that still carries a `play_token` — so the conditional-`create_link` rules are testable instead of theoretical. `GET /mock/_state` answers with those settings *and* what the portal actually received (`seen_profile`, the handshake `prehash`es, `seen_create_link`, the `handshakes`/`version_calls`/`modules_calls`/`create_links`/`player_api`/`short_epg` counters, the `media_refusals`/`media_resolutions` pair that proves a learned cmd form is worth its column (both stay at zero on the second play), the `seen_player_api` query, and per-MAC usage) — which is how the identity and link tests prove a request arrived, or that one deliberately did not, instead of trusting the client.

Tip: the GUI shows the ready-to-copy mock portal URL/MACs in the Portals tab when enabled.

---

## Logs & observability

Every subsystem writes **detailed entries** (module-tagged: portals, fetch, stream, ffmpeg, …) both to the **GUI → logs pane** (level filtering) and to **stdout** (visible in Portainer). ffmpeg's stderr tail is captured on failures. Active stream rows show live throughput; a stream monitor persists recent finished/killed streams.

**Client disconnects are not errors.** A player that switches channel closes the
socket, and uvicorn/Starlette then cancel the *whole* request task - anyio keeps
re-delivering that cancellation every event-loop turn until the task is gone.
Anything awaited during teardown used to be interrupted halfway, which produced

```
ERROR [sqlalchemy.pool] Exception terminating connection <AdaptedConnection ...>
asyncio.exceptions.CancelledError: Cancelled via cancel scope ... by
<Task pending name='Task-158' coro=<RequestResponseCycle.run_asgi() ...>
```

and silently dropped the teardown writes (an `active_streams` row left behind as
a ghost on the dashboard, and no "stopped after N MB" line). Stream teardown now
runs through `app.database.run_uncancelled()`, log rows go through a writer task
instead of a session in the caller's task, and sessions hand their connection
back under a shield. If a client disconnects *while a query is in flight* the
connection cannot be trusted and is discarded - that is correct, and it is
logged as a single INFO line, not a traceback.

**Single-stream rule (do not break it):** *every* record - ours and uvicorn's - is written to **stdout** (`app/config.py` sets the root handler, `app/main.py` re-attaches uvicorn's handlers). The Docker logging driver keeps a container's stdout and stderr apart and `docker logs` re-emits them on *its own* two streams, so anything logged on stderr is invisible to `docker logs <c> | grep …` - which is precisely how the CI smoke test managed to fail six runs in a row while the app was healthy. `dev/smoke.sh` therefore asserts that the boot marker is present **on stdout**; keep that check honest by fixing the logging instead of muting uvicorn's output.

---

## When a channel stays black

Every step of a play request is logged, so start with:

```bash
docker logs stalker-proxy-manager 2>&1 \
  | grep -E "create_link|ffmpeg exited|playing via|no data|fallback"
```

| Log line | Meaning | What to do |
|---|---|---|
| `create_link -> http://…&stream=392166&…&play_token=***` | the resolved URL (token masked) | that is exactly what ffmpeg (or the player, in redirect mode) is given. To taste it, use `python3 dev/probe-link.py --read 8192 '<url>'` — **not** `curl -I`: a live-TS origin often cannot answer a `HEAD` at all and hangs up, which looks like a dead stream and is not (the unmasked URL is in the `Location:` header of `curl -sI 'http://nas:8880/play/live/<id>.ts?u=…&p=…'`) |
| `create_link: portal dropped parameters -> repaired from cmd` | the panel rebuilt its answer and lost a parameter (`&stream=392166` → `&stream=`) — it was restored from the stored cmd | informational; if it appears on every play, re-fetch the source so the stored cmd is clean |
| `[ffmpeg] … HTTP error 405 Method Not Allowed` / `Error opening input file …&stream=&…` | ffmpeg was handed an incomplete URL | update to a build with the link repair, or re-fetch the sources |
| `[stream] ffmpeg exited rc=8 before sending data` | ffmpeg could not open the source at all (dead link, 403/405, template needs a GPU that is not mapped) | read the `[ffmpeg]` line just above it — it carries ffmpeg's stderr tail |
| `[ffmpeg] … HTTP error 456` / `Server returned 4XX Client Error, but not one of 40{0,1,3,4}` then `[stream] … origin answered HTTP 456 to ffmpeg's media request … retrying once with the portal browser user-agent` | the origin's anti-proxy layer refuses the MAG *player* identity on the media endpoint (a 456 is its non-standard "unrecoverable" answer) — direct/redirect channels still play, because they are fetched by the end player and never hit ffmpeg | the second rung is automatic; if the channel then plays, nothing to do (the winner is remembered per origin). If both rungs fail, taste the fresh token with `dev/probe-link.py`, then `--browser`; a panel that pins another firmware build can be matched with `SPM_PLAYER_UA`, and a panel that only likes the browser UA gets legacy behaviour with `SPM_STREAM_UA_LADDER=0` |
| `[ffmpeg] … HTTP error 456` followed by `origin answered HTTP 456 after 6.5s - too slow for an identity refusal (usually: MAC connection slot still held)` | **not** a UA problem: the panel backend checked the MAC's single connection slot and refused because it is still held (the ~5–7 s deliberation is the tell; a WAF refuses in milliseconds). Common triggers: an FFmpeg-tab *playlist demo* ran on the same MAC a minute ago, or a rapid zap | automatic MAC/source fallback handles it; wait a moment and replay (the panel times slots out) — the UA ladder is deliberately *skipped* so it does not add ~6 s per dead attempt. If it happens constantly, add/online more MACs for that portal |
| FFmpeg-tab playlist demo: `✘ … ffmpeg exited rc=8 with no output — HTTP 456 is the panel's non-standard 'unrecoverable' answer …` | the demo's link was resolved/opened on a MAC that was streaming at that moment (the panel holds that MAC's single connection slot and 456s a second concurrent media link — the demo used to always take the portal's first MAC, which is exactly the one the box streams on), or — when no stream touched that MAC — the origin's anti-proxy layer is refusing this server's requests | the demo now skips MACs that are streaming (and MACs the portal says are banned) and names the MAC it used (`mac=…` in the result): if it says `no free MAC on …`, stop the channel on the box (or wait for the lease to expire) and re-run. If a **free** MAC still 456s, treat it as the identity row above: `dev/probe-link.py --read 8192` on the shown URL, then `--browser`; `SPM_PLAYER_UA` / `SPM_STREAM_UA_LADDER=0` are the levers |
| `[stream] no data within 12s from portal/mac \| ffmpeg's last words: …` | the portal accepted the request but sends nothing (MAC busy *on the panel*, expired account, IP/geo block) — ffmpeg's stderr tail now follows the same line, because "no data" alone is not a diagnosis | the per-MAC **Test** button in Portals answers "is this MAC in use?" against the panel; *Check Portal* for the account verdict; try another MAC of the same portal |
| `[stream] … portal said limit - connection limit for this MAC (panel says it is already streaming)` | the panel is right: that MAC already has a stream open (often a previous player that has not been timed out yet) | the chain moves to the next MAC on its own; if every MAC says `limit`, the panel's quota is the real limit |
| `[stream] … portal said nothing_to_play` / `link_fault` | the source is dead or the CDN is unhappy — retrying with another MAC cannot help | *Fetch Sources* for that channel, or drop it from the chain |
| `[stream] ffmpeg exited rc=1 before sending data` **only on VOD/series/local, live plays fine** | a template (usually one stored before the `-sn` fix) still maps subtitle streams: an SRT/ASS/PGS track in the movie aborts ffmpeg at output init before the first byte | re-save the template (fields side re-renders it with `-sn`), or let the spawn-time net handle it — a restart on this build fixes it without any action |
| `create_link: portal left a mac placeholder in the link -> filled in from our MAC` | the panel serves one template for every box and expects the client to insert its MAC | informational; the URL ffmpeg got already contains the right MAC |
| `[portal] TLS error / unable to get local issuer certificate` | the panel has a self-signed or incomplete certificate chain | tick **Allow broken TLS** for that portal (keeps every *other* portal verified) or fix the chain; do not disable verification globally |
| `[stream] [Ch] playing the stored link via portal/mac: the channel flags say nothing needs rebuilding…` | no `create_link` was asked, by design (see *Fallback engine semantics*) | if that channel is black, the panel lied about its links: tick **Play stored links when the panel allows** off for that portal, or re-fetch the sources |
| `[fetch] series categories skipped: the panel says it has no sclub` | the portal's own `get_modules` answer gated the fetch | informational; if the panel *does* have series, press Resolve to re-read the answer |
| `[output] … produced no data within 75s -> 502 … \| …: silent 12s; …` | the chain really was exhausted: every MAC/source the report names stayed silent for its whole start window. The number is the engine's budget (`SPM_STREAM_START_BUDGET` + slack), not a fixed timer, so nothing was cut off | read the listed attempts: `busy (…)` entries are MACs another viewer holds, `limit` is the panel's slot check, `silent` with a stderr tail is ffmpeg's own answer. Use the per-MAC **Test** button to see what the panel says about each account right now |
| `[output] user … exceeded max_connections` | a previous stream of that user was still counted when the player reconnected | raise `max_connections` for that user; the slot frees as soon as the disconnect watchdog notices the client is gone (≤0.5 s) |
| `[stream] … first pass … retrying once in 2.5s (zap overlap?)` | the box zapped while the panel still counted the old channel against the MAC's single slot (or our watchdog was still tearing the old pipe down) | informational — the open is retried once automatically; tune with `SPM_ZAP_RETRY_DELAY`, disable with `SPM_ZAP_RETRY=0` |
| `create_link: panel answered a list of 2 candidate(s) plus 1 ad(s), storage 7` | the panel chose a storage and offered an advertisement with it; the first non-ad candidate was played | informational — this is the answer shape a dict-only reader reports as "no playable URL" |
| `[stream] [Movie] this panel wants another cmd form for this item: '/media/1234.mpg' -> '/media/file_5678.mpg' - remembered on the source row…` | the panel refused its own catalogue cmd and answered for the `/media/file_` form | informational — the next play asks with the learned form (no refusal, no extra request). If the item fails again later, re-fetch the source: a re-ingested movie gets a new file id and the row re-learns it |
| `create_link returned no usable url … - the panel answered a list of 1 advertisement entry and no stream` | the panel offered an ad and nothing else | not a dead channel and not a parsing bug: that item is unplayable on this portal, so give the playlist row another source |
| `[stream] [Ch] redirect: fresh link dead (portal/mac; HEAD 404) -> next candidate` | redirect mode probed the link before the 302 and the origin answered with proof it is gone (404/410, a 4xx/5xx on the GET rung, or nothing listening at all) | the trace in brackets says which rung proved it. If the channel plays anyway when you paste the URL in VLC, the origin is probe-shy rather than dead — run `dev/probe-link.py` on it and, until that is understood, `SPM_REDIRECT_VALIDATE=0` |
| `[stream] [Ch] redirect: the origin at panel:80 is probe-shy (HEAD ReadError -> GET-no-range 200) - it refused the probe's request shape but answered a player-shaped one, so the link was handed out` | the validator had to climb its ladder before it could see the link is fine (here: the origin hangs up on `HEAD` *and* on a ranged `GET`, and answers a plain `GET`) | informational, logged once per origin and trace — this is the validator earning its keep instead of vetoing a working channel. A later `fresh link dead` from the same origin deserves a `dev/probe-link.py` run before you believe it |
| `[…] every MAC is busy right now` (**HTTP 503 + `Retry-After: 2`**) | every candidate this start could use was refused by occupancy — our own pipe/lease on the previous channel, another viewer's stream, or the panel's connection slot. Unlike a 404/502 this is the answer players retry | nothing to do: the box retries and wins the race. If it happens on *every* zap, the portal has too few MACs for the number of concurrent viewers (or raise **Streams per MAC** if that panel really allows more than one link per MAC) |
| `[stream] [Ch] taking over the ffmpeg pipe on this MAC held by <user> (the channel this zap left)` | the requester's *own* previous stream still held the MAC when the new channel was opened | informational — the old pipe is killed and the new channel starts (same rule the redirect lease always had; another user's stream is never taken over) |
| `[stream] [Ch] …: the panel still counts the previous connection (limit) - asking again in 0.5s` | the panel answered `create_link` with `limit` / `account_is_in_use` (or HTTP 456) because its own connection table has not let go of the previous stream yet (measured seconds on a real panel) | informational — the same MAC is re-asked (0.5 s → 1 s → 2 s) instead of burning the candidate; tune with `SPM_BUSY_BACKOFF` |
| `[stream] [Ch] redirect: replaying the link resolved 34s ago on … (no create_link)` | the same live channel was played recently, so the link from then is reused (probed first) | informational — this is the zap-back fast path (`SPM_LINK_CACHE_S`, default 90 s, live only) |
| `[stream] [Ch] playing the stored link via … (ffmpeg): … (ffmpeg plays it directly)` | the ffmpeg path found a permanent link in the source row and skipped `create_link` entirely | informational; tick **Play stored links** off for that portal, or set `SPM_FFMPEG_USE_STORED_LINK=0`, if a channel plays black because the panel's flags lied |
| `[stream] redirect: mac … busy (ffmpeg pipe or redirect lease) -> skip` | that MAC is streaming through ffmpeg (another viewer) or holds a post-302 lease owned by somebody else — the *same* user's lease is taken over automatically, logged as `taking over the redirect lease … (the channel this zap left)` | nothing to do for a same-user zap on this build; a *different* user's stream on the same MAC is real concurrency — give the portal another MAC (or lower `SPM_REDIRECT_LEASE_S`, default 180, if zapping is frequent) |

Two things the proxy does for you here: the outgoing `create_link` cmd is stripped of its stale `play_token` (panels that receive their own token back tend to mangle the answer), and ffmpeg presents the MAG box's embedded **player** user-agent (`Lavf53.32.100`, never its own `Lavf/61.x`) plus a referer for `http(s)` inputs, because the bare modern-libav identity is refused by quite a few panels with a 403/405 — and if the origin instead answers the player UA with **HTTP 456** and zero bytes (the usual anti-proxy answer behind "ffmpeg templates don't play while direct/redirect channels do"), the stream is automatically reopened once with the portal browser UA and the winning identity is remembered for that origin.

---

## Development scripts (`dev/`)

| Script | What it does |
|---|---|
| `bash dev/smoke.sh` | Runs the freshly built image with `SPM_MOCK_PORTAL=1` and checks: GUI answers `/login` (200), mock handshake returns a token, the boot marker is in the container log **and on container stdout** (the single-stream rule above). This is what the `docker` workflow's smoke job runs. `SPM_SMOKE_IMAGE` / `SPM_SMOKE_NAME` / `SPM_SMOKE_PORT` override it for a local run against any port. |
| `bash dev/smoke-puid.sh` | Bind-mounts a directory owned by a foreign uid (mode 750) and checks both sides of the PUID/PGID story: **without** `PUID`/`PGID` the app must *not* be able to list `/media`, **with** them it must (and PID 1 must run as those ids, `/config` must be chowned to them). Needs root/sudo; skips itself otherwise. `SPM_SMOKE_PUID_IMAGE` / `SPM_TEST_UID` override. |
| `python3 dev/check-links.py` | Pins the portal plumbing that decides whether a channel plays: the `create_link` URL rules (prefix stripping, stale-token removal, repair of a mangled answer), the STB fingerprint and account verdict, the link-flag policy table and the `version.js`/`get_modules` parsers — plus greps that no probe's answer is discarded and that the link policy is not re-inlined at a call site. No pytest needed, so it also runs on a NAS. Run it after touching `app/portal/`. |
| `python3 dev/probe-link.py '<url>'` | Runs the redirect guard's real probe ladder against a live URL and prints every rung — method, `Range` or not, status or exception, elapsed ms — plus the verdict, so `redirect: fresh link dead (…)` can be checked against what the origin really answers. `--read N` also fetches N bytes of a plain GET (what a player sends) and classifies them: MPEG-TS sync bytes, the panel's HTML error page, or something else. Needs only `httpx`, so it runs inside the built image; a `play_token` is short-lived, so probe a fresh one. |
| `node dev/check-js.js` | Syntax-checks the JavaScript inside every template's `<script>` block (and `app/static/js/app.js`) with the real parser, after a text-level Jinja pass that keeps one branch of each `{% if %}`. A broken template script is invisible to every Python test — the page renders, the API answers 200, and the table is simply empty. Needs `node`; skip it if your box has none. |
| `bash dev/check-yaml.sh` | Parses every workflow file (and `docker-compose.yml`) and verifies each `dev/*.yml.example` is byte-identical to the workflow it installs (`docker-publish.yml`, `ci.yml`). Run it before pushing anything under `.github/workflows/`. |
| `bash dev/seed-demo.sh [BASE_URL]` | Seeds a *running* instance with a full demo setup against the built-in mock portal (portal → genres → live/VOD/series → users). Idempotent; dev/mockup use (`SPM_SKIP_LOGIN=1`), default base `http://127.0.0.1:8880`. |

**YAML gotcha that silently disabled this whole workflow once:** a plain scalar may not contain `": "`, so step names must be quoted — `- name: "Image metadata (tags: latest, sha, semver releases)"`. Unquoted, GitHub reports *"mapping values are not allowed here"* and refuses the **entire file**: no job in it runs (build, push and smoke all vanish together), which looks like "the workflow stopped working" rather than a typo. `dev/check-yaml.sh` catches it before you push.

`dev/docker-publish.yml.example` and `dev/ci.yml.example` are full copies of their workflows, kept in sync on purpose: the repo's bot cannot commit under `.github/workflows/` (GitHub refuses a GitHub-App push that touches a workflow with *"refusing to allow a GitHub App to create or update workflow `.github/workflows/ci.yml` without `workflows` permission"*), so the copies are installed with

```bash
cp dev/docker-publish.yml.example .github/workflows/docker-publish.yml   # safe: byte-identical
cp dev/ci.yml.example             .github/workflows/ci.yml               # the test suite
```

---

## Architecture

```
┌─ FastAPI app (port 8880) ────────────────────────────────────────┐
│  GUI pages (Jinja + Bootstrap)   REST /api/*   mock portal        │
│  /play/*  /get.php  /player_api.php  /xmltv.php                   │
│                                                                   │
│  StreamManager ─ ffmpeg processes ─┐  Fetch jobs (background)     │
│   - fallback chains                │   - staged portal pulls      │
│   - MAC occupancy + watchdog       │                              │
│  SQLAlchemy  ──►  Postgres / SQLite (/config)                     │
└────────────────────────────────────┴──────────────────────────────┘
                ▲
      /dev/dri (Quick Sync, optional)
```

Single-process by design (MAC locks and stream registry are in-memory; the database keeps the durable mirror for the GUI).

## Development (without Docker)

```bash
pip install -r requirements.txt
SPM_ADMIN_PASSWORD=admin SPM_MOCK_PORTAL=1 \
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8880
```

Tests (no Docker, no portal, no ffmpeg required - the streaming tests substitute
a real subprocess for the ffmpeg binary and keep the rest of the pipeline real):

```bash
pip install -r requirements-dev.txt
python -m pytest          # 700+ tests in ~29 s on two cores (was ~103 s)
python -m pytest tests/test_stream_disconnect.py -v      # one file, one worker
```

(the last two lines of `requirements-dev.txt` pull in pytest-xdist and
pytest-timeout; `pytest.ini` configures both, so re-run that pip install in an
older venv or the run stops with "unrecognized arguments: -n")

What keeps it fast (and what to reach for when a run misbehaves):

| Knob | Why |
|---|---|
| `tests/conftest.py` builds each test's empty database by **deleting rows** instead of dropping and re-creating 31 tables: ~6 ms against ~58 ms per test, ~40 s of the suite. The schema is rebuilt only when a test actually changed it (the migration tests do), detected by fingerprinting `sqlite_master`. |
| `-n auto` (pytest-xdist, the default via `addopts`) spreads the tests over the available cores; each worker gets its own temp database, so they cannot collide. On a 2-core container that measured ~19 s with `-n 3` against ~29 s serial and ~30 s with `-n auto` - the bag of tests is uneven, so more workers are not automatically better. `-n 0` runs in-process (needed for `--pdb`). |
| `timeout = 120` (pytest-timeout) turns a deadlock into a *failed test* instead of a run that never ends. It is a safety net — the slowest test is under 1.5 s, so anything near the limit is a hang, not slow work. `--timeout=30` for a stricter run, `--timeout=0` to switch it off. |
| `app/database.py::run_uncancelled` hands back a cancellation that `asyncio.wait_for` swallowed when the shielded work finished in the same loop turn. Without that, the log writer parked forever (state CANCELLING, no exception anywhere) and the teardown that waits for it - `asyncio.Runner.close()` under xdist, uvicorn's shutdown in production - waited forever too. |
| Long `sleep`s in tests were shortened to the smallest value that still proves the same thing (a demo timeout is asserted at 0.5 s, a simulated portal page latency at 20 ms, a "silent ffmpeg" stub `exec`s its `sleep` so no orphan child holds the pipes and burns a 3 s reap wait). |
| `python -m pytest -q --durations=10` when you want to know where the time went; `-x --timeout=30` for a quick feedback loop while editing one module. |

Two failures on a clean checkout - `test_local_playback.py::test_ffmpeg_argv_injects_annexb_when_copying_to_mpegts`
and `test_stb_identity.py::test_an_existing_install_gets_the_columns_it_is_promised`
- are **pre-existing** (they fail on `main` too, and are unrelated to the
streaming/portal work); deselect them with
`--deselect tests/test_stb_identity.py::test_an_existing_install_gets_the_columns_it_is_promised`
until they are fixed.

### Continuous integration (GitHub Actions)

| Workflow | Trigger | What it does |
|---|---|---|
| `docker` (`dev/docker-publish.yml.example`) | push to `main`/`master`, `v*` tags | builds the image, pushes it to GHCR, then boots it with `SPM_MOCK_PORTAL=1` and runs `dev/smoke.sh`. |
| `tests` (`dev/ci.yml.example`) | **every pull request**, push to `main`/`master`, manual | installs `requirements-dev.txt`, runs `dev/check-yaml.sh` + `node dev/check-js.js`, then the pytest suite. ~1 minute warm, and it is the same command you run locally (`pytest.ini` still adds `-n auto` and the 120 s timeout). |

Install the test workflow once (the bot cannot - see the note in *Development
scripts* below; this is the GitHub web UI equivalent of copying the file:
repo → **Add file** → **Create new file** → path `.github/workflows/ci.yml` →
paste the contents of `dev/ci.yml.example` → commit):

```bash
cp dev/ci.yml.example .github/workflows/ci.yml
git add .github/workflows/ci.yml && git commit -m "CI: run the test suite on PRs" && git push
```

It runs on the next PR/push. Two tests are deselected *in the workflow* because
they fail on `main` too; delete those two `--deselect` lines once they are fixed
(a `--deselect` for a test that no longer exists is silently ignored, so they
cannot go stale). `dev/check-links.py` is deliberately **not** wired in yet: it
currently fails 5/166 on `main` (the EPG now/next endpoints), so it would turn
every run red - run it by hand after touching `app/portal/`.

## Phase 3 (done)

- **EPG auto-match**: add rytec/xmltv sources (plain XML, `.gz`, `.xz`) in Settings
  → they are downloaded server-side, every `<channel>` is indexed, playlist
  channels get fuzzy-matched automatically (empty `epg_id` only — manual
  overrides in the channel editor are never touched), and programmes of matched
  channels/mappings are ingested (now-78h … +7d interpreted UTC window, pruned automatically).
- **Merged XMLTV output**: `/xmltv.php?u=…&p=…` and `/epg.xml` now serve a real
  XMLTV document containing exactly the channels the authenticated user can see,
  with `<icon>`s and current/upcoming programmes (+48h). Use Users → Copy EPG
  URL to configure it in a player; no automatic M3U EPG-fetch header is added.
- **tv-logos matcher**: one GitHub tree call is cached as an index of the
  configured `logo_country` folder (+`countries/all` fallback); channel names are
  fuzzy-matched to logo filenames and the best raw.githubusercontent URL is
  written to the channel logo (Settings → Channel logos).
- **TMDB popups** for VOD/series metadata (GUI detail dialogs). Titles are cleaned before lookup (trailing years, `SxxEyy` tags and resolution/quality suffixes are stripped), and lookup problems — a rejected API key, no match, a failed request — are shown in the popup instead of a silent "no hit".
- **Xtream completeness**: real `get_vod_info` (playlist row + portal-source
  fallback), `timeshift` on live streams, episode `movie_image`+`tmdb_id`.
- **Mock end-to-end**: `/mock/epg.xml` ships a generated XMLTV feed for the mock
  portal channels so the whole EPG flow is demoable without internet access.
- **Config portability**: backup export now also carries `epg_sources`
  (import merges them, duplicates skipped).
- **Sources and playlists back up too** (Settings → Backup / restore, or
  `GET /api/export?section=sources|playlists`, both included in `all`):
  the full source catalog - live channels, VOD items, series with their
  seasons and episodes, local directories with their file rows - and every
  playlist kind (live, VOD, series, local) with group, number, number lock,
  epg id, logo, FFmpeg-template pick, order, fallback source chain and series
  season picks.
  Everything is written as NAME references (portal, genre, template,
  directory + path), so a backup restores onto another install. Importing
  sources/playlists now uses **add-only** semantics: missing rows are added,
  existing rows keep their current values, and nothing is deleted. Use the
  v2 Settings backup interface above for complete table/column coverage. Portal MACs (with
  serial, device id and password) are imported too, so a moved install keeps
  speaking the same identity to the panel. Local files back up their catalog
  rows only; the media themselves live on disk under the media root.
- **Areas and Enigma2 receivers back up too** (`?section=areas`,
  `?section=enigma2`, both included in `all`). Areas export their
  kind-default template picks and per-item exceptions by template name;
  Enigma2 profiles export every receiver setting plus the SPM user they
  carry (by name) and the opaque pull token, so a restored box keeps
  talking to its existing install URL. These legacy section exports omit some
  machine-state columns; the v2 table format includes every database column.

---

*Not affiliated with the Stalker Middleware project. Use only with portals/subscriptions you are authorized to access.*
