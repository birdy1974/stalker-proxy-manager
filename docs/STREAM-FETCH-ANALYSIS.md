# How a stream is fetched (Enigma2 & Playlist) — tokens, zapping, start latency

Scope: the per-request path from the box/player to the panel, for both output
surfaces (**Enigma2 bouquets** and the **Playlist / M3U / Xtream API**), plus a
comparison with reference applications (EStalker, STB-Proxy, crispy-stalker,
iptvnator) and a prioritised list of what to implement.

Everything below was read from this checkout (commit `2dab7ba`) and reproduced
against the built-in mock portal with a real static ffmpeg
(`SPM_MOCK_PORTAL` on, `SPM_REDIRECT_VALIDATE=1`). Measurements are in
[Appendix A](#appendix-a--measurements).

---

## 0. TL;DR

* **There is no per-stream SPM token.** Every Enigma2 bouquet line and every M3U
  line carries the SPM *user/password* (`?u=&p=`), nothing else. The panel
  token is server-side and invisible to the player.
* Three different things behave like "the token", and only one of them is a
  token in the usual sense:
  1. the panel **handshake token** (bearer + `token` cookie), pooled per
     (portal, MAC), TTL 3000 s — used for `portal.php` calls only;
  2. the **per-link token inside the URL the panel returns** from `create_link`
     (`play_token=`, `hdnts=`, …) — this is what actually authenticates the
     *media* fetch, and it is the thing that expires / is single-use;
  3. the **Enigma2 profile pull token** — download of the bouquet tar ball only,
     never a stream.
* Intermittent playback and the failed zap are **not** a token bug. They are a
  **single-connection-per-MAC race**: SPM holds the MAC itself (`mac_locks` /
  redirect lease, 180 s) *and* the panel holds its own slot (HTTP 456/403
  "account is in use", ~6.5 s to clear on a real panel per the comment in
  `app/services/stream_identity.py`). The zap is lost to whichever of the two
  clears last — and the code paths react to "busy" with either an **instant 404**
  (proxy) or **2.5 s + 502** (redirect), never with "wait and retry".
* Start latency has four additive sources: panel round trips per zap
  (handshake/get_profile/create_link), the redirect liveness probe (up to
  `2 × 2 s` per candidate), ffmpeg's input probe on the copy path, and the
  client's own buffering. The 2.5 s zap retry and the 12 s per-candidate
  silence inflate the *failure* case to tens of seconds.

---

## 1. The two entry points, one engine

```
Enigma2:  bouquets.tar.gz  ──►  service ref 4097:0:1:<id>:…:{base}/play/live/<id>.ts?u=&p=
M3U:      playlist.m3u      ──►  #EXTINF … {base}/play/live/<id>.ts?u=&p=
Xtream:   /get.php /player_api.php ──► same /play/... URLs (or the bridge)
                                        │
                                        ▼
                     app/routers/output.py  /play/{live,vod,episode}/{id}.{ts,mkv}
                       ├─ _wants_redirect()  ← ?mode=  OR the item's effective template
                       ├─ redirect → stream_manager.resolve()  → 302 + lease (no ffmpeg)
                       └─ proxy    → stream_manager.open() + _pump() → ffmpeg pipe
```

* Enigma2 bouquet lines are built by `enigma2_bouquets.stream_url()`
  (`app/services/enigma2_bouquets.py:326`): `/play/{kind}/{id}.{ts|mkv}{?u=&p=}`,
  plus `&mode=proxy|redirect` when the profile's delivery mode is not `template`.
* M3U lines are built by `playlist_gen._build_m3u()` (`app/services/playlist_gen.py:224`):
  `?u=<user>&p=<password>` on every line. `mode=` is **not** added, so the
  delivery mode of each line is whatever the item's effective template resolves to
  (`_wants_redirect`, `app/routers/output.py:234`).
* Both surfaces end in the same `/play/...` route, so everything in this
  document applies to both. The differences that remain are the *player*:
  Enigma2/gstreamer stops the old service before it opens the new one; VLC and
  TiviMate frequently open the new URL first (and often issue a probe GET or
  HEAD before the real one). With one MAC and a one-connection panel slot, that
  difference alone decides who gets a picture.

---

## 2. Which token is used, exactly

| # | "token" | Where it lives | Lifetime | Who presents it |
|---|---------|----------------|----------|-----------------|
| 1 | SPM **user/password** | query string of every bouquet/M3U/Xtream line (`?u=&p=`) | until changed | the player, to SPM (`UserAuth.verify(..., need="stream")`) |
| 2 | Panel **handshake token** | `Authorization: Bearer <t>` + `token` cookie, held in the pooled `PortalSession` per (portal, MAC, identity…) | `SPM_TOKEN_VALIDITY=3000 s` (`app/portal/client.py:58`), session idle TTL 900 s | SPM → `portal.php` (`handshake`, `get_profile`, `create_link`, lists) |
| 3 | Panel **per-link token** | inside the URL `create_link` returns (`play_token`, `hdnts`, …; `VOLATILE_PARAMS`) | panel-defined; often minutes or single connection | the **media fetch**: the box itself (redirect mode) or ffmpeg (proxy mode). SPM strips/merges it on stored `cmd` URLs (`sanitize_cmd`/`merge_link`) |
| 4 | Enigma2 **profile pull token** | `/enigma2/<token>/bouquets.tar.gz`, `/install.sh` (`app/routers/api_enigma2.py:297`) | until rotated | the receiver's installer/cron job only — never a stream |
| 5 | *(not a token)* the panel's **one-connection-per-MAC slot** | panel-side | ~seconds after the old connection dies | behaves exactly like a short-lived token: the same URL works now and 456s a second later |

Consequences worth knowing before debugging anything:

* **The URL the player is given is never panel-authenticated.** It is
  authenticated against *SPM* (row 1). If a channel "sometimes plays and
  sometimes doesn't" while the SPM credentials never change, the failure is in
  rows 2/3/5, not in the URL the player holds.
* **The media fetch carries no panel bearer.** ffmpeg is started with
  `-user_agent` + `-referer` only (`app/services/stream_manager.py:733-737`,
  identity chosen by `app/services/stream_identity.py`). Panels that require the
  session at media level can therefore only work in **redirect** mode, where the
  box presents the panel's own link itself. That is one legitimate reason why
  "direct works, copy doesn't" on some portals.
* **Row 2 is refreshed silently.** `_get()` re-handshakes once on HTTP 401/403
  and on an HTTP-200 `{"error":"token"}` (`app/portal/client.py:456-518`) — and
  it does this for *any* 403, including the "account is in use" 403 a panel
  returns while the MAC's slot is still held. See §4.2; this is a real cost on
  every lost zap.
* Stored `cmd` URLs for permanent links are handed out as-is (`link_policy`,
  `app/portal/links.py:196`); a `play_token` inside them is treated as volatile
  and forces a `create_link`. For permanent links (no token), the redirect path
  can hand out the stored URL with **no panel call at all** — that is the
  fastest possible start in the code base today, and it is what
  `_wants_redirect` + `plan.policy.direct` are for.

---

## 3. One play, step by step (with numbers)

### 3.1 Redirect / direct template

`output._stream_response` → `stream_manager.resolve()` (`:1844`):

1. `_live_chain(ref_id)` → sources × (portal, MACs), ordered by
   `route_health` (affinity TTL 1800 s) and then by `demote_recently_handed`
   (300 s window — *the link we handed out last is pushed to the back*).
2. Per candidate: `is_mac_busy()` + `lease_holder()` — own lease may be taken
   back, another user's may not.
3. `plan_for(...)`: `plan.policy.direct` → **no panel call**, probe the stored
   URL, lease, 302. Otherwise `POOL.get` → `ensure_auth()` → `create_link()` →
   probe → lease → 302.
4. Failure → next candidate; after a full pass, sleep `SPM_ZAP_RETRY_DELAY`
   (2.5 s) and walk the chain again. Then 502
   `"…: no source produced a link to redirect to"`.

Measured locally (warm pooled session, mock portal): resolve **48 ms**, total
**52 ms**; a second play of a channel whose 302 is still leased **302 in
~3 s** only when the mock answers every portal request with the 3 s "slow"
toggle. With a slot held by another candidate: **2.5 s → 502**.

### 3.2 Proxy / copy / transcode template

`output._stream_response` → `_ensure_slot()` (`:313`, retries once after 1.2 s)
→ `stream_manager.open()` (`:2001`) → `_pump()` (`:2085`):

1. `open()` pre-checks **"is at least one MAC free"**; if not, it returns
   `dead=True` immediately and the route answers **404** — measured **13 ms**.
   There is no wait and no retry for this case (the 1.2 s retry exists only for
   the user's `max_connections`).
2. `_pump` walks the chain: for adopted (Xtream) sources it plays the stored
   URL; **for every MAC-based source the plan is always "ask"**, i.e. a fresh
   `create_link` (`:2201`, comment: *"ffmpeg owns this stream, so the plan is
   always 'ask'"*).
3. The MAC lock is taken **before** ffmpeg starts (`:2295`), ffmpeg is spawned,
   and the first bytes must arrive within `STREAM_START_TIMEOUT` (12 s, `app/config.py:107`) or the
   candidate is discarded and the walk continues (whole walk bounded by a 75 s
   budget). After a full silent pass: 2.5 s, second pass, then 502.
4. The output guard waits `max(25 s, start_budget + 10 s)` before declaring
   "produced no data".

Measured locally: first byte **570 ms** end-to-end (of which ffmpeg itself is
**534 ms** for a plain HTTP MPEG-TS input with the shipped default input
options); with the mock's 3 s portal delay: **3.56 s**.

---

## 4. Symptom → mechanism → evidence

### 4.1 "Sometimes the same channel plays, sometimes it doesn't"

| Mechanism | Why it is intermittent | Evidence |
|---|---|---|
| Panel slot still held after the previous player | Panel answers 456/403 until its connection table clears (code comment: measured ~6.5 s on a real panel) | `app/services/stream_identity.py` module docs, `UA_POLICY_4XX` (456), `FAST_REFUSAL_S=5 s` |
| A player that probes first (VLC/TiviMate: GET-and-abort, then GET) | The aborted GET starts ffmpeg and takes the MAC lock; the real GET arrives while it is held → immediate **404** | measured: second concurrent request while one pipe is live = `404` in 13 ms |
| Stored/permanent link whose `play_token` aged out | Policy says "permanent", so no `create_link`; the probe verdict is "inconclusive = alive"; the box gets EOF/456 | `app/portal/links.py` (`REBUILD_FLAGS`, `VOLATILE_PARAMS`), `link_is_alive` (inconclusive ⇒ alive) |
| Route affinity vs demote | A zap within 300 s is *deliberately* sent to a different (source, MAC) than the one that just worked | `ROUTE_AFFINITY_TTL=1800`, `DEMOTE_WINDOW=300` |
| UA ladder | A 4xx slower than 5 s is treated as "slot", not identity, so the ladder gives up on the candidate (correct) — but the candidate is burned for the whole walk | `FAST_REFUSAL_S` in `stream_identity.py` |

### 4.2 "Switching to another channel does not play"

Ordered by how often it will be the cause:

1. **The old stream's MAC lock is still held.** `open()` fails fast with 404
   (`all MACs occupied -> 404` in the log) and never takes over the pipe of the
   same user — unlike the *redirect* lease, which an owner may take back.
   With one MAC this is fatal; the player sees a hard error and gives up.
   Measured: 13 ms 404 while a pipe on the same MAC is live.
2. **The panel slot is still held.** Then `create_link` answers 403/456 →
   `_get()` treats 403 as an auth problem, throws the token away and
   **re-handshakes** (2 extra HTTP round trips, and on some panels a fresh
   handshake invalidates the session the box is still using), retries, still
   fails → `PortalError` → candidate burned → after the pass, 2.5 s wait → the
   whole chain again → 502. Measured: redirect zap into a held slot =
   `502` after **2.51 s**; the proxy path = 0 bytes, `fail_note='2 attempt(s)
   without data'`.
3. **No MAC to fall back to — or the wrong one tried first.** `_pick_macs`
   filters banned/expired MACs; with `macs_first` and one MAC there is no
   alternative, and with several the chain used to be walked in affinity order,
   so the MAC the player just left (whose panel slot is still counted) was tried
   first while an untouched one sat in the same chain. A portal configured
   `portal_first` never even sees the other MACs: one per portal per pass.
   
   *Measured on the reference proxy's topology (several MACs, 1 stream per MAC):
   it walks the MAC list and plays the first `isMacFree()` one, so a zap lands on
   the MAC whose slot is actually free. SPM now does the same — see the P0 items
   below, all implemented: a zap to another channel starts on the free MAC (200
   in 556 ms on a two-MAC portal while another user held the first), and with
   nothing free the answer is 503 + `Retry-After` after one chain-wide wait.*

### 4.3 "Switching back sometimes plays the first channel"

* The same user may **take over its own redirect lease** (`resolve()`, log line
  *"taking over the redirect lease on … held by …"*) — this is the one place the
  code explicitly treats a zap as a hand-over rather than a conflict.
* `REAP_GRACE = 45 s` (`:333`): a registered pipe that never produced bytes is
  freed by the reaper, so a retry after a minute succeeds.
* The 2.5 s × 2-pass retry means the second pass often lands *after* the slot
  cleared, which is indistinguishable from "switching back works".

### 4.4 "It takes a long time before the channel plays" (direct *and* copy)

Additive, measured where possible:

| Phase | Cost | Note |
|---|---|---|
| Handshake (cold session) | 2 HTTP GETs | avoided by `portal_warmup` (boot + every 600 s) if the MAC was not busy |
| `get_profile` + `create_link` per candidate | 1–2 RTT each | × sources × MACs × 2 passes |
| `link_is_alive` probe (redirect, `SPM_REDIRECT_VALIDATE=1`) | HEAD → ranged GET → GET, `2 × 2 s` budget per candidate, plus possibly a second identity | extra connection to the media origin; on a one-slot panel the probe can be the connection that makes the box's own fetch fail |
| ffmpeg input probe (copy) | ~0.5 s locally, more on a slow remote TS | shipped defaults are `-fflags +genpts+discardcorrupt` (+`-flush_packets 1`); `fast_input` (`-analyzeduration 1M -probesize 1M`) is only an optional preset |
| Double hop (copy) | panel → SPM → box | redirect has a single hop |
| Client buffering (Enigma2/gstreamer, VLC) | 1–3 s | outside SPM's control |

Failure paths dominate the perceived slowness: 12 s silence per candidate,
75 s engine budget, `max(25 s, budget+10)` guard → a zap that cannot get the
slot can hang the player for ~25 s before the 502, and every extra MAC adds
12 s.

---

## 5. Differences vs the reference applications

| Aspect | SPM (this repo) | EStalker (Enigma2 plugin) | STB-Proxy | crispy-stalker |
|---|---|---|---|---|
| Who fetches the media | SPM (redirect: the box goes straight to the CDN) | the box, straight to the panel URL | the proxy (ffmpeg pipe) or 302 | n/a (library) |
| `create_link` usage | always on the proxy path; redirect path when the link is not permanent (`link_policy`) | only when the channel's link flags say the link is not permanent, or the cmd looks local | one `create_link` per play, all flags `false`, no caching | on demand, session-stateful |
| Panel session | pooled per (portal, MAC), 3000 s token, warmup job | one token per plugin session, re-auths once on an empty answer | re-fetches token + profile + full channel list **per play** per MAC (no cache — slow, but no stale state) | in-process session object, exponential backoff |
| MAC/slot handling | `mac_locks` + 180 s redirect lease, no preemption (own lease excepted), instant 404 when all MACs are busy | no lock at all: the box owns the single connection | per-MAC stream counter; rotates MAC order (`moveMac`) after a failure; optional "try all MACs" | n/a |
| Pre-play validation | `link_is_alive` probe (redirect), UA ladder (proxy) | none | optional ffprobe gate with a timeout | none |
| Fallback | source × MAC chain, route affinity, 2-of-45 s source breaker, 2 passes | one re-authorize, then the panel's error text | fallback channels by name across portals | retry/backoff |
| Start-up tuning | templates; defaults keep ffmpeg's probe limits | the box's own player | ffmpeg opts + 1024-byte pump | n/a |
| Token in the URL given to the player | none (SPM user/pass) | the panel's own URL, `%mac%` substituted, leading token stripped | SPM-equivalent (proxy URL) or the panel URL on 302 | n/a |

**The structural difference:** EStalker is an *emulator that hands over* — the
box owns the panel connection, so there is nothing to race with. STB-Proxy is
*stateless per request* — slow, but no lock can go stale. SPM is the only one
that both keeps a long-lived panel session **and** mediates the media, so it
owns a slot that the panel also owns. Every symptom in §4 is a consequence of
that duplication.

---

## 6. What to implement (prioritised)

> **Status 2026-09-21:** the P0/P1 items below are implemented on this branch
> (see `improvements.md`, "zap robustness: occupancy is a hint, never a veto"),
> with the defaults and the kill switches named in each item. Measured on a live
> instance after the change: zap with the previous channel's pipe still open =
> **200 in 566 ms** (was 502 after 2.5 s), same channel again = **200 in 554 ms**
> (was 404 in 13 ms), panel-reported busy slot = **503 + Retry-After: 2** (was
> 502 after 2.5 s), zap back to the previous channel = 302 with
> **no create_link** (log: *"replaying the link resolved 0s ago"*).

### P0 — make a zap land

1. ~~**Wait for a busy MAC instead of 404-ing (proxy path).**~~ **DONE** —
   `BUSY_WAIT_S` (3 s, 250 ms polls) in both places, and the final answer is
   503 + `Retry-After: 2`, not 404. **Refined for multi-MAC portals:** the walk
   takes the first MAC *nothing* holds (`prefer_free_mac`, default on) and waits
   once for the whole chain instead of once per busy MAC — waiting per MAC turned
   a two-MAC portal's zap into seconds of nothing while a free MAC sat in the
   same chain.
2. ~~**Preempt the *same user's own* pipe.**~~ **DONE** — `preempt_own()`,
   same "same user only" rule as the lease, same log sentence
   (*"taking over the ffmpeg pipe on this MAC … (the channel this zap left)"*).
3. ~~**Do not re-handshake on a busy-slot 403.**~~ **DONE** —
   `SLOT_BUSY_CODES` + `refusal_code()` read the 403's body before reacting, and
   `_create_link_with_backoff()` re-asks the same MAC after 0.5/1/2 s
   (`SPM_BUSY_BACKOFF`) inside the start budget.
4. **Retire (or shrink) the redirect liveness probe.** It is already marked for
   deletion in `improvements.md`, and it is the only component that adds a
   full extra media connection to every 302. If it stays: probe only when the
   verdict can change the decision (stored/permanent links), one rung, 1 s
   timeout, and never probe a fresh `create_link` answer.

### P1 — make it fast

5. ~~**Reuse the last good link for the redirect path.**~~ **DONE** —
   `_LINK_CACHE` (`SPM_LINK_CACHE_S`, 90 s, live only, probed before replay).
   `demote_recently_handed` still applies to the *fresh* resolve when the cache
   misses, so the guard experiment and the cache coexist.
6. ~~**Zap retry tuning.**~~ **DONE** — the busy ladder above; the 2.5 s pass
   retry stays for the "no data" cases.
7. **Live start-up options in the default template.** Add the already-defined
   `fast_input` opts (`-analyzeduration 1000000 -probesize 1000000`) to the
   resilient default for live sources, and `-fflags +nobuffer` /
   `-muxdelay 0 -muxpreload 0` for live copy → the pipe starts on the first
   keyframe instead of after the demuxer's analysis window. Measured locally the
   difference is small (534 ms), but on a remote TS with a slow first keyframe
   it is the difference between "instant" and "a few seconds".
8. **Prefer redirect for live when the panel's link is permanent** — still
   advice, but no longer load-bearing: the pipe now plays the stored link too
   (R2b, `SPM_FFMPEG_USE_STORED_LINK`).

### P2 — make it diagnosable and robust

9. **Player-visible reason codes.** Keep the 502 for "no data", but return
   `409/503 + Retry-After: 2` for `slot-busy` so a player that retries
   (Enigma2/IPTV clients do) can win the race instead of showing "no signal".
   Put `handle.fail_note` / `trace` into the response body — the log already has
   them (`startup timing`, `all MACs occupied -> 404`).
10. **Streams per MAC is a setting now** (`portals.streams_per_mac`, blank = 1) —
   raise it only for a panel that really allows two or three links on one MAC.

11. **Enigma2 token trap.** The generated installer is designed to be run from
    cron (`enigma2_bouquets.py:659-691`) against `/enigma2/<token>/install.sh`;
    rotating the pull token silently breaks every cron refresh. Either serve a
    stable path, have rotation push the new URL, or log when a bouquet was built
    before the last rotation.
12. **Document the single-MAC reality in the UI.** One MAC = one concurrent
    stream at the panel, no matter what `max_connections` says; the GUI should
    warn when a user/area pins a channel to a MAC that another playlist uses, and
    `_pick_macs`'s `portal_first` mode should be the default recommendation for
    one-MAC portals.

---

## Appendix A — measurements

Environment: mock portal at `/mock/c/`, static ffmpeg 7.0.2, local, warm pooled
session (unless stated), `SPM_REDIRECT_VALIDATE=1`.

| Scenario | Result |
|---|---|
| Redirect resolve, warm session | `302`, `resolve;dur=48.3`, total 52 ms |
| Copy/passthrough first byte, warm session | `200`, first byte 576 ms, `source+ffmpeg+first-byte=559 ms` |
| ffmpeg alone (same URL, shipped default input opts) | first bytes after 534 ms |
| Mock portal slowed by 3 s per request — redirect | `302` after 3.06 s |
| Mock portal slowed by 3 s per request — copy | first byte 3.56 s |
| Second GET while a copy pipe on the only MAC is live | before: `404` after **13 ms**; after the fix: `200` in **554 ms** (own pipe taken over) |
| Panel refuses the link as "already streaming" (proxy) | before: `502` after 9.5 s with `http_403` ×2; after the fix: **`503` + `Retry-After: 2`**, same walk, log says *"all 2 refusal(s) were busy slots"* |
| Zap with the previous channel's own pipe still open | before: `502` after **2.51 s** (or `404` in 13 ms when the pipe, not a lease, was the blocker); after the fix: `200` in **566 ms** |
| Zap back to the previous channel (redirect) | after the fix: `302` in 46 ms, log *"replaying the link resolved 0s ago … (no create_link)"* |
| Own 302 lease taken over by the same user | allowed, log line emitted |
| Another user's 302 lease | skipped, `dead` |

### Reproduce

```bash
# mock-portal demo (see README for the environment variables)
SPM_DATA_DIR=/tmp/spm-demo SPM_MOCK_PORTAL=1 SPM_ADMIN_PASSWORD=… \
  .venv/bin/python -m uvicorn app.main:app --port 8880

curl -s -o /dev/null -w '%{http_code} %{time_starttransfer}\n' \
  'http://127.0.0.1:8880/play/live/1.ts?u=box&p=pw'          # proxy/copy template
curl -s -D - -o /dev/null 'http://127.0.0.1:8880/play/live/2.ts?u=box&p=pw'  # redirect template
curl -s -X POST http://127.0.0.1:8880/mock/_control \
  -H 'content-type: application/json' -d '{"slow": true}'     # 3 s on every portal.php call
curl -s -X POST http://127.0.0.1:8880/mock/_control \
  -H 'content-type: application/json' -d '{"max_per_mac": 1}'  # force the slot race
```

Watch the log for: `startup timing: …`, `all MACs occupied -> 404`,
`fallback step i/N`, `taking over the redirect lease …`, `server-timing`.

---

## Appendix B — reference material read for this analysis

* **EStalker** (Enigma2 plugin, `kiddac/EStalker`): `Plugins/Extensions/EStalker/live.py`
  (~1715) decides `create_link` from the channel's link flags; the box plays the
  panel URL itself; `%mac%` substitution, leading-token strip, error mapping
  `limit` / `nothing_to_play` / `link_fault`, one re-authorize on an empty answer.
* **STB-Proxy** (`Chris230291/STB-Proxy`): `stb.py:getLink()` one-shot
  `create_link`; `app.py:/play/<portalId>/<channelId>` re-fetches token, profile
  and the full channel list per MAC and per play, optional ffprobe gate, ffmpeg
  pipe in 1024-byte chunks or 302, `moveMac` reordering after failures, fallback
  channels by name.
* **crispy-stalker** (`docs.rs`): session-stateful client (token + cookie +
  device identity), exponential backoff, portal discovery — the same shape as
  SPM's pool, without the media mediation.
* **iptvnator** (`4gray/iptvnator`): Stalker handler creates a link and hands the
  CDN URL to the player.

---

## Appendix C — Why a zap works in STB-Proxy and not in SPM

Short version: **STB-Proxy never had the problem.** It keeps no session, no link,
no MAC lock and no lease across requests, so there is no proxy-side state a zap
can collide with: it re-asks the panel from scratch on every play and treats
"busy" as a hint, never as a veto.

| # | STB-Proxy (`/tmp/stbproxy`, commit `84eedf0`) | SPM (this repo) |
|---|---|---|
| 1 | Play URL is stateless: `/play/<portalId>/<channelId>` (`app.py:749`, built at `569-650`) — no user, no credentials, no MAC, no token | `/play/live/<id>.ts?u=&p=` → per-user auth plus `_ensure_slot` (`max_connections` → 429 after one 1.2 s retry) |
| 2 | Everything re-fetched per play, per MAC attempt: `getToken` → `getProfile` → `getAllChannels` (`app.py:851-870`, `stb.py:66`) — no token cache, no pooled session | pooled `PortalSession` per (portal, MAC), 3000 s token, warmup job — faster, but the state outlives the play |
| 3 | `create_link` only when the *fresh* cmd points at `http://localhost/` (`app.py:871`, `stb.py:183`); otherwise the URL from the just-fetched channel list is used | `link_policy` decides from cached flags; the proxy path always calls `create_link` |
| 4 | Occupancy is a soft counter: `occupied` + `isMacFree()` (`app.py:822`), gated by `streams per mac` — and **`0` disables the gate** (`if streamsPerMac == 0 or isMacFree()`, `app.py:851`). Entries drop in `finally: unoccupy()` the moment the client's generator ends (`app.py:766-800`) | hard `mac_locks` (one stream per MAC, no setting) plus a 180 s `redirect_leases` entry; `is_mac_busy` → **instant 404** (`stream_manager.py:2062`) or 2.5 s → 502 (`resolve()`) |
| 5 | After a failure: `moveMac()` rotates that MAC to the back of the order (`app.py:176`, called at `930`) and, with *try all macs*, the same request walks the rest (`app.py:926`) | `route_health` affinity pins the same (source, MAC) for 1800 s; the failed candidate is retried only after the 2.5 s pause |
| 6 | Optional ffprobe gate per play (`testStream`, `app.py:802`, default on, 5 s timeout) — a dead link moves to the next MAC inside the *same* request | 12 s of silence per candidate, 75 s whole-chain budget, then a 502 |
| 7 | No per-user connection accounting at all | `_ensure_slot` 429s at `max_connections` |
| 8 | ffmpeg/ffprobe lifetime is tied to the request (`finally` kills the process, `app.py:798`) | watchdog 0.5 s + `kill()` + reaper with `REAP_GRACE` 45 s |

What STB-Proxy does **not** solve: the panel's own one-connection-per-MAC slot.
With a single MAC and `streams per mac = 1` it answers `503 No streams available`
too (`app.py:1042`); it just happens to be configured (unlimited streams per MAC,
several MACs, or *try all macs*) so that it never refuses on its own account. Its
statelessness is also what makes it slow to start (3-4 portal round trips per
play) and it has no mid-stream fallback — a link that dies is EOF for the player.

### What to port into SPM — status

1. ~~Occupancy must never be a veto~~ **DONE** (wait + preempt own + 503).
2. ~~"Streams per MAC" as a setting~~ **DONE** (`portals.streams_per_mac`; blank
   = 1, and unlike STB-Proxy a 0 is not silently "unlimited" — it is rejected).
3. ~~`moveMac` behaviour~~ **DONE** (`FAILURE_DEMOTE_S`, per route, no config file).
4. ~~A "force fresh" retry that ignores our own state~~ **DONE** for the panel's
   slot (`_create_link_with_backoff`) and for the stored-link shortcut
   (`SPM_FFMPEG_USE_STORED_LINK=0` restores "always ask").
