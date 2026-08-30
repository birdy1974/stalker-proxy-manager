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
| 3 | Request identity | `X-User-Agent`, `Referer: …/index.html`, `Pragma`, `Accept-Encoding/Language`, `Host`, `Connection: Close`, cookies `mac/stb_lang/timezone/adid` + `token` cookie | the same set minus `Host`/`Connection: Close`, which we refuse on purpose (pooled keep-alive, and a proxy sets its own `Host`); cookies `mac/stb_lang/timezone` + `adid` on `/stalker_portal/` + `token` after handshake — on every request, **including discovery** | **us** (R1 ✅): same announcement, minus the two headers that hurt a proxy |
| 4 | Adaptive headers | regex-scrapes `set_cookie('x')` / `setRequestHeader('X-…')` out of `xpcom.common.js` and only sends what the portal declares | fixed set | **ES** (nice idea, fragile) |
| 5 | Handshake | 2 shapes (with/without explicit `mac=`), `"missing"` → fake bearer + `prehash=sha1(fake)` retry, reads `js.random`, `js.not_valid` | the same three stages (R1 ✅), plus the `Bearer` header **and** `token=` cookie set together, `js.not_valid` echoed back on the next call, and one transparent re-handshake when the panel expires the bearer under HTTP 200 (R4) — which EStalker does not handle | **us**, narrowly |
| 6 | `get_profile` | full MAG250 fingerprint (sn, device_id, device_id2, signature, hw_version, hw_version_2, metrics, api_signature, prehash, not_valid_token) + minimal fallback | same fingerprint, derived deterministically per MAC (22 params) with the same no-`id` → minimal fallback, plus a per-portal `identity_mode` switch and a `SPM_STB_PROFILE=0` kill switch EStalker has no equivalent of | **us**: equal announcement, and it can be turned off without a code change |
| 7 | Account state | `account_info` expiry **plus** `js.status`, `js.blocked`, `force_ch_link_check`; playlist marked invalid if `blocked==1` | all of it (R3 ✅): `blocked`/`status`/`force_ch_link_check`/`play_token` persisted per MAC, expiry parsed from either source, `banned`/`expired` excluded from chains, and the panel's own reason surfaced in the badge tooltip | **us**: same truth, and it survives the request that read it |
| 8 | Token/session lifecycle | token+headers persisted to JSON, re-`handshake` via `reauthorize_portal()` on **any** failed call, exactly 1 retry (24 call sites) | pooled client per (portal,MAC), TTL 3000 s, `401/403` → re-handshake + 1 retry, idle reap 900 s, hit/miss stats | **us** (TTL, pooling, reuse, stats); ES wins on "retry on transport errors too" |
| 9 | Catalogue fetch | lazy, scroll-driven paging (14/page), `pages_downloaded` set, `all_data[total_items]` positional array, `/tmp/allchannels.json` | background jobs, `get_all_channels` first, 4-way concurrent paging, budget, per-page bulk upsert, resumable, progress in GUI | **us** (server-side need); ES's positional page placement is a good idea we already get from `gather` ordering |
| 10 | Series/VOD type duality | hardcodes `type=vod` for VOD, `type=series` for series | tries `series` then `vod`, validates season/episode rows, id-shape tolerance | **us** |
| 11 | `create_link` policy | **conditional**: only when the channel says `use_http_tmp_link`/`use_load_balancing` or portal says `force_ch_link_check`; else play the stored `cmd` as-is; fallback heuristic for legacy rows (`localhost`, `///`, `/ch/`, no `http`) | **always** resolves (proxy needs an absolute URL + it is the "is this source alive?" signal) | **ES** on portal load & start-up latency, **us** on correctness/uniformity — see R2 |
| 12 | Link post-processing | `%mac%` substitution; `ffmpeg `/`ffrt ` prefix strip; `urlparse().geturl()` normalisation; `.m3u8`→streamtype | prefix strip, stale-token strip, **answer repair** when the panel drops `&stream=…` (`merge_link`), and — since R4 — `%mac%` substitution plus `.m3u8`→HLS input options | **us**: repair is the more valuable trick, and we now have their two as well |
| 13 | Portal error semantics | maps `js.error` → user-visible text: `limit`, `nothing_to_play`, `link_fault`, `access_denied` | the same codes, in `PortalError.code` + `detail()`, and *plus* a decision each one drives (`limit` = rotate MAC, `nothing_to_play` = advance the chain), a per-code check status, and the payload guard on `js.msg` (R4 ✅) | **us** |
| 14 | Portal EPG | `get_short_epg&size=10` for the visible rows only, deduped, `503` retry ×2, one reauth generation | the same fetch discipline, as `GET /api/epg/now`: visible ids only, deduped per batch, capped at 60, 2-min cache, `503` ×2, **XMLTV tried first** so a configured guide costs the panel nothing (R9 ✅) | **tied** — they still build a whole guide, we deliberately do not |
| 15 | TV archive / catch-up | full: modules gate, `get_all_channels`+`archive` filter, `type=epg&action=get_week`, `get_simple_data_table` paged by `total_items`/`max_page_items`, `mark_archive`, `tv_archive_duration` cutoff, play via `type=tv_archive&action=create_link&cmd=auto /media/<id>.mpg` | none — `tv_archive` is fetched and then hard-set to `0` in output (`playlist_gen.py:264`) | **ES** (the single biggest gap) |
| 16 | Module discovery | `type=stb&action=get_modules` → `all_modules` minus `disabled_modules`, gates menu entries (e.g. `tv_archive`) | none: a portal without series/VOD is discovered by trial and error at fetch time | **ES** |
| 17 | Player hygiene | `type=itv&action=set_last_id`, `type=watchdog&action=get_events&init=` (timer currently commented out) | nothing | **ES** (small but cheap) |
| 18 | Multi-MAC / fairness | one MAC per playlist; N playlists fetched with **one in-flight request per domain per round**, ≤30 threads, sequential fallback | MAC order per portal, occupancy locks (1 stream/MAC), `macs_first`/`portal_first`, `FETCH_PAGE_CONCURRENCY=4` | **us** for MAC semantics; ES's per-domain round-robin is the missing fairness piece |
| 19 | Transport/TLS | `verify=False` everywhere, `HTTPAdapter(max_retries=0/1)`, GET `(8,8)` timeouts | TLS verification **on** for every outbound call, `PORTAL_HTTP_TIMEOUT=10`; portal traffic shares the one TLS policy with EPG/logos, plus a per-portal `tls_insecure` opt-out in the GUI (R5 ✅) | **us**, decisively |
| 20 | Xtream interop | harvests `/movie/<user>/<pass>/` from a `create_link` answer, then queries `player_api.php` for status/`created_at`/`exp_date`/`active_cons`/`max_connections`; auto-delete of invalid playlists | we harvest the same credential (also from `/live/` and `/series/`, refusing a 32-hex `play_token`), read the same account fields, and let the operator **adopt** it per channel — the URLs bypass `create_link`, the token and the MAC slot; nothing is automatic and no playlist is deleted on a guess (R7 ✅) | **us**, on safety; **ES**, on doing it at all |
| 21 | Code quality | screen-centric, `print()` debugging, broad `except`, mixed Py2/Py3, no tests | typed, module loggers with masked tokens, 21 pytest files (184 tests), CI smoke test, mock portal that also *records what it received* (R1/R3 ✅) | **us**, decisively |

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

