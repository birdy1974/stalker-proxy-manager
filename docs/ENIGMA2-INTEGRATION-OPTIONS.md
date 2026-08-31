# Enigma2 integration — options & design advice (discussion document)

Target box: **Vu+ Duo2 (BCM7424, MIPS, OpenPLi 9.2)**.
Goal: watch SPM's live / VOD / series catalogue directly from Enigma2 bouquets,
**with subtitles for VOD and series**, and **without software (CPU) transcoding**
anywhere — while still being able to hardware-transcode on the NAS when the
source is something the Duo2 cannot decode (HEVC, 4K, VP9, 10-bit).

Nothing is implemented yet. This document lays out the options, the trade-offs,
and a recommended shape, so we can agree before writing code.

---

## 1. What the box can and cannot do (the hard constraints)

| Capability | Vu+ Duo2 / OpenPLi 9.2 | Consequence for us |
|---|---|---|
| Video decode | MPEG-2, H.264 up to 1080p (BCM7424). **No HEVC, no VP9, no AV1, no 4K, no 10-bit** | 4K/HEVC VOD **must** be transcoded on the NAS (VAAPI) → H.264 ≤1080p 8-bit |
| Audio | AC3, MP2, AAC (AAC-in-TS is occasionally flaky on this generation), E-AC3 passthrough via HDMI | Prefer **AC3 or MP2** for transcoded output; keep `copy` when the source is already AC3/AAC and plays |
| Hardware *encoder* | Yes (the Duo2 has a broadcom encoder used by the transcoding streamproxy on :8001/:8002) | Irrelevant here — it only re-encodes what the box could already **decode**. It cannot rescue a 4K HEVC stream. Server-side transcoding stays the only path |
| Bitmap (DVB) subtitles | Native, in the normal subtitle menu, for service types `1` and `4097` | Live TV subtitles = solved today (`subs=dvb` in our templates) |
| Text subtitles (SRT/ASS) in TS | **Impossible** — MPEG-TS carries DVB bitmap or teletext only, and ffmpeg cannot convert text→bitmap without rendering (= CPU) | This is the actual VOD/series subtitle problem, and it is a **container** problem, not an Enigma2 problem |
| Text subtitles in MKV/MP4 | Yes, with **exteplayer3** (service `5002`) and, less reliably, with gstplayer (`5001`) / servicemp3 (`4097`) | ⇒ VOD/series must be delivered in **Matroska (or the untouched original container)**, not in our `.ts` pipe |

### The player/service-reference matrix on OpenPLi 9.2

| Service type | Backend | Text subs (SRT/ASS in MKV/MP4) | DVB bitmap subs in TS | Seek in VOD | Notes |
|---|---|---|---|---|---|
| `1` | DVB pipeline (raw TS straight into dvbmediasink) | no | **yes** (plus teletext) | no | Lowest CPU/latency on the box, best for live MPEG-TS; the URL must be a clean TS |
| `4097` | `servicemp3` (GStreamer) — or whatever ServiceApp maps it to | partial/patchy (ASS especially) | yes | yes-ish | The default everyone uses; safest generic choice |
| `5001` | ServiceApp → **gstplayer** | optional, toggle "embedded subtitles" in ServiceApp | yes | yes | Middle ground |
| `5002` | ServiceApp → **exteplayer3** (ffmpeg-based) | **yes — best text-subtitle support**, plus multi-audio | yes | yes (HTTP Range) | Requires `opkg install enigma2-plugin-systemplugins-serviceapp exteplayer3 ffmpeg`; present in the OpenPLi 9.2 feed |
| `8193` etc. | image-specific | — | — | — | Not portable, ignore |

**Conclusion that drives the whole design:** live keeps the current MPEG-TS path
(type `1` or `4097`, DVB subs already work), while **VOD and series need
exteplayer3 (`5002`) plus a container that can hold text subtitles**. No burn-in,
no CPU.

---

## 2. Three integration architectures

### Option A — "Do nothing new": point an existing E2 plugin at SPM's Xtream API
SPM already serves `get.php` / `player_api.php` / `xmltv.php`. Plugins like
`e2m3u2bouquet`, IPTV Bouquet Maker or an Xtream plugin on the box can build
bouquets from that today.

