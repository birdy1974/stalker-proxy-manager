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
| `SPM_DATABASE_URL` | sqlite | `postgresql+asyncpg://user:pass@host:5432/dbname` to switch to Postgres |
| `SPM_ADMIN_USERNAME` / `SPM_ADMIN_PASSWORD` | `admin` / *(required)* | GUI login |
| `SPM_VAAPI_DEVICE` | `/dev/dri/renderD128` | Intel Quick Sync render node |
| `SPM_PROBE_TIMEOUT` | `30` | seconds a detail-popup stream probe may take before reporting a timeout (network streams are probed with the MAG identity) |
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
2. **Fetch Sources** – background job pulls genres → channels/movies/series → seasons/episodes with progress logging. Enable/disable **per genre** what enters the catalog; series enablement is per season. In the **Edit portal** popup this is a two-step flow: *Fetch genres* loads the live/VOD/series genre lists (all disabled by default — including the synthetic *(All VOD)* / *(All series)* a portal without categories gets), you tick the genres you want (the filter box narrows the list as you type), and **Save** then fetches the items of exactly those enabled genres.
3. **Playlist Builder** – three tabs (Live, VOD, Series, Local). Every output item keeps its own **ordered fallback chain** (source × portal × MAC as needed), an optional **ffmpeg template**, group, epg id and logo. Drag & drop reorders channels. Clicking a **VOD** or **Series** row (or its ⓘ button) opens the same detail popup as Input Sources — stored portal metadata, a lazy **stream probe** (codec/resolution/bitrate) and **TMDB** enrichment. The ▶ *test stream* buttons (here and in Input Sources) open the preview player, which closes via its header **×** or the **Stop & Close** button.
4. **Users** – each user gets `username/password` and can receive **M3U** and/or **Xtream** URLs (copy-buttons in the GUI). Per-user active-connection caps enforced.
5. **Dashboard** – counters, active streams with kill buttons, quick actions (fetch, retry-busy), messages pane.

### Client URLs (per user)

```
M3U:      http://<host>:8880/get.php?username=USER&password=PASS&type=m3u_plus&output=ts
Xtream:   http://<host>:8880/player_api.php?username=USER&password=PASS
Stream:   http://<host>:8880/play/live/{id}.ts?username=..&password=..
          http://<host>:8880/{user}/{pass}/{stream_id}.ts   (xtream short form)
xmltv:    http://<host>:8880/xmltv.php?username=USER&password=PASS
```

Users only ever talk to port **8880** — GUI, streams, playlists and APIs share it.

---

## ffmpeg templates & transcoding (DS918+ Quick Sync)

Templates are full editable ffmpeg commands with GUI field ↔ command **2-way sync**: the option fields (encoder, bitrate, resolution, fps, GOP, audio, container, rate control, extra args) rebuild the command text, and editing the text parses back into the fields.

Shipped presets (stored as rows in the database and **re-seeded on every boot** — see below):

| Template | Use |
|---|---|
| VAAPI 720p ~1M (DS918+ reference) | hardware H.264 via `/dev/dri/renderD128` |
| VAAPI 1080p ~2.5M | hardware, full HD |
| QSV 720p ~1M | Quick Sync via `-hwaccel qsv` (alternative syntax) |
| Software 720p (libx264) | no-GPU fallback |
| Copy / passthrough | remux only (`-c copy`); also the automatic fallback when no GPU device is mapped |
| **Dreambox DM800se (Enigma2 / MPEG2-SD)** | downmix to an MPEG-2 transport stream the ancient Enigma2/openpli box can play (see below) |
| **Redirect (bypass ffmpeg)** | not an ffmpeg command at all — the player is 302-redirected straight to the portal's CDN. **The default template**: any item without an explicit template assignment redirects (see below) |