### R1 — Emulate the STB properly (fingerprint + full auth dance + headers) — **DO, highest value** ✅ *delivered* (see §6.2 for the three deviations)

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

### R2 — Make `create_link` conditional and *correctly* so — **DO** ✅ *delivered* (see §6.3, which corrects the flag list below)

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
  *(correction, from the implementation: the gate is `use_http_tmp_link or use_load_balancing or
  force_ch_link_check` — `disable_ad` is not part of it, it is a `create_link` parameter. See §6.3.)*
* always `create_link` when a real ffmpeg pipe is opened (we want the fresh `play_token` and the
  liveness check) — plus `%mac%` substitution and `.m3u8` detection (see R4).

### R3 — Consume portal truth in Portal/MAC status — **DO** ✅ *delivered* (see §6.2)

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
  them; both are currently a trap for the next reader. *(deleted, since nothing ever called them —
  `get_profile` returned in R1 as `stb_profile()`, and `short_epg` came back in R9 as the pooled
  client's method, which is the difference between a trap and a feature: the replacement goes through
  the proxy and TLS policy instead of building its own client)*

### R6 — Capability & version probing on *Resolve* — **DO (cheap, big DX win)** ✅ *delivered* (see §6.3)

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

### R7 — MAC → Xtream bridge (optional, but it is the most *useful* trick in EStalker) — ✅ *delivered* (see §6.4)

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

### R9 — Portal short-EPG as a fallback guide source — **MAYBE** — ✅ *delivered* (see §6.4)

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
| 5 | ~~**R6**~~ ✅ `version.js` + `get_modules` capability panel | self-documenting portals | 3 h | low |
| 6 | ~~**R2**~~ ✅ conditional `create_link` + link flags in DB | halves portal load on redirect path | 4 h | medium (fallback semantics) |
| 7 | **R8** TV archive / catch-up | real user-visible feature | 12–20 h | medium |
| 8 | ~~**R7**~~ MAC→Xtream harvest + adopt-as-source | ✅ delivered (§6.4) — much cheaper second source type | 5–8 h | medium (opt-in only) |
| 9 | ~~**R9**~~ short-EPG "now" | ✅ delivered (§6.4) — nice-to-have | 4 h | low |
| — | **R10** list | *guardrail: what not to copy* | — | — |

Suggested order: ~~R5~~ ✅ → ~~R4~~ ✅ → ~~**R1**~~ ✅ → ~~R3~~ ✅ → ~~R6~~ ✅ → ~~R2~~ ✅ →
~~R7~~ ✅ → ~~R9~~ ✅ → **R8**
(cheap-and-safe first, then the identity work that changes request shapes, then the feature).
The mock portal grew a knob set per delivered item: `create_link_error`, `token_rejects`,
`mac_placeholder` (R4), `require_prehash`/`require_mac_param`/`fingerprint_required`/`profile_mode`/
`not_valid` (R1+R3), and `version_mode`/`modules`/`modules_disabled`/`no_modules` with the counters
`version_calls`/`modules_calls`/`create_links`/`create_link_seen` (R6+R2) — all next to the
pre-existing `offline` / `slow` / `max_per_mac`, and all echoed by `GET /mock/_state`.
Still worth adding for the open items: `get_week`/`get_simple_data_table` and `type=tv_archive`.