* **+** Zero code, works this afternoon; the plugin owns bouquet layout, picons, EPG import.
* **−** The box decides the service type; it will almost certainly emit `4097` with our `.ts` URLs ⇒ **no VOD/series subtitles** (the exact thing you want to fix).
* **−** No per-item choice of transcode vs. redirect, no server-side control, plugin quality varies, breaks on plugin updates.
* Verdict: fine as a stopgap / comparison baseline, not the answer.

### Option B — an Enigma2 plugin written by us, talking to SPM
A real SPM plugin on the box (like EStalker does for Ministra).

* **+** Full control of the UI, on-box subtitle picking, no bouquet churn.
* **−** Python 2/3 Enigma2 plugin, skins, i18n, per-image quirks, its own release channel, and it fights with the "everything lives in bouquets" model you asked for. Large, ongoing maintenance for one box.
* Verdict: out of scope. Explicitly declared "not our domain" in `docs/ESTALKER-COMPARISON.md`, and I agree.

### Option C — **a first-class "Enigma2 output" inside SPM** (recommended)
SPM generates the bouquet files itself, per *device profile*, and can push them to
the box and reload them. The box stays dumb: it only ever plays URLs that SPM
already serves.

* **+** All logic stays in the server we already control (players, transcoding decision, subtitle strategy, group filters, per-user credentials).
* **+** Same architecture as the existing M3U/Xtream outputs — it is "one more renderer + a delivery job".
* **+** Lets us do the per-kind trick that makes subtitles work: TS+`1`/`4097` for live, MKV+`5002` for VOD/series.
* **−** New surface: bouquet writing, push transport, reload, picons, EPG mapping.
* Verdict: this is what I would build. The rest of the document details it.

---

## 3. Solving the subtitle problem without CPU

Four candidate pipelines for a VOD/series item. The Enigma2 output should be able
to pick one **per item / per profile**, and to decide automatically.

| # | Pipeline | Video | Subs | Seek | CPU on NAS | When |
|---|---|---|---|---|---|---|
| **S1** | **Redirect (302) to the portal's original MP4/MKV**, service `5002` | untouched | all embedded tracks (SRT/ASS/PGS) exposed by exteplayer3 | full (HTTP Range from the CDN) | **zero** | Default whenever the source is H.264 ≤1080p — i.e. most VOD. Already possible today with the built-in "Redirect (bypass ffmpeg)" template |
| **S2** | **Remux proxy**: `-c copy -f matroska` through SPM, service `5002` | copy | copied (text + bitmap) | limited (no Range on a live pipe) unless we add a seekable remux | ~nothing (I/O only) | When you want the portal hidden / MAC fallback / credentials not leaking to the box, and the source codec is fine |
| **S3** | **HW transcode into Matroska**: VAAPI `h264_vaapi` (+ `-c:a ac3` or copy) **and `-c:s copy`**, service `5002` | Quick Sync | **text subs survive** (MKV can carry SRT/ASS), bitmap copied too | no (live pipe) | GPU only; subtitle copy is a byte copy | 4K / HEVC / VP9 / 10-bit sources — the Duo2 rescue path |
| **S4** | Current `.ts` pipeline (`subs=dvb`), service `1`/`4097` | copy or VAAPI | **bitmap only**; text subs are dropped | no | GPU only | Live TV; VOD only when the source's subs are already DVB/PGS |

Rejected on purpose: **burn-in** (`-vf subtitles=`) — it forces frames through the
CPU and defeats the hardware requirement; and **text→dvbsub** — ffmpeg cannot do
it ("subtitle encoding only possible text→text or bitmap→bitmap").

Optional extra: **S5, sidecar subtitles.** SPM extracts the text tracks to
`/subs/{kind}/{id}/{lang}.srt` (a one-off, few-milliseconds ffmpeg run, no video
decode). Usable on the box via the SubsSupport plugin. Worth having as a
diagnostic/fallback, not as the main mechanism.

### What this means in code
* `output_format` currently accepts `mpegts | hls`. We need **`matroska`** as a third value in `ffmpeg_templates.py`, with `-c:s copy` allowed for text codecs (today the builder forces `-sn` unless `subs=dvb`, and `dvb` re-encodes to `dvbsub`). Proposal: add `subs="keep"` = *copy every subtitle track as-is*, only valid with `output_format=matroska`, validated by `ffmpeg_validate.py`.
* Two new built-in templates:
  * **"Enigma2 VOD — remux + subtitles (MKV)"** = S2
  * **"Enigma2 VOD — VAAPI 1080p H.264 + AC3 + subtitles (MKV)"** = S3
