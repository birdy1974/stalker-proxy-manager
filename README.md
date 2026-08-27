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

---

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SPM_DATA_DIR` | `/config` | config DB + state volume |
| `SPM_MEDIA_ROOT` | `/media` | local video files mount |
| `SPM_DATABASE_URL` | sqlite | `postgresql+asyncpg://user:pass@host:5432/dbname` to switch to Postgres |
| `SPM_ADMIN_USERNAME` / `SPM_ADMIN_PASSWORD` | `admin` / *(required)* | GUI login |
| `SPM_VAAPI_DEVICE` | `/dev/dri/renderD128` | Intel Quick Sync render node |
| `SPM_MOCK_PORTAL` | `0` | `1` boots a built-in demo portal (test data, busy-MAC emulation) |
| `SPM_LOG_LEVEL` | `INFO` | Python log level (stderr/Portainer) |
| `SPM_SKIP_LOGIN` | `0` | **Mockup/preview only**: bypass admin login (`*** LOGIN DISABLED ***` banner in log). Never set on a real deployment |

Everything else (portals, MACs, channels, templates, users, EPG sources, settings) is configured in the GUI and persisted in the database.

---

## The workflow

1. **Portals** – add each Stalker portal base URL and its MAC addresses (optionally per-MAC password). *Check Portal* resolves the real endpoint (`/c/`, `/client/`, `/portal.php`, …) and verifies each MAC online (busy-ness and subscription expiry included). *Delete* offers a replacement-dialog cleanup for playlists that reference it.
2. **Fetch Sources** – background job pulls genres → channels/movies/series → seasons/episodes with progress logging. Enable/disable **per genre** what enters the catalog; series enablement is per season.
3. **Playlist Builder** – three tabs (Live, VOD, Series, Local). Every output item keeps its own **ordered fallback chain** (source × portal × MAC as needed), an optional **ffmpeg template**, group, epg id and logo. Drag & drop reorders channels.
4. **Users** – each user gets `username/password` and can receive **M3U** and/or **Xtream** URLs (copy-buttons in the GUI). Per-user active-connection caps enforced.
5. **Dashboard** – counters, active streams with kill buttons, quick actions (fetch, retry-busy), messages pane.

### Client URLs (per user)

```
M3U:      http://<host>:8880/get.php?username=USER&password=PASS&type=m3u_plus&output=ts
Xtream:   http://<host>:8880/player_api.php?username=USER&password=PASS
Stream:   http://<host>:8880/play/live/{id}.ts?username=..&password=..
          http://<host>:8880/{user}/{pass}/{stream_id}.ts   (xtream short form)
xmltv:    http://<host>:8880/xmltv.php?username=USER&password=PASS   (stub for now)
```

Users only ever talk to port **8880** — GUI, streams, playlists and APIs share it.

---

## ffmpeg templates & transcoding (DS918+ Quick Sync)

Templates are full editable ffmpeg commands with GUI field ↔ command **2-way sync**: the option fields (encoder, bitrate, resolution, fps, GOP, audio, container, extra args) rebuild the command text, and editing the text parses back into the fields.

Shipped presets:

| Template | Use |
|---|---|
| **VAAPI 720p ~1M (DS918+ reference)** | hardware H.264 via `/dev/dri/renderD128` — default |
| VAAPI 1080p ~2.5M | hardware, full HD |
| QSV 720p ~1M | Quick Sync via `-hwaccel qsv` (alternative syntax) |
| Software 720p (libx264) | no-GPU fallback |
| Copy / passthrough | remux only (`-c copy`); also the automatic default when no GPU device is mapped |

Verify acceleration inside the container with `vainfo -a` (should list EGL/VA-API entrypoints for the iHD driver).

At boot the app performs a **hardware sanity check**: if the default template needs VAAPI/QSV but the device is absent, the default degrades to *Copy* with a warning in the log — streams never die silently.

---

## Fallback engine semantics

- Every MAC streams **at most one channel at a time** (typical Stalker limit); occupancy is tracked centrally, busy MACs are skipped instantly.
- Per play request the ordered chain is walked (source priority → MAC order); a `global setting` decides whether *all MACs of a portal are tried before moving to the next portal*.
- No data within 12 s (configurable) or an ffmpeg exit → next step; when the chain exhausts, the client gets a clean end-of-stream and the GUI log shows every step.
- Client disconnects (and the Dashboard *kill* button) deterministically free the MAC and kill ffmpeg via a disconnect watchdog.

---

## Built-in mock portal (testing without a real subscription)

`SPM_MOCK_PORTAL=1` mounts a fake Stalker portal at `http://<host>:8880/mock/c/` with MACs `00:1A:79:AA:AA:01` / `…02` (plus expired `…BB:BB:01`), 3 live genres × 4 channels, 2 VOD genres × 12 movies, 2 series genres × 6 series × 3 seasons × 5 episodes. `POST /mock/_control {"offline":…, "slow":…, "max_per_mac":…}` emulates outages and account-in-use errors for fallback testing.

Tip: the GUI shows the ready-to-copy mock portal URL/MACs in the Portals tab when enabled.

---

## Logs & observability

Every subsystem writes **detailed entries** (module-tagged: portals, fetch, stream, ffmpeg, …) both to the **GUI → logs pane** (level filtering) and to **stdout** (visible in Portainer). ffmpeg's stderr tail is captured on failures. Active stream rows show live throughput; a stream monitor persists recent finished/killed streams.

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
- **TMDB popups** for VOD/series metadata (GUI detail dialogs).
- **Xtream completeness**: real `get_vod_info` (playlist row + portal-source
  fallback), `timeshift` on live streams, episode `movie_image`+`tmdb_id`.
- **Mock end-to-end**: `/mock/epg.xml` ships a generated XMLTV feed for the mock
  portal channels so the whole EPG flow is demoable without internet access.
- **Config portability**: backup export now also carries `epg_sources`
  (import merges them, duplicates skipped).

---

*Not affiliated with the Stalker Middleware project. Use only with portals/subscriptions you are authorized to access.*