---

## 6. Delivered

### 6.1 R5 + R4

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

Not done, deliberately: R8 (the TV archive), and the parts of R7/R9 §6.4 says were left out on
purpose — see the ranking above and the deviations for why.

---

### 6.2 R1 + R3 — the box the portal thinks it is serving

`app/portal/identity.py` and `app/portal/account.py` (both new, both pure functions), then
`client.py`, `pool.py`, `resolver.py`, `api_portals.py`, `fetch_jobs.py`, `item_info.py`,
`stream_manager.py`, `portals.html`, `api_misc.py` (backups), `mock_portal.py`.
Tests: `tests/test_stb_identity.py` (50), 19 more cases in `dev/check-links.py` (58 total),
and the full suite at 184.

**R1 — what the portal is told.**
* The handshake is three stages, as prescribed: today's request; the same with `mac=`; and, only
  when `js.msg` says `missing`, a 32-char `A-Z0-9` invented bearer plus `prehash=sha1(that bearer)`
  in a third request. `js.random` is kept as `token_random` and quoted inside the `metrics` blob,
  `js.not_valid` is kept and echoed as `not_valid_token=1`. The answer is *data*, not an exception:
  a refusal-shaped handshake reply (no token) leaves the client unauthenticated and says so.
* `stb_profile()` sends the 22-parameter fingerprint (`sn=md5(mac).upper()[:13]`,
  `device_id=sha256(mac).upper()`, `device_id2`, `signature=sha256(device_id+device_id).upper()`,
  `hw_version_2=sha1(mac)`, `prehash=sha1(sn+mac)`, `api_signature=262`, `num_banks`, `hd`,
  `video_out`, `auth_second_step`, `hw_version`, `ver`, `metrics`, `timestamp`) and falls back to the
  `sn`/`device_id=""`/`timestamp` minimal shape when the answer has no `id` — EStalker's own
  fallback, including its silent one.
* **Every** portal request, discovery included, carries `User-Agent`, `X-User-Agent: Model: MAG250;
  Link: WiFi`, `Referer: <portal dir>/index.html`, `Pragma`, `Accept-Language`, `Accept-Encoding` and
  the `mac`/`stb_lang`/`timezone`/(`adid`) cookies; after a token, `Authorization: Bearer` and the
  `token=` cookie are set and cleared together.
* Deviations from the plan, all three deliberate:
  1. **`identity_mode` and `stb_timezone` live on the Portal, not per MAC.** The plan asked for a
     per-MAC escape hatch; MACs are edited through a textarea in the portal modal, which cannot host
     a select, and the mode describes what *this panel* wants to see — which is a property of the
     portal. `sn`/`device_id` stayed per MAC, because a serial belongs to one box. GUI: MACs → ⚙ → 🎩.
  2. **The `mac` cookie is not percent-encoded** (EStalker sends `quote(mac, safe='')`). Colon form is
     what our portals have always accepted; a device that suddenly looks unknown is worse than a
     slightly less faithful cookie. The mock decodes both, so either spelling works against it.
  3. **`sn`/`device_id` are derived, not persisted, and only *overrides* are stored.** The plan said
     "persist them so a regenerated value cannot re-enrol the box" — but a value derived from the MAC
     cannot drift across restarts in the first place, so persisting it would only add a state file to
     lose. A captured real serial is stored as an override on the MAC row (and exported in backups).
* Two things EStalker announces that we refuse on purpose: `Connection: Close` (we hold pooled
  keep-alive sessions; asking every request to close doubles our handshake load) and a `Host`
  override (our client may be talking to a proxy, which sets its own).

**R3 — what the portal's answer is allowed to do.**
* `account_verdict(profile=…, info=…, token=…)` is the single decision function: a blocked panel
  wins over every date (`banned`), an unusable MAC is one whose *own* verdict said no, expiry is
  parsed from `account_info.phone` or `end_date` in both ISO and `DD.MM.YYYY` forms, and "no token"
  is `unauthorized` rather than an outage. `mac_is_usable()` deliberately only excludes
  `banned`/`expired` — our own `offline`/`error` verdicts stay retryable, because a portal that timed
  out is not a portal that said no.
* Check Portal and the nightly portal sync both call `refresh_account()`, store
  `status`/`force_ch_link_check`/`last_error` per MAC, and the GUI badge reads the panel's word
  (`banned`, `expired`) with the reason in its tooltip. `force_ch_link_check` is stored, not acted
  on: that is R2's job, and R2 is still open.
* The stream chain (`_pick_macs`) and a fetch job's starting MAC skip `banned`/`expired`. Both keep a
  documented escape: if *every* MAC looks unusable, fall back to the plain first MAC — a status that
  went stale while the panel was rude must not disable a portal forever.