**Redirect (bypass ffmpeg) is the default.** The old global *proxy vs redirect* switch in Settings is gone: redirect is now a built-in template **and the default**. An item without an explicit template assignment is 302-redirected straight to the portal's CDN — instant start and zero CPU, but no transcode, no transport-stream rewriting and no mid-stream fallback. Assign any other template (inline *FFmpeg tpl* dropdown, the edit dialog, or bulk *Assign template…* in the Playlist Builder) to switch that channel back to ffmpeg proxying/transcoding. The `?mode=redirect` / `?mode=proxy` query parameter still works as a per-URL override.

**Default templates are persistent (stored in the database).** The shipped presets are real `ffmpeg_templates` rows marked `is_builtin`. On every boot the app reconciles them by name, so:

* they survive deletion (delete one, restart → it is back),
* they pick up fixes/tuning shipped in new releases,
* your edits win — a built-in whose command you changed by hand keeps your text,
* the built-in **default** (*Redirect (bypass ffmpeg)*) is reconciled on every boot, so upgrades switch over too; a default set on a *user-created* template is never overridden.

> Deleting a built-in template is therefore always safe — the next restart restores it, and the DS918+ reference preset stays available as a fallback.

**VAAPI tuning (what the `low-power` / `rate-control` / `async-depth` fields do).** The VAAPI presets are tuned for the Intel iHD driver on Apollo Lake (the DS918+'s J3455). The reference 720p template renders to:

```text
ffmpeg -rw_timeout 10000000 -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1
       -reconnect_delay_max 5 -fflags +genpts+discardcorrupt -err_detect ignore_err
       -init_hw_device vaapi=intel:/dev/dri/renderD128 -hwaccel vaapi
       -hwaccel_device intel -hwaccel_output_format vaapi -i <url>
       -vf scale_vaapi=w=1280:h=720:format=nv12,fps=25,setsar=1
       -map 0:v:0 -map 0:a:0? -sn -dn
       -c:v h264_vaapi -b:v 1000k -maxrate 1100k -bufsize 2000k -profile:v high -level 4.1
       -g 50 -r 25 -low_power 1 -rc_mode vbr -async_depth 4
       -c:a aac -b:a 128k -ac 2 -ar 48000
       -f mpegts -mpegts_flags +resend_headers pipe:1
```

**Bitrate numbers are tuned for external (internet) streaming.** On a LAN the NAS uploads as fast as it likes; over the internet a bursty stream underruns the viewer's download link and stalls. Every transcode preset therefore caps spikes close to the target (`maxrate` ≈ bitrate + 10 %) and carries a ~2-second VBV buffer (`bufsize` = 2× bitrate) so short-lived congestion is absorbed by the encoder instead of freezing the player. The shipped values:

| Preset | `-b:v` | `-maxrate` | `-bufsize` |
|---|---|---|---|
| VAAPI 720p ~1M (reference) | 1000k | 1100k | 2000k |
| VAAPI 1080p ~2.5M | 2500k | 2750k | 5000k |
| QSV 720p ~1M | 1000k | 1100k | 2000k |
| Software 720p (libx264) | 1200k | 1300k | 2400k |
| Dreambox DM800se | 1200k | 1300k | 2400k |

* `-low_power 1` selects the **fixed-function H.264 encoder** (`VAEntrypointEncSliceLP` in `vainfo`) instead of the EU/3D path — faster, lower power, and it leaves the GPU's shader units free for more concurrent streams. On this silicon it only exists for **H.264**, so the flag is emitted for `h264_vaapi` only (an HEVC low-power entrypoint would fail).
* `-rc_mode vbr` makes rate control **explicit** — VAAPI's implicit "auto" mode is driver-dependent, so `-b:v`/`-maxrate` are otherwise not guaranteed to be honoured the same way across driver versions. `cbr` is there too when you need a hard bandwidth ceiling (set *maxrate = bitrate* for true CBR).
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

**Identity of the outgoing ffmpeg request:** for `http(s)` inputs the manager injects `-user_agent "<MAG200 UA>"` and `-referer "<stream origin>/"` in front of `-i` — unless the template sets them itself. ffmpeg would otherwise announce itself as `Lavf/61.x` and send no referer, which plenty of panels (and the CDNs in front of them) answer with **403/405** on an otherwise perfectly valid token. The detail-popup **stream probe** uses the same identity, so a probe reflects what the stream path actually gets.

At boot the app performs a **hardware sanity check**: if the default template needs VAAPI/QSV but the device is absent, the default degrades to *Copy* with a warning in the log — streams never die silently. (The built-in default is *Redirect*, which needs no GPU, so this only matters once you pick a VAAPI/QSV template as default.)

---

## Fallback engine semantics

- Every MAC streams **at most one channel at a time** (typical Stalker limit); occupancy is tracked centrally, busy MACs are skipped instantly.
- Per play request the ordered chain is walked (source priority → MAC order); a `global setting` decides whether *all MACs of a portal are tried before moving to the next portal*.
- No data within 12 s (configurable) or an ffmpeg exit → next step; when the chain exhausts, the client gets a clean end-of-stream and the GUI log shows every step. An ffmpeg that dies *before* the first byte (bad URL, 405, missing GPU) is detected immediately — the log then says `ffmpeg exited rc=8 before sending data` instead of a misleading "no data within 12s", so you do not wait 12 s per dead source.
- **Link repair:** some panels rebuild the `create_link` answer instead of echoing it and lose parameters on the way (`&stream=392166` → `&stream=`). The request is stripped of its stale `play_token` before asking, and the answer is repaired against the request (missing/blanked parameters restored, the fresh token always wins). See `dev/check-links.py`.
- **Portal refusals keep their code.** A panel rarely answers a refusal with a 4xx: the usual shape is HTTP 200 + `{"js":{"error":"limit"}}`. `PortalError.code` carries it through to the fallback log, so `limit` / `account is in use` (→ *this MAC is busy over there, try the next one*) are never mistaken for `nothing_to_play` or `link_fault` (→ *this source is gone*). A bearer the panel expired — also a 200 + `{"error":"token"}` — triggers exactly one transparent re-handshake and retry, like a 401 does, instead of looking like a broken portal until the local token TTL runs out.
- **`%mac%` in a resolved link is filled in.** Portals that keep one link template for every box hand out `…/ch/%mac%/1234.ts` and expect the set-top box to substitute its own MAC; anything else is a 404 that reads as a dead channel.
- **HLS links get the input options they need.** When the resolved link is a `.m3u8` playlist, ffmpeg receives `-protocol_whitelist file,http,https,tcp,tls,crypto` and `-allowed_extensions ALL` (unless the template already sets them) — without those two, a valid portal playlist dies before the first byte.
- **`create_link` is asked only when it has to be asked.** A channel whose own flags (`use_http_tmp_link`, `use_load_balancing`) say its link is permanent, and whose stored `cmd` is a complete `http(s)` URL with no session token in it, is handed to the player as-is: a redirect play of such a channel costs the portal **zero** requests (no handshake, no token, no link). Every other case asks, including the ffmpeg path — where the request is worth making twice over, because it hands ffmpeg a fresh `play_token` *and* answers "is this source alive right now", which is what the chain above walks. `Portal.direct_links` (on by default) turns the shortcut off for a panel whose flags lie. The stream log says which rule fired and why.
- **One trust policy for every outbound call.** Portal, EPG and logo fetches all go through `app/services/http_client.outbound_client()` (OS CA store, verification on). `Portal.tls_insecure` is the only opt-out, it is per portal, and it is part of the pooled-session key so flipping it cannot leave an old session behind.
- **The panel's own account state is honoured.** Per MAC the panel reports `blocked`, `status` and an expiry, and Check Portal / the nightly portal sync now store that verdict: `banned` and `expired` MACs are dropped from fallback chains and from a fetch job's starting MAC, the Portals tab shows the badge, and the *reason* the portal gave is on the MAC row (`last_error`, shown in the badge tooltip). `offline`/`error` — our transport verdicts — stay retryable, because a portal that timed out is not a portal that said no.
- Client disconnects (and the Dashboard *kill* button) deterministically free the MAC and kill ffmpeg via a disconnect watchdog.

---

## What the portal is told about the box (STB identity)

A Stalker portal does not authenticate a user, it authenticates a *set-top box* — so a
proxy that announces itself as `python-httpx/0.28` gets a token and a playlist, and then
either 403s on the stream or starts behaving in ways no box ever does. Every portal request
now carries the identity of a MAG250, per MAC and derived from the MAC itself
(`md5(mac)` for the serial, `sha256(mac)` for the device id, …) so it is **stable across
restarts and unique per MAC** — nothing is stored on disk, and two MACs never look like the
same box:

- **The full handshake dance.** Some panels answer the first handshake with
  `{"js":{"msg":"missing"}}` plus a random seed and expect a *second* request carrying
  `mac=` and `prehash=sha1(<the bearer we just invented>)`. Both steps are performed, and the
  `Authorization: Bearer` header and the `token=` cookie are set together, as the stalker
  app does.
- **`get_profile` with the device fingerprint** (serial, device id, signature, hw versions,
  `api_signature=262`, a `metrics` blob quoting the random seed), and `not_valid_token`
  echoed from the handshake. The answer's `blocked` / `status` / `force_ch_link_check` are
  consumed (see above). A panel that answers nothing usable falls back to the minimal
  `sn`/`device_id`/`timestamp` request, because some panels 403 the full one.
- **Headers and cookies on every request, including portal *discovery*:** the MAG200
  `User-Agent`, `X-User-Agent: Model: MAG250; Link: WiFi`, `Referer: <portal>/index.html`,
  and `mac=…; stb_lang=en; timezone=<yours>` cookies (plus `adid=<md5(sn+mac)>` for
  `/stalker_portal/` panels). A wrong or missing cookie MAC is a 403 on *any* action, so the
  `mac` cookie is always in colon form and never rewritten.

Per portal you can tune what it is told, in the portal dialog or via the API:

| Field | Default | why you would change it |
|---|---|---|
| `identity_mode` | `mag250` | `minimal` sends only `sn`/`device_id`/`timestamp` to `get_profile` — some panels 403 the full fingerprint |
| `stb_timezone` | `Europe/Amsterdam` | what the box's `timezone=` cookie says; some panels key content or sessions on it |
| per-MAC `sn` | derived | the box's **real** serial, if you captured one (see below) |
| per-MAC `device_id` | derived | same, for `device_id`/`device_id2`/`signature` |

To pin a real serial: **MACs → ⚙ → 🎩** on the MAC's row, then type
`SERIAL123, DEVICEID456` (device id optional). A panel that has seen the box's real serial
once will notice a changed one, so pinning it is what makes moving a subscription to this
proxy invisible.

Environment knobs, when a panel wants something different again: `SPM_STB_UA` (the portal
calls, the stream probes and ffmpeg's `-user_agent` all use this one value),
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

## Built-in mock portal (testing without a real subscription)

`SPM_MOCK_PORTAL=1` mounts a fake Stalker portal at `http://<host>:8880/mock/c/` with MACs `00:1A:79:AA:AA:01` / `…02`, expired `…BB:BB:01` and blocked `…CC:CC:01`, 3 live genres × 4 channels, 2 VOD genres × 12 movies, 2 series genres × 6 series × 3 seasons × 5 episodes. `POST /mock/_control {...}` emulates what the portal can do to you, so the client's behaviour is testable without a subscription: `offline`, `slow`, `max_per_mac`, `http_status`, `require_prehash` (demand the second handshake step), `fingerprint_required` (403 a `get_profile` with no serial), `profile_mode` (`full`/`no_id`/`none`), `not_valid` (short-lived token), `token_rejects` (answer 200 + `{"error":"token"}`), `js_error`, `empty_reply`, `corrupt_stream`, `require_tls`, `require_host`, `reject_no_cookie`, `reject_no_referer`. `version_mode` (`full`/`none`/`html` — the last one is a captive portal answering `version.js`), `modules`, `modules_disabled` and `no_modules` (404 the action, which means *we do not know*, not *it has nothing*) emulate what the panel *says about itself*, and the live catalogue ships five deliberate link shapes — permanent, tmp-link, load-balanced, no flags at all, and a "permanent" URL that still carries a `play_token` — so the conditional-`create_link` rules are testable instead of theoretical. `GET /mock/_state` answers with those settings *and* what the portal actually received (`seen_profile`, the handshake `prehash`es, `seen_create_link`, the `handshakes`/`version_calls`/`modules_calls`/`create_links` counters, and per-MAC usage) — which is how the identity and link tests prove a request arrived, or that one deliberately did not, instead of trusting the client.

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
| `create_link -> http://…&stream=392166&…&play_token=***` | the resolved URL (token masked) | copy it and `curl -I` it inside the container: that is exactly what ffmpeg is given |
| `create_link: portal dropped parameters -> repaired from cmd` | the panel rebuilt its answer and lost a parameter (`&stream=392166` → `&stream=`) — it was restored from the stored cmd | informational; if it appears on every play, re-fetch the source so the stored cmd is clean |
| `[ffmpeg] … HTTP error 405 Method Not Allowed` / `Error opening input file …&stream=&…` | ffmpeg was handed an incomplete URL | update to a build with the link repair, or re-fetch the sources |
| `[stream] ffmpeg exited rc=8 before sending data` | ffmpeg could not open the source at all (dead link, 403/405, template needs a GPU that is not mapped) | read the `[ffmpeg]` line just above it — it carries ffmpeg's stderr tail |
| `[stream] no data within 12s from portal/mac` | the portal accepted the request but sends nothing (MAC busy *on the panel*, expired account, IP/geo block) | *Check Portal* in the GUI; try another MAC of the same portal |
| `[stream] … portal said limit - connection limit for this MAC (panel says it is already streaming)` | the panel is right: that MAC already has a stream open (often a previous player that has not been timed out yet) | the chain moves to the next MAC on its own; if every MAC says `limit`, the panel's quota is the real limit |
| `[stream] … portal said nothing_to_play` / `link_fault` | the source is dead or the CDN is unhappy — retrying with another MAC cannot help | *Fetch Sources* for that channel, or drop it from the chain |
| `create_link: portal left a mac placeholder in the link -> filled in from our MAC` | the panel serves one template for every box and expects the client to insert its MAC | informational; the URL ffmpeg got already contains the right MAC |
| `[portal] TLS error / unable to get local issuer certificate` | the panel has a self-signed or incomplete certificate chain | tick **Allow broken TLS** for that portal (keeps every *other* portal verified) or fix the chain; do not disable verification globally |
| `[stream] [Ch] playing the stored link via portal/mac: the channel flags say nothing needs rebuilding…` | no `create_link` was asked, by design (see *Fallback engine semantics*) | if that channel is black, the panel lied about its links: tick **Play stored links when the panel allows** off for that portal, or re-fetch the sources |
| `[fetch] series categories skipped: the panel says it has no sclub` | the portal's own `get_modules` answer gated the fetch | informational; if the panel *does* have series, press Resolve to re-read the answer |
| `[output] user … exceeded max_connections` | a previous stream of that user was still counted when the player reconnected | raise `max_connections` for that user; the slot frees as soon as the disconnect watchdog notices the client is gone (≤0.5 s) |

Two things the proxy does for you here: the outgoing `create_link` cmd is stripped of its stale `play_token` (panels that receive their own token back tend to mangle the answer), and ffmpeg gets the MAG user-agent plus a referer for `http(s)` inputs, because `Lavf/61.x` is refused by quite a few panels with a 403/405.

---

## Development scripts (`dev/`)

| Script | What it does |
|---|---|
| `bash dev/smoke.sh` | Runs the freshly built image with `SPM_MOCK_PORTAL=1` and checks: GUI answers `/login` (200), mock handshake returns a token, the boot marker is in the container log **and on container stdout** (the single-stream rule above). This is what the `docker` workflow's smoke job runs. `SPM_SMOKE_IMAGE` / `SPM_SMOKE_NAME` / `SPM_SMOKE_PORT` override it for a local run against any port. |
| `bash dev/smoke-puid.sh` | Bind-mounts a directory owned by a foreign uid (mode 750) and checks both sides of the PUID/PGID story: **without** `PUID`/`PGID` the app must *not* be able to list `/media`, **with** them it must (and PID 1 must run as those ids, `/config` must be chowned to them). Needs root/sudo; skips itself otherwise. `SPM_SMOKE_PUID_IMAGE` / `SPM_TEST_UID` override. |
| `python3 dev/check-links.py` | Pins the portal plumbing that decides whether a channel plays: the `create_link` URL rules (prefix stripping, stale-token removal, repair of a mangled answer), the STB fingerprint and account verdict, the link-flag policy table and the `version.js`/`get_modules` parsers — plus greps that no probe's answer is discarded and that the link policy is not re-inlined at a call site. No pytest needed, so it also runs on a NAS. Run it after touching `app/portal/`. |
| `bash dev/check-yaml.sh` | Parses every workflow file (and `docker-compose.yml`) and verifies `dev/docker-publish.yml.example` is byte-identical to the real workflow. Run it before pushing anything under `.github/workflows/`. |
| `bash dev/seed-demo.sh [BASE_URL]` | Seeds a *running* instance with a full demo setup against the built-in mock portal (portal → genres → live/VOD/series → users). Idempotent; dev/mockup use (`SPM_SKIP_LOGIN=1`), default base `http://127.0.0.1:8880`. |

**YAML gotcha that silently disabled this whole workflow once:** a plain scalar may not contain `": "`, so step names must be quoted — `- name: "Image metadata (tags: latest, sha, semver releases)"`. Unquoted, GitHub reports *"mapping values are not allowed here"* and refuses the **entire file**: no job in it runs (build, push and smoke all vanish together), which looks like "the workflow stopped working" rather than a typo. `dev/check-yaml.sh` catches it before you push.

`dev/docker-publish.yml.example` is a full copy of the workflow, kept in sync on purpose: the repo's bot cannot commit under `.github/workflows/` (GitHub denies GitHub-App commits that touch workflows), so the copy is installed with

```bash
cp dev/docker-publish.yml.example .github/workflows/docker-publish.yml   # safe: byte-identical
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
python -m pytest          # or: python -m pytest -v tests/test_stream_disconnect.py
```

## Phase 3 (done)

- **EPG auto-match**: add rytec/xmltv sources (plain XML, `.gz`, `.xz`) in Settings
  → they are downloaded server-side, every `<channel>` is indexed, playlist
  channels get fuzzy-matched automatically (empty `epg_id` only — manual
  overrides in the channel editor are never touched), and programmes of matched
  channels are ingested (now-6h … +7d window, pruned automatically).
- **Merged XMLTV output**: `/xmltv.php?u=…&p=…` and `/epg.xml` now serve a real
  XMLTV document containing exactly the channels the authenticated user can see,
  with `<icon>`s and all ingested programmes (+48h). The M3U `url-tvg` attribute
  already points there.
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

---

*Not affiliated with the Stalker Middleware project. Use only with portals/subscriptions you are authorized to access.*