* A new stream route that serves MKV, e.g. `/play/vod/{id}.mkv` (or a `?container=mkv` switch), so the URL extension matches what exteplayer3 gets — some builds sniff the extension.

### "Auto-fit" — decide transcode vs. passthrough per item
We already have `services/probe.py`. Per profile, define a device capability set
(`h264 ≤1080p, 8-bit, AC3/AAC`). At bouquet-build time (or lazily on first play):

```
probe(source) → codec/width/height/pix_fmt
  fits profile      → S1 redirect (or S2 remux)     → zero CPU, subs, seek
  does not fit      → S3 VAAPI MKV transcode        → GPU only, subs kept
```

Probe results are cached on the item, and the decision can always be overridden
manually per item (the existing per-item template assignment stays the source of
truth; auto-fit only *proposes*).

---

## 4. The "Enigma2 output" feature — proposed shape

### 4.1 Data model (new table `enigma2_profiles`)

| Field | Meaning |
|---|---|
| `name`, `enabled` | e.g. "Wohnzimmer Duo2" |
| `user_id` | which SPM user's credentials/group filters the bouquets carry |
| `host`, `web_port`, `use_https`, `owif_user`, `owif_pass` | OpenWebif, for reload + "test connection" |
| `transport` | `download` \| `ftp` \| `ssh` (see 4.3) |
| `ssh_port`/`ftp_port`, `login`, `password`, `key` | push credentials (stored like portal creds today) |
| `bouquet_prefix` | file naming, default `spm` → `userbouquet.spm_live.tv` |
| `player_live`, `player_vod`, `player_series` | `1` \| `4097` \| `5001` \| `5002` (defaults `4097`/`5002`/`5002`) |
| `container_live`, `container_vod`, `container_series` | `ts` \| `mkv` \| `original` (302) |
| `delivery_mode` | `auto-fit` \| `always redirect` \| `always proxy` \| `always transcode` |
| `caps_json` | max resolution / codecs / bit depth for auto-fit (preset: "Vu+ Duo2") |
| `include_live/vod/series/local`, group whitelists | what goes into the bouquets (can just reuse the user's group filter, with an extra per-profile filter) |
| `layout` | how series/VOD are split into bouquets (see 4.4) |
| `picons`, `epg_export` | booleans for the optional extras |
| `last_push_at`, `last_push_result` | status for the GUI |

Rendering itself is a service (`services/enigma2_bouquets.py`) next to
`playlist_gen.py`, and stays pure: *profile → list of files*. That makes it
trivially unit-testable, exactly like `build_m3u`.

### 4.2 Bouquet format (what we generate)

```
#NAME SPM • Live
#SERVICE 4097:0:1:0:0:0:0:0:0:0:http%3a//192.168.1.10%3a8880/play/live/42.ts?u=box&p=secret:Das Erste HD
#DESCRIPTION Das Erste HD
#SERVICE 1:64:0:0:0:0:0:0:0:0::Sport            <- marker line (section header)
```

Details that bite if we get them wrong:

* Colons inside the URL must be `%3a` (lower case is what every tool emits); the
  *name* after the last colon must not contain `:`.
* Field 3 (`service_type`) `1` = TV, `2` = radio. Field 4 (SID) is where we can
  put a **stable per-item number** — it is what makes picons and EPGImport
  mapping stable across regeneration. Recommend deriving it from the SPM item id.
* Every `#SERVICE` gets a `#DESCRIPTION` line — some images show a blank name otherwise.
* `bouquets.tv` must be *edited*, not overwritten: insert our
  `#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.spm_live.tv" ORDER BY bouquet`
  lines if missing, remove stale `spm_*` ones, keep everything else, and always
  write a timestamped backup on the box before touching it.
* Write to `/tmp/…` then `mv` into `/etc/enigma2/` (atomic; a half-written bouquet
  crashes the service list).

### 4.3 Getting the files onto the box — four transports

| Transport | How | Pros | Cons |
|---|---|---|---|
| **D1 — download / pull** | SPM exposes `/enigma2/{profile}/bouquets.tar.gz` + a one-line installer script; the user runs it or puts it in cron on the box | no credentials stored in SPM, works through NAT/firewalls, easy to test | manual, or needs a cron line on the box |
| **D2 — FTP push** | `ftplib` (stdlib, run in a thread) → `/etc/enigma2/` | OpenPLi ships vsftpd with root login enabled by default; **zero new dependencies** | plaintext credentials, no atomic move (upload to `/tmp` then rename via FTP `RNFR/RNTO`) |
| **D3 — SSH push** | Dropbear on the box; upload via an `exec` channel (`cat > /tmp/x && mv …`) — do **not** assume `scp`/`sftp-server` exist on OpenPLi | secure, atomic, can run the reload/backup commands in the same session | new dependency (`asyncssh` preferred over `paramiko` for our async stack) |
| **D4 — OpenWebif only** | OpenWebif has no generic file-upload API for bouquets | — | not viable for writing; **but it is how we reload** |

Recommendation: ship **D1 + D2** first (no new dependency, covers stock OpenPLi),
add **D3** behind an optional extra requirement.

**Reload after push** (all transports): `GET /api/servicelistreload?mode=2`
(bouquets only; `mode=0` also reloads lamedb) with optional basic auth, then
optionally re-zap. Show the JSON result in the GUI, and log it via `db_log`.

### 4.4 Bouquet layout (this is where big libraries hurt)

Enigma2 gets slow and ugly with tens of thousands of entries and hundreds of bouquets.

* **Live** → one bouquet per group, or one bouquet with markers per group. Cheap either way.
* **VOD** → one bouquet per group (`SPM • VOD • Action`), alphabetical auto-split at N entries (default 1000–2000), markers as letter separators.
* **Series** → three candidates:
  1. one bouquet per series (clean, but 400 series = 400 bouquets — do not default to this);
  2. **one bouquet per group, markers per series and per season** (recommended default — the list stays browsable and the bouquet count stays sane);
  3. only "recently added / favourites" series, on the assumption that browsing happens elsewhere.
* Configurable `max_entries_per_bouquet` with auto-split, and a hard preview
  ("this profile will create 7 bouquets / 4 812 services") **before** anything is pushed.

### 4.5 EPG

* Live: generate **EPGImport** files — a `sources.xml` snippet pointing at SPM's
  existing `/xmltv.php?u=…&p=…`, and a `custom.channels.xml` mapping each XMLTV
  channel id to the exact service reference we wrote into the bouquet. Push both to
  `/etc/epgimport/`. Without the channel map, EPGImport matches nothing.
* Alternative (much simpler, worse): no EPG for IPTV bouquets at all — Enigma2 then
  shows just the service name.
* VOD/series have no EPG concept on the box; the description line is all we get.

### 4.6 Picons (optional, phase 2)

Enigma2 looks up picons by *service reference with `:` replaced by `_`*, which for
IPTV refs means the whole escaped URL — long and fragile. Two ways out:

1. Generate PNGs from SPM's existing logo cache, named after the exact reference
   we emit, pushed to `/usr/share/enigma2/picon` or `/media/hdd/picon`. Works
   because *we* control the reference string and keep it stable via the SID field.
2. Skip picons; rely on names. (Default.)

### 4.7 GUI

A new **Enigma2** tab (or a section inside Settings), styled like the existing
pages: profile list → edit form with the fields above, plus
`Preview bouquet` (renders the text in a modal),
`Download .tar.gz`, `Test connection` (OpenWebif version + `/api/about`),
`Push now`, `Push + reload`, and a status line with the last result.
A per-item "Enigma2" badge in the Playlist tab showing which delivery each item
would get under a chosen profile (auto-fit dry run) would be very useful for
debugging "why does *this* movie have no subtitles".

### 4.8 API sketch

```
GET    /api/enigma2/profiles                 list
POST   /api/enigma2/profiles                 create
PUT    /api/enigma2/profiles/{id}            update
DELETE /api/enigma2/profiles/{id}
POST   /api/enigma2/profiles/{id}/preview    -> {files: [{name, text, count}], summary}
POST   /api/enigma2/profiles/{id}/push       -> {uploaded: n, reloaded: bool, log: [...]}
POST   /api/enigma2/profiles/{id}/test       -> OpenWebif reachability + image/version
GET    /enigma2/{token}/bouquets.tar.gz      public pull endpoint (token-authed, no admin session)
GET    /enigma2/{token}/install.sh           installer/cron one-liner for the box
```

---

## 5. Risks, edge cases and things to decide

1. **Credentials in bouquet files.** The URLs carry `?u=&p=`. Anyone with FTP on
   the box reads them. Mitigation: give each profile its own SPM user, or add
   opaque per-device tokens (`/play/live/42.ts?t=<token>`) — worth doing anyway.
2. **Zapping and `max_connections`.** Enigma2 opens/closes streams fast while the
   user scrolls the list; with `max_connections=1` you get 429s and a "no free
   connection" black screen. Needs a short linger/reuse window in
   `stream_manager.py`, or a per-profile recommendation to give the box ≥2 connections.
3. **No seek on proxied VOD.** Our pipe is not Range-capable, so with S2/S3 the
   Duo2 gets no fast-forward — acceptable for a rescue path, but it is the main
   reason to prefer **S1 redirect** whenever the codec fits.
4. **exteplayer3 must be installed** (`opkg install enigma2-plugin-systemplugins-serviceapp exteplayer3 ffmpeg`) and set up in *Menu → Setup → ServiceApp*. If the profile picks `5002` we should say so in the GUI and, if SSH is configured, offer to check/install it.
5. **Stable ids.** Regenerating bouquets must not renumber services, or the user's
   own favourites and the EPG mapping break. SPM playlist ids are stable — keep
   using them as the reference SID and never renumber on re-sync.
6. **Base URL.** The bouquet is consumed by a device on the LAN: `output_base_url`
   must be a LAN-reachable IP/hostname, not `localhost`. Validate on push and warn.
7. **MKV over a live pipe.** Matroska is streamable but not all builds of
   exteplayer3 like a non-seekable MKV; needs a real test on the Duo2 before we
   commit to S3 as the transcode default. Fallback if it disappoints: `-f mpegts`
   with `subs=dvb` (bitmap only) plus S5 sidecars.
8. **Duo2 is MIPS/1080p:** the transcode template should be H.264 **High@4.0,
   1080p max, 8-bit, AC3 2.0/5.1** — the existing "Dreambox DM800se" preset is far
   too conservative (576p/MP2) for this box; a new "Vu+ Duo2" preset is warranted.

---

## 6. Suggested phasing

* **Phase E1 — make subtitles possible at all (no Enigma2 code yet).**
  `matroska` output format + `subs=keep` (copy) in `ffmpeg_templates.py`/`ffmpeg_validate.py`,
  `.mkv` stream route, two new built-in templates, "Vu+ Duo2" transcode preset.
  Testable today from VLC/Kodi, and independently useful.
* **Phase E2 — bouquet generation, pull delivery.**
  `enigma2_profiles` model, `services/enigma2_bouquets.py`, preview + `.tar.gz`
  download + installer script, GUI tab. No credentials stored, nothing pushed.
* **Phase E3 — push + reload.** FTP transport, OpenWebif reload, test-connection,
  status/logging. SSH transport optional.
* **Phase E4 — auto-fit** (probe-driven transcode decision per item) **and EPGImport
  export**.
* **Phase E5 — picons**, and per-item Enigma2 diagnostics in the Playlist tab.

Each phase is shippable on its own; E1 alone already gives you subtitles on the
Duo2 if you hand-write one bouquet line pointing at an `.mkv` URL with `5002` —
which is also the cheapest way to validate the whole idea on real hardware before
we build E2–E5.

---

## 7. Decisions taken (2026-08-31)

| Question | Decision |
|---|---|
| Player | **ServiceApp + exteplayer3 available** → `5002` for VOD/series, `4097` default for live (`1` selectable), all overridable per profile |
| Delivery | **FTP push from SPM** (`ftplib`, stdlib, no new dependency) + OpenWebif reload; the download/pull endpoint stays as a fallback/debug path |
| Layout | **One bouquet per group, markers per series and season**, with auto-split above `max_entries_per_bouquet` |
| Scope of release 1 | **Phases E1 – E3**: MKV/subtitle pipeline, bouquet generation + GUI, FTP push + reload. EPGImport export and picons deferred to E4/E5 |

## 8. Concrete build plan for E1 – E3

**E1 — pipeline (no Enigma2 code)** — ✅ **implemented** (see the commit that added `tests/test_mkv_output.py`)
* `app/services/ffmpeg_templates.py`: `output_format` gains `matroska`; `SUB_MODES` gains `keep` (map `0:s?`, `-c:s copy`, only legal with `matroska`); MKV output writes `-f matroska pipe:1` and drops the mpegts-only flags.
* `app/services/ffmpeg_validate.py`: accept `keep`+matroska, reject `keep`+mpegts/hls with a clear message, keep the existing "text subs in TS" guard.
* `app/services/stream_manager.py`: the spawn-time probe gate must not degrade `keep` to `-sn` for text tracks (that gate exists for the `dvb` mode only).
* `app/routers/output.py`: `/play/{kind}/{id}.mkv` (and `?container=mkv`) serving `video/x-matroska`; Xtream-style `/movie/{u}/{p}/{id}.mkv`, `/series/{u}/{p}/{id}.mkv`.
* New built-in templates: *Enigma2 VOD — remux + subtitles (MKV)* (S2), *Enigma2 VOD — VAAPI 1080p H.264 + AC3 + subtitles (MKV)* (S3), *Vu+ Duo2 (H.264 High@4.0 1080p / AC3)*.
* Tests: extend `tests/test_subtitle_modes.py`; new `tests/test_mkv_output.py` (renderer, 2-way-sync fixed point, presets, spawn gate, HTTP routes).
* Delivered as: `output_format="matroska"` (+ `-live 1` for the pipe), `subs="keep"` (`-map 0:s? -c:s copy`), `option_warnings()` surfaced by `POST /api/ffmpeg/build` and shown in the editor, `.mkv` aliases for `/play/{live,vod,episode}` and the Xtream-style `/movie|/series` URLs, and the three built-in presets (`E2_VOD_REMUX_PRESET_NAME`, `E2_VOD_TRANSCODE_PRESET_NAME`, `E2_DUO2_LIVE_PRESET_NAME`).
* Not verified in CI: no ffmpeg binary in the build sandbox, so the rendered commands are asserted as text. Run *Demo (test video)* in the FFmpeg tab on the NAS once to see real Matroska bytes.

**E2 — bouquets** — ✅ **implemented**
* `app/models.py`: `Enigma2Profile` (fields in §4.1) + a light `Enigma2PushLog`, created by the existing bootstrap/migration path in `database.py`.
* `app/services/enigma2_bouquets.py`: pure renderer `profile → [BouquetFile(name, text, count)]`, reusing `_groups()/_allowed()` from `playlist_gen.py`, `best_title()` from `titles.py`, plus the reference builder (escaping, stable SID from item id, marker lines) and the `bouquets.tv` merge helper.
* `app/routers/api_enigma2.py` (admin) + public `GET /enigma2/{token}/bouquets.tar.gz` and `install.sh` in `output.py`.
* `app/templates/enigma2.html` + nav entry in `base.html`, JS in `app/static/js/`; profile CRUD, preview modal, summary ("7 bouquets / 4 812 services").
* Tests: `tests/test_enigma2_bouquets.py` — 22 cases: reference escaping, marker syntax, stable SIDs, per-layout file sets, letter/series/season markers, auto-split, the two group filters, warnings, tarball contents, installer semantics, full CRUD + preview + public pull + token rotation.
* Delivered as: `Enigma2Profile` (created by `create_all`, no migration needed), `app/services/enigma2_bouquets.py` (pure renderer + `merge_bouquets_tv` + `install_script` + `tarball_bytes`), `app/routers/api_enigma2.py` (admin CRUD/preview/download **and** the token-authenticated public pull), `app/templates/enigma2.html` + nav entry.
* Deviation from the sketch: `bouquets.tv` is **not** shipped in the tarball - only the box knows which other bouquets it has, so the installer merges `bouquets.spm.add` into the receiver's own copy instead.

**E3 — push**
* `app/services/enigma2_push.py`: FTP upload (thread-pooled `ftplib`), upload to `/tmp` → `RNFR/RNTO` into `/etc/enigma2`, timestamped remote backup of `bouquets.tv`, then `GET /api/servicelistreload?mode=2` on OpenWebif with optional basic auth; every step through `db_log`.
* GUI: *Test connection*, *Push now*, *Push + reload*, last-result status line.
* Tests: a fake FTP server / injected transport, plus an httpx mock for the reload call — no real box needed in CI.
* README section: ServiceApp/exteplayer3 prerequisites, the subtitle matrix, and the Duo2 profile walk-through.

## 9. Open questions (were: questions for you)

1. How big is the catalogue (rough number of live channels / VOD titles / series)?
   It decides the default for `max_entries_per_bouquet`.
2. Should the Enigma2 output reuse the existing per-user model (`?u=&p=`) or do you
   want per-device tokens introduced with it? (Tokens keep the box's FTP-readable
   bouquets from leaking a real SPM password.)
3. Duo2 FTP login — default `root` with the box's password, or a different account?
4. Where should the transcode ceiling sit: 1080p H.264 High@4.0 with AC3 passthrough
   for 5.1 sources, or force stereo AC3 for simplicity?