**The bug this round found is mine, and it is worth keeping in the record.** The first implementation
hung the suite for 300 s: a `get_profile` that answers **403** went through the ordinary request path,
whose 401/403 branch calls `handshake()` — which calls `get_profile`. `_may_reauth()` now forbids
re-authenticating from inside a handshake, and best-effort calls on the handshake path pass
`retry_on_auth=False`. The rule generalises: *any* call added to the handshake path has to say whether
it may re-enter it. The regression test asserts a portal whose `get_profile` 404s/403s still yields a
working session, so nobody re-invents the loop.

**Test harness change (`tests/conftest.py`), and why it is here rather than in a chore commit.**
Adding DB-touching tests turned a latent order-dependency into 23 `ERROR at setup` entries
(`sqlite3.OperationalError: database is locked`) in one of every three full runs: `drop_all` needs
SQLite's exclusive lock, and `test_get_db_survives_an_abandoned_request` intentionally abandons a
connection, which holds a shared one until the GC terminates it. The fixture now drains the log writer
queue and retries the DDL through a forced `gc.collect()`. 5/5 clean full runs afterwards, versus a
baseline that had already been failing `test_deregister_deletes_row_even_when_its_task_is_cancelled`
in roughly one run in three for the same reason. A suite whose failures depend on which files ran
first teaches you nothing, and mine was the change that made it loud.

---

### 6.3 R6 + R2 — what the panel says about itself, and what we do about it

Two halves of the same idea, landed together: the portal is asked what it has (R6) and what it
expects us to do about each channel (R2), and the answers are *stored* so the next request can be
skipped or refused on evidence instead of on a guess.

**R6 — `app/portal/capabilities.py`** (pure, stdlib only, importable from `dev/check-links.py`):
`PortalVersion` (frozen; `.known`, one-line `.label`, `.public()` for JSON), `read_version_js()`,
`parse_version_js()`, `enabled_modules()`, `FEATURE_MODULES` + `supports()`/`gate_feature()`,
`dumps_modules()`/`loads_modules()`. The split between the two probes follows what they cost:
`version.js` is a static file needing no token, so the **resolver** reads it while discovering the
portal (`resolver._read_version()`, both success paths; `ResolveResult.version`/`.referer`), which is
the one moment we know the panel is answering. `get_modules` needs an authed session, so it is
`StalkerClient.version_info()`/`portal_modules()`/`refresh_capabilities()`, called from
`POST /api/portals/{pid}/resolve`, which persists `Portal.portal_version` / `modules` /
`capabilities_at` and answers `referer`, `version`, `version_url`, `modules`, `features` and
`modules_error`. Check Portal probes only when nothing is stored yet, so a nightly check does not
turn two extra requests into 200 portals × 2 requests. The GUI got a **Panel** column in the portals
table (version badge · "N modules" with the list in its tooltip · `no vclub` badge for what is
switched off · "capabilities unknown" · "press Resolve") and the resolve box now shows version,
modules and the Referer we will claim.

Three rules that carry this feature:

* **NULL is not `[]`.** `dumps_modules(None) is None`: "the panel has no modules" and "the panel
  never answered" must not share a value, because the second one would hide catalogues that nobody
  removed. `_feature_gate()` allows a fetch when the row is NULL, and a `version.js`/`get_modules`
  failure may never fail a portal check — the same principle as `get_profile` in §6.2, asserted by a
  test that turns both answers off and expects the resolve to succeed.
* **A refusal-shaped answer is still data.** `parse_version_js` rejects a body that looks like HTML
  (`<html`/`<!doctype`): a captive portal or WAF serving that file has `ver = '…'` in its markup, and
  printing a fragment of it as "the portal version" sends the user off to debug their panel.
* The first version of that guard **crashed on its own immutability**: `read_version_js` annotated the
  frozen `PortalVersion` with `version.error = …`, raising `FrozenInstanceError`, which the resolver
  swallowed — so the *only* case the guard existed for was the one that broke, and it broke into
  "no version" (indistinguishable from a panel with no file). `dataclasses.replace()` is now used, and
  the test asserts the message, not just the absence. It is recorded here because "the exception path
  is the untested path" is the same lesson as the handshake recursion in §6.2.

**R2 — `app/portal/links.py`** (which also *took over* the link helpers `client.py` re-exports, so
`VOLATILE_PARAMS`/`extract_url`/`apply_mac_placeholder` have one home): `parse_link_flags()`,
`split_flags()`/`has_flag()`/`flags_known()`, `why_not_self_served()`, `link_policy()`,
`LinkPlan`/`plan_for()`, `link_request_params()`. Storage is **one column per table** —
`link_flags String(60)`, a comma string, written by `fetch_jobs._live_fields`/`_vod_fields` and the
episode mapping — and `Portal.direct_links` (default on) is the per-portal escape hatch.
`stream_manager._plan()` is the single place a source row and a MAC row are read: `resolve()` decides
**before** `POOL.get`/`ensure_auth`, and `_pump()` passes `ffmpeg=True`.

