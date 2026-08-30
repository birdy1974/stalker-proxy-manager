# EStalker vs. Stalker Proxy Manager — portal-interface comparison

Compared on 2026-08-30.

| | ours | EStalker |
|---|---|---|
| Repo | `birdy1974/stalker-proxy-manager` @ `75f1682` | `kiddac/EStalker` @ `032967f` ("Code refactor for URL calls", 2026-08-25) |
| Version | Phase 3 delivered | `1.48-20260824` (`CONTROL/control`) |
| Language / runtime | Python 3.11+, FastAPI, httpx **async**, SQLAlchemy async | Python **2 & 3** compatible, Enigma2 GUI plugin, `requests` + Twisted `Agent`, threads |
| Portal code size | ~1.1k LOC (`app/portal/*` + portal-facing services ~2.5k) | ~17.9k LOC total, portal-facing ≈ 4–5k (`utils.py` 528, `playlists.py` 1178, `live.py` 2237, `catchup.py` 910, `liveplayer.py` 1454) |
| License | (repo has no LICENSE file) | **no LICENSE file at all** — see [Legal note](#legal-note-do-not-copy-code) |

**Do not copy code, only behaviour.** EStalker publishes no license, so legally the default is
"all rights reserved". Everything recommended below is a *re-implementation of protocol behaviour*
(field names, endpoints, quirks), which is not copyrightable expression — that is how we should
consume this project.

---

## 1. Why the comparison is asymmetric

They are not two versions of the same thing:

* **EStalker** is an *STB emulator + player* that runs on one receiver box, for one MAC, driven by
  a remote control. It renders Enigma2 screens, feeds URLs to gstreamer, and stores state in
  `/etc/enigma2/…/playlists.json`. It has no users, no playlist output, no transcoding.
* **We** are a *multi-tenant portal gateway*: N portals × M MACs → persisted catalogue → generated
  M3U + Xtream output + XMLTV, with an ffmpeg pipeline, hardware transcoding, MAC occupancy
  arbitration and fallback chains.

So the interesting overlap is exactly one layer: **how each project talks to `portal.php`**
(discovery, auth, catalogue fetch, `create_link`) — plus a couple of portal *features* EStalker
uses that we do not expose at all (TV archive/catch-up, modules, portal short-EPG, `set_last_id`).

That is the lens used below. Where EStalker is simply *not comparable* (skin XML, Enigma2 screens,
bouquet writing, aspect-ratio handling, i18n) I ignore it.

---

## 2. Interface-by-interface scorecard

| # | Interface area | EStalker | us | Winner |
|---|---|---|---|---|
| 1 | Portal endpoint discovery | `xpcom.common.js` at `/c/`, `/stalker_portal/c/` + saved prefix; parses `this.ajax_loader` with 3 regex shapes; caches per host | 9 path candidates, `varpattern`+`ajax_loader` eval (STB-Proxy port), **plus** a direct `handshake` probe per path, full attempt log in GUI | **us** (more strategies, observable); ES for the `portal_ip`/`path_prefix` line-shapes |
| 2 | Portal self-description | fetches `version.js` → "Ministra 5.4.x", persists `path_prefix` | none | **ES** |
| 3 | Request identity | `X-User-Agent`, `Referer: …/index.html`, `Pragma`, `Accept-Encoding/Language`, `Host`, `Connection: Close`, cookies `mac/stb_lang/timezone/adid` + `token` cookie | `User-Agent` + cookies `mac/stb_lang/timezone` only | **ES** by a wide margin |
| 4 | Adaptive headers | regex-scrapes `set_cookie('x')` / `setRequestHeader('X-…')` out of `xpcom.common.js` and only sends what the portal declares | fixed set | **ES** (nice idea, fragile) |
| 5 | Handshake | 2 shapes (with/without explicit `mac=`), `"missing"` → fake bearer + `prehash=sha1(fake)` retry, reads `js.random`, `js.not_valid` | 1 shape, `prehash=0`, no `mac` param, no prehash dance | **ES** |
| 6 | `get_profile` | full MAG250 fingerprint (sn, device_id, device_id2, signature, hw_version, hw_version_2, metrics, api_signature, prehash, not_valid_token) + minimal fallback | no fingerprint request at all — the old `profile()` had no caller and was deleted in R5 | **ES** (R1) |
| 7 | Account state | `account_info` expiry **plus** `js.status`, `js.blocked`, `force_ch_link_check`; playlist marked invalid if `blocked==1` | expiry only (`account_expires`); no blocked/status/force flags anywhere | **ES** |
| 8 | Token/session lifecycle | token+headers persisted to JSON, re-`handshake` via `reauthorize_portal()` on **any** failed call, exactly 1 retry (24 call sites) | pooled client per (portal,MAC), TTL 3000 s, `401/403` → re-handshake + 1 retry, idle reap 900 s, hit/miss stats | **us** (TTL, pooling, reuse, stats); ES wins on "retry on transport errors too" |
| 9 | Catalogue fetch | lazy, scroll-driven paging (14/page), `pages_downloaded` set, `all_data[total_items]` positional array, `/tmp/allchannels.json` | background jobs, `get_all_channels` first, 4-way concurrent paging, budget, per-page bulk upsert, resumable, progress in GUI | **us** (server-side need); ES's positional page placement is a good idea we already get from `gather` ordering |
| 10 | Series/VOD type duality | hardcodes `type=vod` for VOD, `type=series` for series | tries `series` then `vod`, validates season/episode rows, id-shape tolerance | **us** |
| 11 | `create_link` policy | **conditional**: only when the channel says `use_http_tmp_link`/`use_load_balancing` or portal says `force_ch_link_check`; else play the stored `cmd` as-is; fallback heuristic for legacy rows (`localhost`, `///`, `/ch/`, no `http`) | **always** resolves (proxy needs an absolute URL + it is the "is this source alive?" signal) | **ES** on portal load & start-up latency, **us** on correctness/uniformity — see R2 |
| 12 | Link post-processing | `%mac%` substitution; `ffmpeg `/`ffrt ` prefix strip; `urlparse().geturl()` normalisation; `.m3u8`→streamtype | prefix strip, stale-token strip, **answer repair** when the panel drops `&stream=…` (`merge_link`), and — since R4 — `%mac%` substitution plus `.m3u8`→HLS input options | **us**: repair is the more valuable trick, and we now have their two as well |
| 13 | Portal error semantics | maps `js.error` → user-visible text: `limit`, `nothing_to_play`, `link_fault`, `access_denied` | the same codes, in `PortalError.code` + `detail()`, and *plus* a decision each one drives (`limit` = rotate MAC, `nothing_to_play` = advance the chain), a per-code check status, and the payload guard on `js.msg` (R4 ✅) | **us** |
| 14 | Portal EPG | `get_short_epg&size=10` for the visible rows only, deduped, `503` retry ×2, one reauth generation | the unused `client.short_epg()` is now deleted, so this is external XMLTV only until R9 | **ES** |
| 15 | TV archive / catch-up | full: modules gate, `get_all_channels`+`archive` filter, `type=epg&action=get_week`, `get_simple_data_table` paged by `total_items`/`max_page_items`, `mark_archive`, `tv_archive_duration` cutoff, play via `type=tv_archive&action=create_link&cmd=auto /media/<id>.mpg` | none — `tv_archive` is fetched and then hard-set to `0` in output (`playlist_gen.py:264`) | **ES** (the single biggest gap) |
| 16 | Module discovery | `type=stb&action=get_modules` → `all_modules` minus `disabled_modules`, gates menu entries (e.g. `tv_archive`) | none: a portal without series/VOD is discovered by trial and error at fetch time | **ES** |
| 17 | Player hygiene | `type=itv&action=set_last_id`, `type=watchdog&action=get_events&init=` (timer currently commented out) | nothing | **ES** (small but cheap) |
| 18 | Multi-MAC / fairness | one MAC per playlist; N playlists fetched with **one in-flight request per domain per round**, ≤30 threads, sequential fallback | MAC order per portal, occupancy locks (1 stream/MAC), `macs_first`/`portal_first`, `FETCH_PAGE_CONCURRENCY=4` | **us** for MAC semantics; ES's per-domain round-robin is the missing fairness piece |
| 19 | Transport/TLS | `verify=False` everywhere, `HTTPAdapter(max_retries=0/1)`, GET `(8,8)` timeouts | TLS verification **on** for every outbound call, `PORTAL_HTTP_TIMEOUT=10`; portal traffic shares the one TLS policy with EPG/logos, plus a per-portal `tls_insecure` opt-out in the GUI (R5 ✅) | **us**, decisively |
| 20 | Xtream interop | harvests `/movie/<user>/<pass>/` from a `create_link` answer, then queries `player_api.php` for status/`created_at`/`exp_date`/`active_cons`/`max_connections`; auto-delete of invalid playlists | we *serve* Xtream, we do not *ingest* it | **ES** (see R7) |
| 21 | Code quality | screen-centric, `print()` debugging, broad `except`, mixed Py2/Py3, no tests | typed, module loggers with masked tokens, 20 pytest files (3141 LOC), CI smoke test, mock portal with toggleable failure modes | **us**, decisively |

---

## 3. Advantages / disadvantages, honestly stated

### EStalker's advantages (things we lack)

1. **It looks like a real MAG.** Fingerprinted `get_profile`, `Referer`, `X-User-Agent`, `adid`,
   `token` cookie, portal-version string. This is the difference between "portal serves data" and
   "portal 403s / serves an empty body / bans the IP after 200 requests". Our README's own risk note
   says real portals were never testable from the dev environment — the fingerprint is exactly what
   unblocks that on picky panels.
2. **The auth dance is complete.** `not_valid_token`, `token_random`, `prehash`, `mac=` retry, and
   `js.random` are all handled. Panels that do "second step auth" are simply unusable for us today.
3. **Portal state is consumed, not discarded.** `blocked`, `status`, `force_ch_link_check`,
   `play_token` and expiry drive real decisions (playlist marked invalid, link-check flag echoed).
   We store an `expire_date` string that nothing ever acts on, and mark an expired MAC `online`.
4. **Cheapest possible portal footprint.** Conditional `create_link`, scroll-driven paging, per-host
   `xpcom` cache, one-request-per-domain scheduling. A Stalker panel tolerates ~1 request per
   second per MAC before it starts answering 403; EStalker is built around that fact.
5. **Feature breadth on the portal API:** TV archive, modules, short-EPG, `set_last_id`, watchdog,
   favourites/recents, search-via-`get_all_channels`, Xtream-credential harvesting.
6. **Human-legible failures.** Four distinct create-link error strings vs. our one generic message.

### EStalker's disadvantages (why we must not just "adopt it")

1. **It is a GUI plugin, not a service.** No async, no multi-user, no state machine, no API. Screen
   classes carry the HTTP layer (`live.py` contains both a `List` widget and a `create_link` call).
2. **Error swallowing.** `make_request()` returns `None` for *every* failure — timeout, TLS error,
   HTTP 500, malformed JSON are indistinguishable, and the only recovery is "reauth once". For a
   proxy that must decide *fallback vs. retry vs. report*, that is unusable.
3. **No TLS verification at all** (`verify=False` on every call). We state the opposite policy in
   `app/services/http_client.py:6` ("Never set verify=False") and should keep it.
4. **Brittle-by-construction discovery.** Regex-scraping `set_cookie(`/`setRequestHeader(` and the
   `ajax_loader` line works on unobfuscated files only; ours already keeps a handshake-probe
   fallback, which is the more robust primitive.
5. **Hardcoded identity is a *device*, not a fleet.** One `MAG250` / `ImageDescription…2018` string
   for every playlist. As a proxy serving many users from a few MACs we can reuse it per-MAC, but we
   must not rotate it (see R1's "stability" rule) or invent per-user ones (portal may reject unknown
   device ids).
6. **Fragile scale paths.** `all_data = [{} for _ in range(total_items)]`, `del self.list2[i]` +
   `buildLists()`, `os.system("sync")` + writing `/proc/sys/vm/drop_caches`, `time.sleep(3)` in a UI
   thread. Fine on a receiver, unacceptable in a container.
7. **No tests, no packaging, no docs** — everything must be reverse-engineered from the source, as in
   this document.
8. Python 2 heritage: `basestring`, `urllib/urllib2` try-except pairs, `print` debugging.

---

## 4. Recommendations

Effort is in *senior-dev hours including tests*. R1–R4 are the ones worth doing.

### R1 — Emulate the STB properly (fingerprint + full auth dance + headers) — **DO, highest value**

*Effort ≈ 6–10 h. Risk: low if derived per MAC and persisted.*

Changes, all inside `app/portal/client.py` (+ `models.py`, `resolver.py`):

1. `handshake()` → three stages instead of one:
   * stage 1: today's request;
   * stage 2: same with explicit `&mac=<MAC>` (EStalker `utils.py:187-198`);
   * stage 3: if `js.msg` matches `missing`, generate a 32-char pseudo token, set
     `Authorization: Bearer <fake>`, re-handshake with `prehash=sha1(fake)` (EStalker `utils.py:205-235`).
   Read and keep `js.random` (as `token_random`) and `js.not_valid`.
2. `profile()` → send the device fingerprint, and *use* the answer:

```python
# derived, never random: stable per MAC across restarts (a portal remembers the box it enrolled)
sn        = md5(mac).hexdigest().upper()[:13]
device_id = sha256(mac).hexdigest().upper()
hw2       = sha1(mac).hexdigest()
prehash   = sha1(sn + mac).hexdigest()
adid      = md5(sn + mac).hexdigest()
params = {"type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml",
          "hd": "1", "num_banks": "2", "sn": sn, "stb_type": "MAG250", "client_type": "STB",
          "image_version": "218", "video_out": "hdmi", "device_id": device_id,
          "device_id2": device_id, "signature": sha256(device_id + device_id).hexdigest().upper(),
          "auth_second_step": "1", "hw_version": "1.7-BD-00",
          "not_valid_token": "1" if not_valid else "0", "hw_version_2": hw2,
          "timestamp": str(int(time.time())), "api_signature": "262", "prehash": prehash,
          "ver": "ImageDescription: 0.2.18-r23-250; ImageDate: …; PORTAL version: 5.3.0; "
                 "API Version: JS API version: 343; STB API version: 146; Player Engine version: 0x58c",
          "metrics": quote(json.dumps({"mac": mac, "sn": sn, "model": "MAG250", "type": "STB",
                                       "uid": "", "random": token_random or ""}, separators=(",", ":")))}
```

   If the profile comes back without `id`, retry with the minimal shape (`sn`, `device_id=""`,
   `timestamp`) — EStalker's own fallback, `utils.py:350-362`. Persist nothing secret, but *do*
   persist `sn`/`device_id` per MAC row so a re-generated value can never silently re-enrol the box.
3. Headers for **every** portal request: `Referer: <base><path>/index.html`,
   `X-User-Agent: Model: MAG250; Link: WiFi`, `Accept-Language`, `Accept-Encoding: gzip, deflate`;
   cookies `mac`, `stb_lang`, `timezone` (from a new setting, not the hardcoded `Europe/Amsterdam`)
   and `adid` **only** when the path contains `stalker_portal`; after handshake also set the
   `token=<jwt>` cookie next to the `Authorization` header.
4. New per-MAC GUI/env escape hatch: `identity_mode = minimal | mag250` (default `mag250`) and
   per-portal `sn/device_id` overrides — because the one time the fingerprint is *wrong* for a
   panel it is worse than no fingerprint, and the user must be able to switch without a code change.

*Why:* our single biggest unknown is real-portal tolerance. This buys us the same "unfair advantage"
EStalker has (it is a client, so panels accept it) while keeping our architecture.

### R2 — Make `create_link` conditional and *correctly* so — **DO**

*Effort ≈ 4 h. Risk: medium — must not break the fallback semantics.*

Today we resolve every `cmd` on every stream open. EStalker only calls `create_link` when the channel
or portal says it must (`live.py:1709-1718`, `liveplayer.py:945-951`). Two reasons to adopt a
*narrowed* version: it halves portal requests per channel-change, and it removes our own
`create_link` round trip as a failure mode on panels where the stored `cmd` is already a full URL.

But do **not** make it unconditional-direct like a player would: for us `create_link` doubles as the
"this source + this MAC is alive" probe that drives the fallback chain, and redirect-only links
expire. Proposed compromise:

* store the per-channel link flags during fetch (new columns on `LiveSource`/`VodSource`/
  `SerieEpisode`: `use_http_tmp_link`, `use_load_balancing`, `disable_ad`, `force_ch_link_check`,
  all tiny ints; mapping point is `fetch_jobs._live_fields`, and `get_all_channels` rows already carry
  them — the mock portal at `mock_portal.py:123` even sets `use_http_tmp_link: 1`);
* in `stream_manager`, when `cmd` is a complete `http(s)://` URL **and** none of the three flags is
  set **and** the template is `@redirect`, skip `create_link` (saves a round trip on the hot path);
* always `create_link` when a real ffmpeg pipe is opened (we want the fresh `play_token` and the
  liveness check) — plus `%mac%` substitution and `.m3u8` detection (see R4).

### R3 — Consume portal truth in Portal/MAC status — **DO**

*Effort ≈ 3–5 h.*

`api_portals.test_portal` (`:220-256`) marks a MAC `online` on a successful handshake even when the
account is blocked or expired. Adopt EStalker's decision function instead:

* `blocked == 1` → `status="banned"`, `online=False`, excluded from chains;
* expiry string parses to a past date → `status="expired"`, excluded from chains (this is what the
  `expired` value in the `mac_addresses.status` comment was always meant to mean);
* keep `status`/`play_token`/`force_ch_link_check` on the MAC or Portal row and show them in the
  Portals tab (the GUI already shows expiry; add `Portal version` from R6 and `link check` flag);
* `test` should report *why* (`active / expired 2024-01-01 / blocked by panel / no token`), and the
  chain builder (`stream_manager._live_chain` → `_pick_macs`) should skip non-`online` MACs, which
  also fixes the "expired mock MAC is still tried" wart.

### R4 — Portal error codes, `%mac%`, and a real message per failure — **DO** ✅ *delivered*

*Effort ≈ 2–3 h. Highest value-per-hour in this document.*

1. In `_get()`, treat HTTP 200 with `js.error` / `js.msg` as an error, and classify it:
   `limit` (`MAC over connection quota` → *try another MAC, do not count as portal outage*),
   `nothing_to_play`, `link_fault`, `access_denied` (`MAC not enrolled` → set status
   `unauthorized`), `not_authorized`/`token` (→ re-handshake, our existing path).
2. In `create_link()`, raise `PortalError(f"create_link: {code}")` carrying the code so the log line
   in `stream_manager` (`:725-728`, `:851-854`) says *what happened* instead of
   `create_link returned no usable url for cmd=…`, and so fallback can distinguish "this MAC is
   busy → next MAC" from "this source is dead → next source".
3. After resolving, do what EStalker does and we don't: substitute `%mac%` with the active MAC
   (case-insensitive) — several panels emit `…/ch/%mac%/…` inside links.
4. If the answer is a `.m3u8`, mark it so HLS handling is right (they flip the gstreamer type; we
   should keep `-protocol_whitelist`/`allowed_extensions` coherent in the template and let
   `mpegts.js`/HLS.js serve the browser preview).
5. Detect `js.error == "limit"` in the *stream* log message and surface a per-user toast
   ("portal reports connection limit reached") instead of a silent fallback.

### R5 — Fix the two internal inconsistencies this comparison exposed — **DO (tiny)** ✅ *delivered*

*Effort ≈ 1 h.*

* `StalkerClient._http()` builds its own `httpx.AsyncClient`, so portal calls use **certifi** while
  EPG/logo calls use the OS trust store via `outbound_client()`. Portal TLS is exactly where broken
  or self-signed panels bite — route the portal client through `outbound_client()` too, and add a
  per-portal `tls_insecure` flag (default **off**) for panels with broken chains, so we never have to
  write `verify=False` into the code the way EStalker does everywhere.
* `client.profile()` and `client.short_epg()` are dead code — either wire them up (R1, R8) or delete
  them; both are currently a trap for the next reader. *(deleted, since nothing ever called them)*

### R6 — Capability & version probing on *Resolve* — **DO (cheap, big DX win)**

*Effort ≈ 3 h.*

`POST /api/portals/{id}/resolve` currently answers ok/url/path/attempts. Add, from the same session:

* `version.js` scrape (`Portal version: 5.4.2`) — one GET, pure information, and the single most
  useful line to have in a bug report;
* `type=stb&action=get_modules` → keep `all_modules − disabled_modules` on the Portal row, then
  *grey out* the VOD/Series/EPG tabs (and refuse to queue those fetch jobs) when the module is
  absent, instead of letting the user burn a 30-minute job on a portal that has no `series`.
  EStalker uses exactly this to gate its menu (`menu.py:338-344`, `:422-435`).
* echo the resolved `path_prefix` + `index.html` `Referer` used, so a "works but why?" report is
  answerable from the GUI.

### R7 — MAC → Xtream bridge (optional, but it is the most *useful* trick in EStalker)

*Effort ≈ 5–8 h for a "detect + offer" version.*

EStalker plays detective (`playlists.py:1030-1075`): it takes the first `create_link` answer, looks
for `/movie/<user>/<pass>/`, and if found queries `player_api.php` — turning a MAC-only subscription
into a real Xtream account (status, `created_at`, `exp_date`, `active_cons`, `max_connections`).

For us this is a *source type*: an Xtream source is cheaper and far more robust than a MAC portal
(playlist + `player_api.php` catalogue instead of 14-items-per-page crawling). Proposal: on
`Portal test`, opportunistically probe for harvested credentials; when found, offer
"adopt as Xtream source (read-only)" with EPG/stream URLs taken from `player_api.php`. Keep it
explicit — some panels do not want that, and it must never be automatic.

### R8 — TV archive / catch-up — **DO NEXT** (the one feature our users will actually ask for)

*Effort ≈ 12–20 h. Everything needed is already in the DB except the archive calls.*

We already store `LiveSource.tv_archive`; we already emit `tv_archive_duration`/`timeshift` keys as
zeros in the Xtream payload (`playlist_gen.py:264-265`). EStalker shows the exact recipe:

1. capability gate from R6 (`tv_archive` in `available_modules`) **and** `tv_archive`/`archive == 1`
   per channel (`catchup.py:263-289`);
2. `type=epg&action=get_week` → the list of dates;
3. per channel+date: `type=epg&action=get_simple_data_table&ch_id=&date=&p=` paging with
   `total_items`/`max_page_items`, keep only `mark_archive == 1`, cut off at `now − tv_archive_duration`
   hours (`catchup.py:380-496`);
4. play: `type=tv_archive&action=create_link&cmd=auto /media/<event_id>.mpg` → URL → the normal
   ffmpeg/fallback pipeline, and for the M3U/Xtream output emit the standard
   `&catchup=<start_ts>&catchupid=<event>&catchupdays=<n>` / `timeshift` parameters we already
   reserve room for.

This also gives the internal player (`hls.min.js`/`mpegts.js`) a "watch from the beginning" affordance
for free, and it is the one area where being a *proxy* beats being a *box*: we can offer archive
playback to every client type, not just Enigma2.

### R9 — Portal short-EPG as a fallback guide source — **MAYBE**

*Effort ≈ 4 h.* `get_short_epg&ch_id=&size=10` (already implemented, unused) is perfect for "what's on
now" in the GUI/player when the user configured no XMLTV source, and EStalker's fetch discipline is
worth copying: only for **visible** rows, deduped per batch, `503` retried ×2, one reauth per batch.
Do not try to build the whole XMLTV guide from it — 1 request per channel is exactly the pattern that
gets an IP banned. Cheap version: an `/api/epg/now?source_id=` endpoint feeding a tooltip.

### R10 — Things **not** to adopt

| EStalker behaviour | Why we keep our own |
|---|---|
| `verify=False` everywhere | We verify; offer an explicit opt-in flag instead (R5). |
| `make_request()` returning `None` on any error | We need error *classification* for fallback. Keep `PortalError` + codes (R4). |
| Scroll-driven lazy paging for catalogues | A server must persist a catalogue; our background jobs + budget + bulk upsert are the right shape. |
| Reauth on *every* failure (incl. network) | Blind re-handshake on a timeout multiplies load on a struggling panel. Ours: TTL + `401/403` only. (One exception worth taking: also re-handshake once on `httpx.ConnectError`/read-timeout when the client has been idle > 60 s — the panel very likely dropped the session. Add a counter so we do not loop.) |
| Hardcoded single MAG250 identity for all playlists | We multiplex; identity must be per-MAC, derived, stable, and overridable (R1.4). |
| `os.system("sync")` + `drop_caches`, `time.sleep(3)` in UI paths | Never in a container. |
| Persisting tokens to a world-readable JSON next to the config | Our tokens are process-memory only (pool). Keep it that way — no portal JWTs on disk. |
| Enigma2 skin XML, i18n `.po`, bouquet/service writing | Not our domain. |

---

## 5. Priority summary

| Rank | Item | Value | Effort | Risk |
|---|---|---|---|---|
| 1 | **R1** STB identity (fingerprint, prehash dance, headers/cookies) | unlocks picky/blocked portals | 6–10 h | low (flag-guarded) |
| 2 | ~~**R4**~~ ✅ error codes + `%mac%` + classified `PortalError` | turns "black channel" into an actionable log | 2–3 h | very low |
| 3 | **R3** consume `blocked`/expiry in MAC status & chain | stops burning quota on dead MACs | 3–5 h | low |
| 4 | ~~**R5**~~ ✅ `outbound_client` for portal + per-portal TLS flag, drop dead code | fixes a latent TLS failure mode | 1 h | very low |
| 5 | **R6** `version.js` + `get_modules` capability panel | self-documenting portals | 3 h | low |
| 6 | **R2** conditional `create_link` + link flags in DB | halves portal load on redirect path | 4 h | medium (fallback semantics) |
| 7 | **R8** TV archive / catch-up | real user-visible feature | 12–20 h | medium |
| 8 | **R7** MAC→Xtream harvest + adopt-as-source | much cheaper second source type | 5–8 h | medium (opt-in only) |
| 9 | **R9** short-EPG "now" | nice-to-have | 4 h | low |
| — | **R10** list | *guardrail: what not to copy* | — | — |

Suggested order: ~~R5~~ ✅ → ~~R4~~ ✅ → **R1** → R3 → R6 → R2 → R8 (cheap-and-safe first, then the
identity work that changes request shapes, then the feature). The mock portal gained the knobs for
the two delivered items (`create_link_error`, `token_rejects`, `mac_placeholder`, next to the
pre-existing `offline` / `slow` / `max_per_mac`, all echoed by `GET /mock/_state`);
still worth adding for the rest: `require_prehash`, `get_modules`, `version.js`,
`get_week`/`get_simple_data_table` and `type=tv_archive`, so each R above lands with a test instead
of a hope.

---

## 6. Delivered: R5 + R4

What actually landed, including two deviations from the plan and two real bugs the wiring exposed.

**R5 — one trust policy.**
* `http_client.outbound_client()` is now the only place in the app that decides TLS, and the only
  place that builds an `httpx.AsyncClient`. It grew an explicit `insecure` keyword (default False)
  that resolves to `verify=False` **for that one client**; everyone else keeps getting the shared OS
  trust context. `StalkerClient._http()` and `resolve_portal()` both go through it, so portal
  handshake, catalogue pages, `create_link`, EPG and logo fetches now share one policy —
  and the resolver no longer has its own `httpx` import to get out of sync.
  Deviation from the plan: the plan said "route portal calls *through* `outbound_client()`", which
  read as one client per request; what landed is the *factory* being shared instead, so the pooled
  long-lived client keeps its keep-alive (a fetch is dozens of paginated calls on one session).
* `Portal.tls_insecure` is a new column (added to `_NEW_COLUMNS`, so existing installs migrate on
  boot), editable in the portal modal, badged `TLS unverified` in the portal list, and **part of the
  pooled session key**, so flipping it cannot leave an old verified session attached to the MAC.
  Default off; verification is never disabled globally.
* `profile()` and `short_epg()` — which nothing ever called, and which would have bypassed the
  portal's proxy too — are deleted.

**R4 — portal language.**
* `normalize_error()` / `js_error()` / `js_has_payload()` in `client.py`. The plan missed one thing:
  real portals use `js.error` **and** `js.msg` for both refusals and chatter, and `error: 0/1/2` are
  *status codes* on some `itv` pages. A refusal is therefore only honoured when the reply carries no
  usable data; otherwise a page that legitimately contains a field named `error` becomes a phantom
  failure. `{"js":{"msg":"OK"}}` on an empty payload still reads as `ok_with_empty_payload`.
* `PortalError` gained `code` / `message` / `hint`, and a `detail()` line that reads
  `portal said limit - connection limit for this MAC (panel says it is already streaming)`.
  `MAC_SUSPECT_CODES` separates "go away, MAC" (rotate) from "the source is dead" (advance the
  chain); `status_for_error()` maps codes to `unauthorized` / `offline` / `error` instead of the old
  "every exception is an error".
* Deviation/addition: refusals arriving under **HTTP 200** with a token-shaped code — Ministra's way
  of saying "your bearer expired", with no 401 anywhere — trigger the same one-shot re-handshake +
  retry as a 401 (never a loop). That closes part of R1.2 and fixes a real stall: a revoked bearer
  stayed broken until our own 30-minute TTL expired.
* `%mac%` (and the `%25mac%25` a query round trip produces) is substituted **after** `merge_link`;
  before it, `urlencode(..., safe=":")` re-encodes the placeholder and the repair never matches.
* `is_hls()` on the resolved link adds `-protocol_whitelist` and `-allowed_extensions ALL`,
  **each tested independently** — a single `"-protocol_whitelist" in cmd` gate lets a template that
  sets one flag silently lose the other, which is how this was written first and how a user would
  have inherited a black channel.
* Tests: `tests/test_portal_tls_and_errors.py` (23, driving the real `StalkerClient` against the
  mock ASGI app rather than stubbing `profile()`), plus 17 new cases in `dev/check-links.py`.

**Three bugs found while wiring it up** — all pre-existing, all fixed here:
* `routers/api_portals.py` called `resolve_portal()` **without** `proxy_url` while the MAC
  verification just below built its own client *with* it: a portal configured to go through a proxy
  resolved its base path over the open internet — on IP-locked panels, precisely the request that
  must not leak.
* `fetch_jobs._prepare_client` handed `proxy_url` to its throw-away verification client but
  called `POOL.get(...)` for the real catalogue work **without** it: a proxied portal could be
  resolved, could stream, and could not be fetched. The proxy now reaches all three call sites
  (resolve, fetch, stream).
* `app/main.py::_reap_sessions` referenced `asyncio`, imported only inside `_lifespan`, so every
  300-second pass died with `NameError`, was swallowed by its own `except Exception` and logged
  "session reaper failed". Idle portal sessions were therefore never reaped — one socket per MAC for
  the life of the process, with a log line claiming the safety net had a hiccup while it never ran.
  Now imported at module level, with a regression test that asserts a reap actually happens.
* `fetch_jobs._sync_season_links` reported its failure with `log.exception` in a module that has no
  `log` (it reports through `db_log`) — a `NameError` raised *inside* the `except` that exists to
  prevent bookkeeping from failing a fetch, so the fetch job failed and the real error was lost.
  It is `db_log` now, also with a fail-before/pass-after test.

The pattern is worth naming, because it is the reason these survived: a bare `except Exception` that
logs turns a bug into a *feature that quietly never runs*. All three of the above were
import/typing slips of exactly one line; a `ruff check` (F821 catches all of them) is the cheapest
possible guard, and it is not in this repo's CI yet — that is now the only thing in this document I'd
add to the backlog that EStalker never mentioned.

Not done, deliberately: R1, R2, R3, R6, R7, R8, R9 — see the ranking above for why.

---

## 7. Legal note: do not copy code

EStalker ships no `LICENSE` (only `README.md`: "Enigma2 - IPTV Ministra stalker player") and
`CONTROL/control` credits `Maintainer: kiddac`, `Source: linuxsat-support.com`. Absent a license we
must assume **all rights reserved**: take the *protocol knowledge* (endpoint names, parameter names,
quirk handling — facts and interfaces, not expression), re-implement in our own style, and keep the
attribution comment in commit messages (e.g. "behaviour observed in kiddac/EStalker @032967f")
without pasting their code or strings verbatim. The long `ver`/`ImageDescription` string in R1 is a
device-advertisement value used by real MAG firmware, not original authorship — still worth writing
ours from a captured `set_profile` rather than lifting the literal from their source.