* A permanent channel on a redirect play now costs the portal **zero requests** — asserted against
  the mock's `handshakes` counter, not against a mock of our own code, because "we skipped
  `create_link`" while still authenticating would have been an easy way to pass a weaker test.
* A transcode always asks: it wants the fresh `play_token` *and* the liveness answer, which is what
  the fallback chain is built on. This was in the plan and is the reason the fast path lives in the
  redirect branch only.
* `MacAddress.force_ch_link_check`, stored by R3 and unused since, now forces a rebuild *and* is
  forwarded as `force_ch_link_check=true`; `disable_ad` is forwarded as `disable_ad=true`. A flag the
  panel set that we neither honour nor forward is a flag we should not have read.

**Corrections to the rule text in §R2**, found while implementing it (the bullet above came from a
quick read of two files; the code disagrees, and the difference is user-visible):

* `disable_ad` is **not** a rebuild flag. The gate is `use_http_tmp_link or use_load_balancing or
  force_ch_link_check` (`live.py:1712`, `liveplayer.py:946`); `disable_ad` is one of the parameters
  EStalker *sends back* with `forced_storage`/`download`/`series` — hence `link_request_params()`.
  Gating on it would send every ad-free channel through `create_link` on every play, forever.
* `-1` means "this channel does not use tmp links", not "no". `parse_link_flags()` folds any `-1`
  into NULL (never told) rather than `""` (told: nothing applies). A client that inherits PHP's
  truthiness rebuilds links forever on such a panel; a client that reads it as "no" plays a link the
  panel may still be rotating. `""` and NULL stay distinct end to end — one takes the fast path, one
  does not.
* The plan's "skip when the template is `@redirect`" is where the policy lives, but the guard that
  makes the fast path *safe* is not in the plan: **a URL that carries a session token
  (`play_token`, `usertoken`, `hash`, … — the same `VOLATILE_PARAMS` list `strip_volatile()` removes
  when asking) is never played directly**, even when its flags are clean. A 302 to a dead token is a
  black screen with no fallback chain behind it, and we cannot tell from a stored URL whether it is
  still alive. That rule is what the earlier "unknown flags ⇒ always ask" decision was really
  protecting, so unknown rows could in principle take the fast path when their URL is plainly
  self-served (EStalker's own legacy heuristic); the shipped code stays stricter — NULL flags always
  ask — so a database that has not been re-fetched behaves *exactly* as before, and the fast path
  arrives with the fetch rather than with a migration.
* `%mac%` is deliberately **not** a blocker (we substitute it, R4). `localhost`, `///`, `/ch/` and
  "no absolute http(s) URL" are, and the template check runs *before* the scheme check so
  `/ch/101.ts` is reported as "a template the panel finishes" rather than as a malformed URL.
* `why_not_self_served()` and `link_policy()` return a *sentence*, and that sentence goes into the
  stream log (`[Ch] playing the stored link via portal/MAC: the channel flags say nothing needs
  rebuilding…`). "We skipped the portal" without "because" is a mystery for the next reader, which by
  then will be whoever reports that a channel broke after an update.

**A finding from the mock, kept because it would otherwise have been a false green.** The fixture's
`cmd`s were `http://mock/…`, which only ever *worked* because `create_link` rewrote the host — so a
fast-path play would have handed the player an unresolvable hostname, and the test for it could only
pass by avoiding the assertion. `_live_rows()` now builds absolute URLs from `request.base_url`, and
the catalogue ships five deliberate shapes (permanent · tmp+`disable_ad` · **no flags at all** ·
`use_load_balancing` · permanent-but-token-carrying) with a test asserting the shapes still exist, so
tidying the fixture cannot silently un-test the policy. In the field, a portal that hands out a link
pointing at a name the player cannot resolve is the same failure, and it is the http(s)-plus-host
requirement — not the flags — that keeps us from forwarding one.

The flag column is also *visible*: `_live_item()` puts `link_flags` in the sources API row, since
"why did this channel skip `create_link`" is a question asked with a curl in hand, not with a sqlite
prompt.

**Two consumers added beyond the plan.** `item_info.playable_url()` (the detail popup) now reads the
same policy and takes the source row, so the popup reports the URL the player would really get
instead of paying for a link nobody uses; its private `cmd_to_url()` copy of the extractor is gone
(it missed percent-encoded cmds, so the popup could disagree with the stream path about what the URL
*is*). And `api_misc` exports/imports `direct_links`, `portal_version` and `modules`, with the import
filtered to the columns this build has — a newer backup must not kill a restore.

Verification: `278 passed` (94 of them new: 42 in `tests/test_portal_capabilities.py`, 52 in
`tests/test_link_flags_and_direct_play.py`, plus `tests/mockclient.py` — the `Wired` harness moved
there so the portal test files share one transport patch, including `resolver.outbound_client`, which
is what keeps a resolve test from calling the internet and being "proved" by the sandbox proxy's 404);
`dev/check-links.py` grew from 58 to 122 cases, including grep guards that `read_version_js(`,
`portal_modules(`, `gate_feature(`, `parse_link_flags(` and `why_not_self_served(` all have callers —
the mirror-image rule of §6.1, *an answer nobody reads is dead code* — and that the link policy is
imported by at least two call sites rather than re-implemented inline.

### 6.4 R7 + R9 — the two things the panel had that we were not asking for

Both rounds are the same gesture applied to a different answer: the portal was already telling us
something useful (an entire Xtream account, in the link it hands out; the next ten programmes, in an
action nobody called) and we were ignoring it. They landed together because both are *read-only
extras with a budget attached*, and because both had to be prevented from becoming the kind of
feature that costs a household its IP.

**R7 — `app/portal/xtream.py`** (pure, stdlib only, so `dev/check-links.py` can drive it): `harvest()`
+ `looks_like_session_token()`, `xtream_base()`, `mask_password()`, `XtreamCreds` (frozen; the four URL
builders `api_url`/`stream_url`/`playlist_url`/`epg_url`, plus `public()`/`masked()`), `XtreamAccount` +
`parse_player_api()`, `parse_streams()`, `norm_title()`, `media_base()`, `plan_adoption()` → `AdoptionPlan`.
**`app/services/xtream_bridge.py`** is the part that decides: `probe()` / `adopt()` / `detach()` /
`summary()` and the observation store (`dumps_observation`/`loads_observation`/
`creds_from_observation`/`public_observation`). `api_request()` is the module's only network call and
it goes through `outbound_client(proxy=…, insecure=…)` with the *portal's* values — the Xtream host is
normally the same machine as the panel, so a portal reachable only through a proxy is unreachable
here too, and a bridge built on a bare `httpx.AsyncClient()` works in development and hangs in the
field. `StalkerClient.xtream_harvest()` asks VOD first then live (EStalker's order, because a movie
`cmd` is a plain file path), and **never raises**: a portal without a bridge is the ordinary case, and
the caller is a portal check that must still answer "portal OK".

Five rules carry it:

* **Detect and adopt are separate steps with different blast radii.** `probe()` writes only
  `Portal.xtream` + `xtream_at`; playback changes only when `xtream_adopted` is set. The probe runs on
  an explicit press and — the one automatic case — during Check Portal when nothing has ever been
  stored, mirroring the capability probes of §6.3: opportunistic, once, never fatal.
* **A credential that looks like a token is refused.** A 32-hex string in the password slot is a
  `play_token`, not an account; adopting it builds a playlist that dies at 3 a.m. and then re-asks a
  panel that is refusing us, which is how a proxy earns a ban. `harvest()` also accepts `/live/`,
  `/series/`, `/vod/` and `/streaming/<kind>/` (EStalker knows only `/movie/`), and rejects a
  relative link rather than inventing an origin to leak the password to.
* **Matching is number-first, name-second, and never a guess.** A title shared by more than one stream
  row is reported `ambiguous` and left untouched; a *number* collision falls through to the name
  rather than aborting the row. Re-adopt rewrites **every** row, including to `NULL`, so a channel the
  panel dropped cannot keep yesterday's stream id.
* **Adoption writes beside the portal data, not over it.** `xtream_url` sits next to `cmd` and
  `link_flags` on the live/VOD row, so Detach is one flag (URLs kept; `?clear=1` drops them) and the
  GUI can show both addresses. `fetch_jobs` never writes `xtream_url`, so re-fetching a catalogue
  cannot lose the mapping.
* **`plan_adopted()` is the single documented exception to "an ffmpeg template always asks"**
  (`app/portal/links.py`, `ADOPTED_REASON`): for an adopted channel there is no `create_link` left to
  call, so the transcode path plays the harvested URL and — on failure — advances to the next *source*
  instead of re-asking the panel through a MAC. Adopted plays skip the busy-MAC check, take no
  `mac_locks` entry, and `StreamHandle` reports `Portal (xtream)` with an empty MAC;
  `stream_manager._macs_for()` yields `[None]` for an adopted source with zero MAC rows, which is the
  panel that bans per-MAC sessions but hands out Xtream links.

The two things this round **deliberately does not do**: it does not adopt series (`get_series_info` per
title is a different trade than "one request, whole catalogue"), and it never auto-creates, rewrites or
deletes a playlist — EStalker auto-deletes playlists whose `player_api.php` answer stops validating,
and an app that owns a household's output cannot be allowed to remove it because a panel had a bad
minute. `Portal.xtream` stores the password **unmasked** (a backup that restored `****` would be a
silently dead bridge) while every response, tooltip and log line masks it; that makes
`GET /api/export?section=portals` a secret-bearing file, which is stated in the README rather than
hidden, and it is admin-only like the user rows it already carries.

**R9 — `app/portal/epg.py`** (pure): `Programme` (`.public()`), `parse_portal_ts()`, `parse_short_epg()`
(the full answer, a bare `js`, `{"data": […]}` and a dict-or-list, because panels spell all four) and
`pick_now()` (with `lookback_minutes=90`, so a programme that ended two minutes ago is still the
answer). **`app/services/epg_now.py`** is the budget: `now_for(live_ids, playlist_ids)`, XMLTV-first,
TTL cache (`SPM_EPG_NOW_TTL`, 120 s), one query per kind to resolve rows, dedupe per batch, cap of
`MAX_IDS=60`, `CONCURRENCY=6`, `RETRIES=2` for retryable codes only, `MAX_PORTAL_FAILURES=3` per portal
per batch, one session per portal (so at most one re-handshake per batch) and one `INFO` log line for
the batch. `GET /api/epg/now?live=…&playlist=…` fronts it and **takes no request-scoped session**: the
service opens its own short sessions, because holding a pooled connection across a portal round trip
that may take 30 s is how a tooltip starves the app of connections.

Four rules carry it:

* **A configured guide source makes this feature free.** The XMLTV lookup runs first, keyed by
  `LivePlaylist.epg_id` (the mapping that the auto-matcher maintains) or `LiveSource.epg_original`,
  and *any* answer from it counts — including "nothing now, X at 21:00" — because asking the panel to
  confirm a gap the guide already knows about is a request spent proving nothing.
* **"No guide" and "busy" are different facts**, and the distinction is why `absent`/`empty`/`flaky`
  are three separate mock modes rather than one: `{"js": []}` is an empty answer (not retried, not a
  failure), a 404 + `no such action` is a panel that does not implement it (stop asking it), and a 503
  is worth two tries. `_retryable()` keys on `PortalError.code` for exactly this reason; there is no
  HTTP status left on the object by the time it reaches us.
* **The stop-loss is per portal, not per batch.** Three answered-wrong requests and the rest of *that
  portal's* channels are skipped with the reason in `why`, while a second portal in the same batch
  keeps answering. A rude panel must not blank the tooltip on a working one, and a `gated: true` row
  (R6's `epg` module answer) is drawn as a dash rather than as a failure — a skipped row for a reason
  is not a broken channel.
* Portal guide times carry no offset, so they are parsed in `stb_timezone` — the *same* value sent as
  the `timezone=` cookie, because that is why the panel chose them. The mock renders its schedule in
  the zone from that cookie, so the chain (Portal column → cookie → parser) is exercised; a mock that
  assumed UTC would let a client pass while every non-London user read a guide two hours out.

**Five bugs this round found**, recorded because each was invisible to a weaker test - and the last
one was invisible to *every* test:

1. `XtreamCreds.masked()` built `http://h/…/john/secret/…` and handed it to `mask_password()`, whose
   path pattern keys on the *kind* segment (`/live/`, `/movie/`) — so the string it existed to redact
   came back unchanged, and this method is what a log line prints. It now builds its input through
   `stream_url()`, which is the shape the masker recognises. A masking helper that silently doesn't is
   worse than no helper, because the caller believes the output is safe.
2. `parse_streams()` had `row.get("container_extension") or row.get("container_extension")` — a copy
   of the second key, so panels that spell it `extension` silently lost the extension and adopted
   `.ts` for an `.mkv`. Asserted per-key now, not per-row.
3. `(await s.execute(select(…).where(…)
                    .order_by(…).limit(1))
                    .scalars()).first()` — `.scalars()` inside the awaited parens — parses as
   `await (s.execute(…).scalars())`, i.e. `.scalars()` on a *coroutine*: `AttributeError`, on the
   happy path of the XMLTV lookup, in a helper that no test without a database reaches. Both queries
   in `_xmltv_now()`/`_targets()` looked correct and were not. The same function then met the other
   half of the same trap: SQLite hands `DateTime(timezone=True)` back **naive** while Postgres hands it
   back aware, so `now - row.start_ts` is a `TypeError` on one backend and fine on the other — hence
   `_as_utc()` on read, and the comment saying why the column type is not the answer.
4. `harvest()` let `streaming` be a *kind* while any `[a-z_]+` segment could be its wrapper, so
   `http://h/streaming/live/u/p/1.ts` matched with `h` as the wrapper and `streaming` as the kind —
   which shifts every group by one and yields user=`live`, password=`u`. Aiming wrong credentials at a
   paying panel is the one failure a "read-only detection" feature must not have, and any short host
   name (`h`, `server`, `localhost`) triggered it. The mock's host is `test`, whose extra `.` kept the
   wrong branch out of reach — **so every unit test exercised only the correct case.** `streaming` is
   now a wrapper and nothing else, and a parametrised test pins four shapes including this one.
5. The mock originally served the panel's Xtream side at the origin root (`/live/<u>/<p>/<id>.ts`,
   `/player_api.php`), which is where a real panel serves it — and which is also **this app's own
   Xtream output API** (`output.py`, included before the mock router). Every adopted URL the demo
   produced 403'd against `output.py`'s credential check while the pytest suite stayed green, because
   the test harness mounts the mock router *bare*, with no output router in front of it. The mock now
   lives under `/mock/…`, which required (and is the reason for) the path-prefix rule in `harvest()` /
   `media_base()` above: the fixture and production now agree that a media host can be behind a prefix.
   Two lessons, both general: a harness that mounts a subset of the app can hide a route collision, and
   **"the tests are green" is not "the feature works"** — this one was only caught by booting the app,
   pressing the buttons and reading the HTTP status, which is why `dev/seed-demo.sh` + a mock-enabled
   instance is part of the round, not a curiosity.

**Verification.** `tests/test_xtream_bridge.py` (50 tests: the four harvest shapes and the token
refusal, the account parse, the matcher's number/name/ambiguous/collision cases, the observation store
and its masked views, probe/adopt/detach against the **real** mock portal, and `resolve()` returning the
adopted URL while the panel's `create_links` counter stays put) and `tests/test_epg_now.py` (20 tests,
mostly *counts*: one request per channel, dedupe, cache, truncation, XMLTV-first with zero portal
calls, the three failure modes, the per-portal stop-loss, the timezone chain, mixed live/playlist keys).
`dev/check-links.py` grew from 122 to 137 cases: the obsolete `def short_epg(` ban is gone (R9 made it
live, through the pooled client), and `WIRED_CALLER` now demands that `xtream_harvest(`,
`plan_adoption(`, `plan_adopted(`, `parse_player_api(`, `now_for(` and `pick_now(` each have a caller
**outside the file that defines them** — the shape of check that would have caught EStalker's own
`get_profile`-is-called-but-discarded pattern. `xtream_url` must be written, played *and* exported (≥ 4
of 6 files), and the mock must still be able to answer both rounds.

**Verified against a running instance**, not only against pytest: `dev/seed-demo.sh` on a
mock-enabled app, then `xtream_mode=on` → Detect (`found`, masked `api_url`, account `online`,
`exp_date` nine days out) → Adopt (**37 of 40** live channels matched by number, the 3 holes the
mock deliberately leaves reported as `unmatched`, **48 of 48** VOD matched by name) → `/api/sources/live`
carrying `xtream_url` per row → Detach (URLs kept) → `?clear=1` (URLs dropped) → our own
`/player_api.php?username=demo` still answering 200 (the output API unharmed). For R9: six live ids
asked = six panel requests, the same six again = `cache: 6, asked: 0`, `playlist=` ids answering from
the *shared* channel cache, and after assigning one `tvg-id` and re-refreshing the source, `xmltv: 1,
asked: 1` — one local row, one portal request, exactly the ordering the feature is built on. Guide times
came back `+02:00`, the portal's own zone, not the server's UTC.

This round also replaces an excuse: `node` turned out to be installed after all, so
`dev/check-js.js` now syntax-checks every template `<script>` block with the real parser (after a
text-level Jinja pass that keeps one branch of each `{% if %}`), which is the only gate that catches a
typo in the code that builds these tables — the Python tests cannot see it, and a broken script renders
as an empty table at 200 OK. It reports nothing about behaviour, which is the point: no DOM here.

**Deviations from the plan text in §4** (kept, not drift):

| Planned in §R7/§R9 | Shipped | why |
|---|---|---|
| "offer as Xtream source" as a *new source type* | `xtream_url` on the existing live/VOD rows + one portal flag | a second source type means a second set of playlists and a second genre sync for the same channels; the flag gives the same savings and detaches in one click |
| EPG "for GUI/player use" | `/api/epg/now` + the *Now* column on Sources → Live, visible page only | a player-facing guide would need the whole schedule, which is the expensive thing this item explicitly is not |
| (implied) adopt EPG URLs from `player_api.php` too | `epg_url`/`playlist_url` are *offered* in `public()` for the user to paste, never written into `EpgSource` | an ingest that rewrites a user's guide source, from a credential they only agreed to *play* with, is a bigger decision than the one they pressed |
| probe on portal test (always) | only when `Portal.xtream` is empty | one `create_link` + one `player_api.php` per portal per check is 400 requests on a 200-portal box, for an answer that does not change minute to minute |
| (implied) treat `{"user_info": []}` as "account with no status" | refusal: `status=error`, and `adopt` needs `force=1` | that is how an Xtream server answers a wrong password; adopting it would call a dead login "a source" |
| `player_api.php` at `host.rstrip("/") + "/player_api.php"` (root, always) | base = origin **plus the path prefix in front of the credential segment**, and `http_live_url` is read the same way | a server streaming from `http://h/xtream/live/…` answers its API at `/xtream/`; keeping the prefix also made the mock reachable inside a running app (bug 5 above), so the deviation is what closed the gap between the fixture and the deployment |


---

## 7. Legal note: do not copy code

EStalker ships no `LICENSE` (only `README.md`: "Enigma2 - IPTV Ministra stalker player") and
`CONTROL/control` credits `Maintainer: kiddac`, `Source: linuxsat-support.com`. Absent a license we
must assume **all rights reserved**: take the *protocol knowledge* (endpoint names, parameter names,
quirk handling — facts and interfaces, not expression), re-implement in our own style, and keep the
attribution comment in commit messages (e.g. "behaviour observed in kiddac/EStalker @032967f")
without pasting their code or strings verbatim. The long `ver`/`ImageDescription` string in R1 is a
device-advertisement value used by real MAG firmware, not original authorship — what we ship is our
own composition of the same *field names* (`ImageDescription`/`ImageDate`/`PORTAL version`/
`API Version`/`Player Engine version`) with our own values, env-overridable via `SPM_STB_VER`; no
literal from their source was copied.
