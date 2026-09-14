# EStalker vs. Stalker Proxy Manager — the **streaming / playback** path

Compared on 2026-09-14.

| | ours (SPM) | EStalker |
|---|---|---|
| Repo | `birdy1974/stalker-proxy-manager` @ `26032ae` (branch `arena/01a0a15c-…`) | `kiddac/EStalker` @ `c91dbfe` ("Allow multiple playlist txt files", 2026-09-02) |
| Version | Phase 3 delivered | `1.49-20260902` (`CONTROL/control`) |
| Scope of this doc | **only the streaming path**: how a `cmd` becomes bytes on the screen | idem |
| Companion doc | [`ESTALKER-COMPARISON.md`](ESTALKER-COMPARISON.md) — the *portal interface* layer (discovery, handshake, `get_profile`, catalogue fetch, error codes, Xtream harvest) | — |

**Nothing streaming-related changed upstream since the last comparison.** `032967f → c91dbfe` is one
commit touching `plugin.py`/`processfiles.py`/`server.py`/`settings.py` (multiple portal `.txt` files),
`utils.load_playlists_all()` (lazy config read) and one local variable in `vodplayer.py`. The
`create_link` policy, the streamtype selection, the player and the catch-up code are byte-identical to
what [`ESTALKER-COMPARISON.md`](ESTALKER-COMPARISON.md) already scored, so R1–R7/R9 in that document
are still correct and still marked delivered. This document answers the *other* half of the question:
**playback**.

**Do not copy code, only behaviour.** EStalker ships no `LICENSE`; see §7 (*Legal note*) of the companion doc. Everything
below is protocol/behaviour description plus our own implementation notes.

---

## 1. The one difference that explains all the others

They are not two implementations of the same streaming design — the *emulator lives on opposite sides
of the network*:

```
EStalker (client-side STB emulator)

  remote → [Enigma2 box]  ── handshake / get_profile / get_ordered_list ──▶ [panel]
                 │  create_link at zap time (only when the channel asks for it)
                 └── eServiceReference(1|4097|5001|5002|8193, panel CDN URL) ──▶ gstreamer / exteplayer3
                     bytes flow  panel CDN ───────────────────────────────▶ box   (nothing in between)

SPM (server-side STB emulator)

  remote → [Enigma2 box]  ── HTTP GET /play/live/42.ts?u=box&p=… ──▶ [SPM]
                                                                      │ create_link / stored-link fast path
                                                                      │ MAC occupancy + ordered fallback chain
                                              ┌───────────────────────┴─────────────────────┐
                                    redirect: 302                              proxy: ffmpeg pipe
                                    box ─────────▶ panel CDN                   SPM ◀─── panel CDN
                                    (identical to EStalker,                    box ◀── SPM (MPEG-TS or
                                     minus the box knowing the panel)          Matroska, transcoded/remuxed)
```

Consequences, all of which follow from that picture:

* In **redirect mode** SPM's playback is *behaviourally EStalker's playback*, with the portal identity,
  the token handling and the `create_link` decision moved off the box and onto the server. Same
  latency, same zero-CPU profile, same "the panel's own container and subtitle tracks", and the box
  never learns the portal URL or holds a portal token (`app/routers/output.py:269`,
  `app/services/stream_manager.py:1484`).
* In **proxy mode** SPM does something EStalker structurally cannot: rewrite the transport stream
  (transcode HEVC/4K → H.264 1080p on Quick Sync, remux into Matroska so text subtitles survive,
  downscale for a DM800se, re-encode bitmap subs into DVB tracks) and swap the source *mid-response*
  when a link dies (`app/services/stream_manager.py:1690` `_pump`).
* EStalker's box-side fallbacks (streamtype cycling, resume points, aspect-ratio poking) are things a
  server **cannot** do for the box — and SPM's server-side fallbacks (MAC chain, stall detection,
  route affinity) are things a box **cannot** do for itself. Neither list is a defect in the other.

---

## 2. Streaming-path scorecard

`ES` = EStalker file:line, `SPM` = ours. "Winner" is about the streaming path only.

| # | Aspect | EStalker | SPM | Winner |
|---|---|---|---|---|
| 1 | **When `create_link` runs** | at zap time, only if the channel says `use_http_tmp_link`/`use_load_balancing` or the panel set `force_ch_link_check`; legacy rows fall back to a URL-shape heuristic (`localhost`, `///`, `/ch/`, no `http`) — `live.py:1706-1723`, `liveplayer.py:936-960` | same conditional policy (R2, `app/portal/links.py:208`) **plus one stricter rule**: a stored link carrying a session token (`play_token`, `token`, `sig`, `e`, … `links.py:67`) is *never* played as-is, and one looser rule: the ffmpeg path always asks, because a refusal there *is* the liveness answer for the chain | **tie by design** — we already adopted their idea and documented the two deviations |
| 2 | **What the player receives** | the panel's own URL (`live.py:1772`) | redirect: the panel's own URL via 302; proxy: an SPM MPEG-TS/Matroska pipe | **SPM** (choice per item) |
| 3 | **`ffmpeg `/`ffrt ` prefix in `cmd`** | string-split off and ignored — the URL is handed to gstreamer regardless (`live.py:1758-1760`) | `extract_url()` picks the URL (`links.py:76`) and the template *actually runs ffmpeg*, which is what the prefix means on a real MAG | **SPM** (faithful), ES (cheaper) |
| 4 | **`%mac%` substitution** | `re.sub(r"%mac%", self.mac, …, IGNORECASE)` (`live.py:1756`) | `apply_mac_placeholder()` handles `%mac%`, `%MAC%`, `%25mac%`, `%25MAC%` (`links.py:96-108`) | **SPM** (double-encoded variants) |
| 5 | **Answer repair** | none — the panel's `js.cmd` is used verbatim | `merge_link()` restores request parameters a panel blanked (`&stream=392166` → `&stream=`) while the fresh token always wins (`client.py:103`, used at `client.py:1102`) | **SPM**, and it is the single trick ES lacks that most often decides "plays / doesn't play" |
| 6 | **`js` answer shapes for `create_link`** | dict **and list**: takes the first candidate with a `cmd`, skips entries with `type == "ad"`, keeps `storage_id` (`vodplayer.py:1080-1096`, `catchup.py:802-806`) | ✅ **now dict, str and list** (`links.read_link_answer`, used by `client._ask_for_link`): first non-ad candidate wins, `storage_id` is logged, and an unusable answer *names its own shape* in the `no_url` error | **was ES, now tied** — see S-A (delivered) |
| 7 | **`/media/<id>` VOD cmds** | resolves the movie first (`type=vod&action=get_ordered_list&movie_id=`) and rewrites to `/media/file_<id><ext>` (`vodplayer.py:1040-1065`, `vod.py:2437-2474`) — on *every* play | ✅ **now a repair, and a remembered one**: tried only after a refusal that can mean "wrong form", and the form that worked is stored on the row (`media_cmd`), so the next play asks with it directly | **was ES, now SPM** — see S-B (delivered); they pay a request per play, we pay one per item |
| 8 | **Player / service reference** | chosen by the *user* per playlist: live `1`\|`4097`, VOD `4097`\|`5001`\|`5002`\|`8193` (`playsettings.py:80-93`), auto-raised to `4097` for `.m3u8` (`live.py:1764-1767`) | chosen by the *profile* per content kind and per item: `player_live/vod/series` (defaults `4097`/`5002`/`5002`), `container_mode=auto` derives `.ts`/`.mkv` from the item's own ffmpeg template, and `1` is auto-raised to `4097` whenever the bytes cannot be DVB-piped (`enigma2_bouquets.py:61-77`, `:153-240`, `models.py:691-703`) | **SPM** (per item, and it knows the container); ES still wins on `8193` (DreamOS) — see S-E |
| 9 | **HLS** | streamtype switch only; gstreamer must cope (`live.py:1764`) | `is_hls()` adds `-protocol_whitelist` + `-allowed_extensions ALL` to the ffmpeg input, output is always TS/MKV so the box never sees an m3u8 (`client.py:146`, `stream_manager.py:494`) | **SPM** |
| 10 | **Codec/container mismatch** | the box decodes whatever the panel serves, or it doesn't; the only remedy is cycling the engine by hand (`toggleStreamType`, `liveplayer.py:759`, `vodplayer.py:901`) | ffmpeg templates: VAAPI/QSV transcode, Matroska remux with `-c:s copy`, DVB-subtitle re-encode, AC3 audio fallback when TS cannot carry the source audio, Annex-B BSF only for codecs that need it (`stream_manager.py:_subs_gate/_remux_gate/_ensure_annexb`) | **SPM**, decisively — this is the whole reason the product exists |
| 11 | **Dead link / dead source** | nothing automatic. A `create_link` refusal gives one of four MessageBox strings (`limit`, `nothing_to_play`, `link_fault`, `access_denied`); a link that resolves and then dies is a black screen until the user zaps | ordered chain over sources × MACs, MAC occupancy lock, 25 s stall timeout, source swap **inside the same HTTP response** so the player keeps playing, per-route affinity + breaker, zap-overlap retry, and (experimental) a pre-handout liveness probe on the redirect path (`stream_manager.py:83,127-179,239,1484,1690,1921`; `redirect_guard.py`) | **SPM**, decisively |
| 12 | **Concurrency & fairness** | one MAC per playlist entry — `(domain, port, mac)` each becomes its *own* playlist (`processfiles.py:206-261`); no arbitration; `limit` is shown to the user | occupancy per MAC (1 pipe), redirect lease `SPM_REDIRECT_LEASE_S=180`, per-user `max_connections`, 429 instead of a hung client (`stream_manager.py:231-302`, `output.py:227`) | **SPM** |
| 13 | **Session lifecycle during playback** | token + headers persisted to `playlists.json` on the box; `reauthorize_portal()` on *any* falsy response, exactly one retry (`liveplayer.py:919-933`, `utils.py:512`) | pooled session per (portal, MAC), TTL 3000 s, re-handshake on 401/403 and on a 200 `{"error":"token"}`, nothing on disk (`client.py:435-478`, `pool.py`) | **SPM** (no tokens on flash); ES wins on retrying transport errors too — already noted as R10's one exception |
| 14 | **Portal "I am still watching" hygiene** | `type=itv&action=set_last_id` after every zap and `type=watchdog&action=get_events&init=` every 80 s per playing service (`liveplayer.py:481-510`, `:722-726`; `vodplayer.py:521-532`) | nothing | **ES — see S-D** (cheap, opt-in, and the only playback-side behaviour we simply do not emit) |
| 15 | **Seek / resume / timeshift** | full: HTTP Range against the CDN, `service.seek()`, resume points pickled to `/etc/enigma2/estalker/resumepoints.pkl` with a "resume at h:mm:ss?" prompt (`resumepoints.py`, `vodplayer.py:302-349`) | redirect: full Range from the CDN (seek works). proxy: **no Range → no seek** — a live pipe, documented in `README.md:299` and `docs/ENIGMA2-INTEGRATION-OPTIONS.md` S2 | **ES** on the proxy path only — see S-H |
| 16 | **TV archive / catch-up** | complete: `get_week` → `get_simple_data_table` paged by `total_items`/`max_page_items`, filtered on `mark_archive`, cut off at `now − tv_archive_duration`, played via `type=tv_archive&action=create_link&cmd=auto /media/<event_id>.mpg` in the VOD player (`catchup.py:380-506`, `:779-845`) | none. `LiveSource.tv_archive` is stored (`fetch_jobs.py:742`) and then hard-zeroed in output (`playlist_gen.py:428-429`) | **ES — the one real feature gap, see S-C** (= R8 in the companion doc) |
| 17 | **Subtitles** | whatever the panel's file carries, exposed only if the user picked `5002`/exteplayer3 | per-item: Matroska + `-c:s copy` (text tracks survive), `subs=dvb` re-encode for TS, gate that refuses codecs the muxer cannot hold (`stream_manager.py:566-660`, `:734`, `:814`; `docs/ENIGMA2-INTEGRATION-OPTIONS.md:21-37`) | **SPM** |
| 18 | **TLS / timeouts** | `verify=False` on every call, GET `(8, 8)`, POST `10` (`utils.py:86-125`) | verification on, per-portal `tls_insecure` opt-in in the GUI, `PORTAL_HTTP_TIMEOUT=10` | **SPM**, decisively |
| 19 | **Diagnosability of a failed play** | four MessageBox strings, `print()` when `debugs` is on | classified `PortalError.code` + a human sentence per decision (`LinkPolicy.reason`), stream log per attempt, `Server-Timing` on every response, dashboard kill switch | **SPM** |
| 20 | **Non-Enigma2 clients** | none — it *is* an Enigma2 plugin | the same resolved stream serves VLC, Kodi, TiviMate, Smarters, the browser preview (M3U + Xtream API + `/play/…`) | **SPM** |

---

## 3. What EStalker's playback does that we should not copy

Repeating the companion doc's R10 verdicts where they touch streaming, because they are the tempting
ones:

| EStalker behaviour | Why not |
|---|---|
| Hand the panel URL to the box directly, always | That *is* our redirect mode — but as the only mode it loses transcoding, subtitles, the fallback chain, multi-user output, and it leaks the panel host + token to every receiver. Redirect stays a per-item choice, not the architecture. |
| `verify=False` | No. |
| `make_request()` → `None` on any failure | A proxy must tell "retry the same MAC", "rotate MAC", "next source" and "report" apart; `PortalError.code` is that vocabulary. |
| Reauth on *every* falsy response | Blind re-handshake on a timeout multiplies load on a struggling panel. Ours: TTL + 401/403 + 200-token. (The one exception worth taking is unchanged from R10: a single re-handshake after a transport error on a session idle > 60 s, with a counter.) |
| `toggleStreamType()` engine cycling | The server cannot change the box's engine at runtime. Our equivalent is choosing the right player *per item* when writing the bouquet (`container_mode=auto`) — already done, and it beats cycling because it is decided with knowledge of the container. |
| Resume points, aspect ratio, `drop_caches`, `os.system("sync")` | Box-side concerns. A proxy has no business writing `/proc` or pickling the user's playback position; players and the box's own VOD plugins do that better. |
| One hardcoded MAG250 identity string for all playlists | Already solved per-MAC (R1.4). |

---

## 4. The gaps worth acting on (streaming-only)

Ordered by value ÷ effort. S-A and S-B are **defect-shaped**: they are not features we lack, they are
answer shapes we mis-read, and each one turns a playable channel into "no usable url".

### S-A — list-shaped `create_link` answers (+ ad skipping, `storage_id`) — ✅ **DELIVERED 2026-09-14**

*Effort was ≈ 1–2 h incl. tests and a mock-portal knob; see §7 for what shipped.*

`app/portal/client.py:1087-1092` accepts `js` as a dict or a bare string. Ministra panels answer
`type=vod`/`type=tv_archive` `create_link` with **a list of candidates** when the storage selection or
an ad insertion is involved; EStalker handles exactly that shape
(`vodplayer.py:1080-1096`: first entry with a `cmd` whose `type != "ad"`, keeping `storage_id`;
`catchup.py:802-806`: first entry with a `cmd`). With our current reader such a panel yields
`raw = ""` → `PortalError(code="no_url")` → *every* VOD item on that portal is reported dead and the
chain burns through all MACs before failing. The failure is silent in the worst way: the log says "the
portal returned no playable URL", which reads like a dead channel rather than an unparsed answer.

Fix: in `create_link`, accept `list` — pick the first entry with a usable `cmd`, skipping
`type == "ad"`; log the candidate count and the chosen `storage_id`; keep `no_url` for a genuinely
empty list. Add `create_link_list=1` / `create_link_ad=1` knobs to `app/portal/mock_portal.py` next to
`create_link_error` and assert in `tests/test_link_flags_and_direct_play.py`. This is also a
prerequisite for S-C: TV-archive links are the shape most likely to arrive as a list.

### S-B — `/media/<id>` → `/media/file_<id>` rewrite for VOD — ✅ **DELIVERED 2026-09-14** (as a conditional retry, as recommended)

*Effort was ≈ 2–3 h; see §7 for what shipped.*

Some panels list a VOD item with a generic `/media/<id>.mpg` cmd and expect the box to resolve the
movie (`type=vod&action=get_ordered_list&movie_id=<id>&category=1&p=1`) and ask `create_link` for
`/media/file_<id><ext>` instead (`vodplayer.py:1040-1065`). We have no `/media/` handling at all
(`grep -rn "/media/" app/` → nothing).

Do **not** do what EStalker does (resolve every `/media/` cmd up front — that is one extra portal
request per play on panels that are perfectly happy with the stored form). Do it as a *repair*:
when `create_link` on a `/media/…` cmd fails with `no_url`/`nothing_to_play`/`link_fault`, resolve the
movie id once, retry with `/media/file_<id><ext>`, and cache the rewritten cmd on the `VodSource` row
so it costs one request ever, not one per play. Log both attempts — that log line is the difference
between "this panel needs the file_ form" and "this channel is broken".

### S-C — TV archive / catch-up — **DO NEXT** (unchanged from R8; still the only user-visible feature gap)

*Effort ≈ 12–20 h. Recipe in the companion doc §4 R8, verified against `catchup.py` at `c91dbfe`.*

Everything needed is already in the database (`LiveSource.tv_archive`, `fetch_jobs.py:742`) or already
reserved in the output payload (`tv_archive_duration`/`timeshift`, `playlist_gen.py:428-429`). The
portal calls are `type=epg&action=get_week`, `type=epg&action=get_simple_data_table&ch_id=&date=&p=`
(filter `mark_archive == 1`, cut at `now − tv_archive_duration` hours), then
`type=tv_archive&action=create_link&cmd=auto /media/<event_id>.mpg` — the answer feeds the *existing*
ffmpeg/fallback pipeline, so playback needs no new code path. Capability gate comes from R6
(`tv_archive` in `available_modules`, `capabilities.py:191`).

Worth noting for the streaming side specifically: an archive item is a **file**, not a live stream, so
it is the one kind where redirect mode gives the box real seeking (Range against the CDN) *and* our
proxy mode can offer it to VLC/Kodi/Smarters too. Being the proxy is what makes this feature reach
every client type — EStalker can only ever give it to one box. Depends on S-A.

### S-D — `set_last_id` + `watchdog` keepalive — **MAYBE, opt-in, default off**

*Effort ≈ 2–3 h.*

EStalker tells the panel "this is what I am watching" after every zap (`type=itv&action=set_last_id`)
and pings `type=watchdog&action=get_events&cur_play_type=&event_active_id=0&init=` every 80 s while
something plays. We emit neither, ever.

Value for us is *not* the events (we do not want the panel's ad/notice payload). It is that some panels
use the watchdog to decide a box is alive: a MAC that handshakes, fetches and never watches can be
flagged idle, and on a few panels that affects `limit` accounting and the "last played channel" a real
MAG would resume on. Cost is real too — it is recurring portal traffic proportional to active streams,
which is exactly the pattern that gets an IP throttled.

Recommendation if implemented: **one keepalive per active MAC, not per stream**, period
`SPM_WATCHDOG_INTERVAL` (default off, e.g. 120 s when enabled), only while that MAC actually has a
stream open or a redirect lease, and a per-portal GUI switch next to `identity_mode`. `set_last_id`
only on the redirect path (it is a lie on the proxy path — the panel's stream id is not what the client
is watching once ffmpeg is in the middle). Skip entirely if the panel's `get_modules` does not list
`watchdog`: we already fetch and store the whole module list (`enabled_modules()` /
`supports()`, `capabilities.py:279`/`:294`), where `watchdog` is deliberately *displayed but gating
nothing* (`capabilities.py:194-197`). This would be its first use as a gate, so it must keep the
"unknown ≠ absent" rule that reader already follows: a panel that refuses `get_modules` gets no
keepalive, not an unconditional one.

### S-E — service reference `8193` (DreamOS gstreamer) — **cheap, only if a DreamOS box joins the fleet**

*Effort ≈ 0.5 h.*

EStalker offers `8193` when `/usr/bin/apt-get` exists, i.e. DreamOS (Dreambox One/Two,
`vodplayer.py:119`, `playsettings.py:92`). Our `PLAYERS` dict stops at `5002`
(`enigma2_bouquets.py:61-66`) and `FFMPEG_PLAYERS` would need it too, or the auto-raise rule
(`1 → 4097`) leaves a DreamOS user unable to pick their own engine. The known fleet in this project is
OpenPLi/Vu+ plus a DM800se (old openpli, not DreamOS), so this is a 3-line change to make when — and
only when — somebody actually runs DreamOS. Do not add it speculatively: every entry in that dict is a
GUI choice a user can get wrong.

### S-F — automatic player-engine fallback — **NOT IMPLEMENTABLE from the server (do not try)**

`toggleStreamType()` is a remote-control action; the server cannot change the engine of a running
service reference. The nearest server-side equivalents already exist: per-item player selection,
`delivery_mode=proxy|redirect`, and `?mode=proxy` on the URL. What *is* worth doing (and is a GUI/docs
item, not a streaming one) is making the bouquet preview show **why** an item got the player it got —
`_Resolver` already collects those notes (`enigma2_bouquets.py:220-240`); they just need to be visible
per row in the Enigma2 tab.

### S-G — resume points — **DON'T**

Box-side state, better handled by the box's own player/VOD plugins, and impossible on a proxied pipe
(no Range). Redirect mode already gives real seeking, which is the part users actually miss.

### S-H — seeking in proxied VOD — **ONLY IF redirect mode is not an option for you**

*Effort ≈ 8–12 h, medium risk.*

Not an EStalker feature — a consequence of our architecture that EStalker never has: in proxy mode a
VOD `.mkv`/`.ts` is a live pipe, so Enigma2 (and every other player) cannot seek. Two ways out, both
already documented in `docs/ENIGMA2-INTEGRATION-OPTIONS.md`: prefer **S1 redirect** whenever the codec
fits the box (zero CPU, full Range, original subtitles), or **S2 remux** when the portal must stay
hidden. A third way — mapping an incoming `Range: bytes=N-` to a fresh `ffmpeg -ss <t>` spawn against
the same source and serving 206 — is buildable (we already re-spawn ffmpeg mid-response for fallback,
`_pump`), but it needs a duration/index probe per item, it costs a new `create_link` per seek on
tokenised panels, and it will feel worse than a 302. Verdict: **do not** unless there is a concrete
case where redirect is impossible *and* seeking matters (a panel whose CDN blocks the box's IP, or a
transcode the box cannot live without).

---

## 5. Is it advisable to implement? — the verdict

The question has four plausible readings; here is each answer.

| Reading | Verdict |
|---|---|
| **(a) Adopt EStalker's playback model** — let the Enigma2 box talk to the portal itself, SPM only supplies lists | **NO.** We already have that model as *redirect mode*, minus the downsides: no portal credentials or tokens on the box, `create_link` retried across a MAC chain before the 302, and the same URL still playable from VLC/Kodi/Smarters. Turning it into the only mode would delete transcoding, subtitle remuxing, per-user output and mid-stream fallback — i.e. the product. |
| **(b) Adopt EStalker's `create_link` policy** (conditional, flag-driven) | **ALREADY DONE** (R2, `app/portal/links.py`), and deliberately stricter in one place (a stored link with a session token is never replayed) and looser in another (the ffmpeg path always asks, because the refusal is the liveness signal). Nothing left to implement; the only open item here is the *answer parsing*, which is S-A. |
| **(c) Adopt EStalker's streaming-side behaviours we lack** | **S-A and S-B: ✅ done** (2026-09-14 — small, defect-shaped, and prerequisites for S-C). Still open: **YES for S-C / TV archive** (the one real feature gap, and the item already on the wish list in `improvements.md`), **opt-in MAYBE for S-D**, **defer S-E**, **don't do S-F/S-G/S-H**. Remaining effort for the recommended set: ≈ 14–23 h, of which S-C is the bulk. |
| **(d) Ship an SPM Enigma2 plugin instead of bouquets** (i.e. become EStalker) | **NO** — already analysed as option B in `docs/ENIGMA2-INTEGRATION-OPTIONS.md:50-51`: the box would pick the streamtype, we would lose per-item transcode-vs-redirect control, and we would inherit a second codebase that breaks on every plugin/image update. Bouquet generation + FTP push + OpenWebif reload (`enigma2_bouquets.py`, `enigma2_push.py`) is the right shape and it is atomic-by-rename, which is more than EStalker's JSON rewriting is. |

### Recommended order

1. ~~**S-A** list-shaped `create_link` answers~~ ✅ delivered (§7).
2. ~~**S-B** `/media/file_` retry~~ ✅ delivered (§7) — and S-A's reader is what makes its answer
   readable in the first place.
3. **S-C** TV archive / catch-up (12–20 h) — the feature; its `type=tv_archive` answers arrive as a
   list, which S-A now reads.
4. **S-D** watchdog/`set_last_id` keepalive, per-portal opt-in (2–3 h) — only if a panel is observed
   treating our MACs as idle; do not add it pre-emptively, it is recurring portal traffic.
5. **S-E** `8193` (0.5 h) — when a DreamOS box appears.

Leave untouched: redirect mode, the fallback chain, `merge_link`, the ffmpeg gates, the bouquet player
logic, TLS policy. On the streaming path those are all *better* than EStalker's equivalent, and the
comparison above found no case where copying theirs would fix anything we have.

---

## 6. One caveat about this comparison

EStalker was read from source at `c91dbfe`; nothing was executed (it needs an Enigma2 box, a skin and a
live panel). SPM's side was read from source too — no real panel was contacted for this document, which
is the same limitation the README already states. That is precisely why S-A and S-B should be
implemented *defensively* (log both shapes, keep `no_url` for the truly empty answer, cache the rewrite)
rather than on the assumption that one panel's shape is the standard: we cannot enumerate the panels,
we can only refuse to mis-read an answer they give us.

---

## 7. Delivered (2026-09-14): S-A + S-B

Both implemented as *behaviour*, in our own style, with the witnesses a NAS can run. Nothing was copied
from EStalker; the field names (`type: "ad"`, `storage_id`, `movie_id`, `category`, `file_`) are the
panel's own vocabulary.

### S-A — every `create_link` answer shape is read

* `app/portal/links.py`: `LinkAnswer` + `read_link_answer(js)` — a pure reader for dict / bare string /
  list / empty / unknown. List rules: skip any entry whose `type` is an ad label (`ad`, `ads`, `advert`,
  `advertisement`, `commercial`, `promo`, any case), take the **first** remaining candidate that carries
  a `cmd`/`url`/`link` (panels order candidates by preference), keep the `storage_id` that came with it.
  `LinkAnswer.describe()` is the sentence that goes into the failure, because "no playable URL" is what
  a dead channel looks like and what an unparsed answer must not look like.
* `app/portal/client.py`: `create_link` was split — the request/parse/repair steps now live in
  `_ask_for_link()`, which reads the answer through `read_link_answer` and logs a list answer
  (`panel answered a list of N candidate(s) plus M ad(s), storage X`). `no_url` keeps its code and now
  carries the shape.
* Mock portal knob `create_link_list` = `one` | `ad` | `ads_only` (the last is the case that must read
  as "no link", not as a crash and not as the ad's URL).

### S-B — the two cmd forms of one VOD file, as a repair that is remembered

* `app/portal/links.py`: `generic_media_ref()` (only a **relative** `/media/<id>` token counts — an
  absolute URL is a link, not a storage reference, and is never rewritten), `media_file_form(cmd,
  file_id=None)` (keeps any `auto `/`ffmpeg ` prefix and the extension; `file_id=None` is the cheap
  same-number guess), and `CmdRepair` (what we asked with, what worked, how).
* `app/portal/client.py`: `create_link(…, item_id=, alt_cmd=)` now tries the row's forms in order and,
  only when a refusal can mean "wrong form" (`MEDIA_REPAIR_CODES` = `no_url`, `nothing_to_play`,
  `link_fault`), runs `_media_form_repair()`: resolve the file id via
  `type=vod&action=get_ordered_list&movie_id=&category=1` (`_media_file_id`, every failure is `None`,
  never an exception that would mask the panel's own refusal), else fall back to the `file_` prefix on
  the stored number. `limit`/`access_denied`/transport errors never trigger it — that is about the MAC,
  and asking again in another shape would burn a slot. Kill switch `SPM_MEDIA_CMD_REPAIR=0`.
  The winning form is handed back on `client.last_cmd_repair` (reset every call: a pooled client is
  shared, so it is a hand-off slot, not state).
* `app/portal/links.py` `plan_for()` reads the row once for both stream paths: ask with the learned
  `media_cmd` **first**, keep the catalogue `cmd` as `alt_cmd`, and carry `portal_item_id` as `item_id`.
* `app/models.py` + `app/database.py`: new nullable `media_cmd` on `vod_sources` and `serie_episodes`,
  with idempotent `ALTER TABLE`s for existing installs. `cmd` is never rewritten — the catalogue stays
  the panel's truth — and `_vod_fields()` in `app/services/fetch_jobs.py` deliberately leaves
  `media_cmd` alone, so a re-fetch cannot wipe what a play learned. A stale learned form self-heals:
  it is refused, the catalogue form is tried, and the row is re-learned (writing NULL is an action).
* `app/services/stream_manager.py`: `_store_media_cmd()` persists the learned form on both stream paths
  (redirect *and* ffmpeg — on the ffmpeg path before the pipe is opened, so a form that got a link is
  kept even if ffmpeg then fails on the bytes) and logs the swap in the stream log.
  `app/services/item_info.py` asks with the same two forms so a detail popup does not re-pay the
  refusal, but never persists: a tooltip must not be what rewrites a source row.
* Mock portal knobs `media_form=file_only` + `media_file_id`, and the counters `media_refusals` /
  `media_resolutions` — the witnesses that the second play costs neither.

### Verification

* `tests/test_create_link_answers.py` — 56 tests: the pure readers, the plan plumbing, the client
  against the mock portal (list/ad/ads-only, the ladder and its rungs, the kill switch, and that a
  `limit` refusal triggers no second attempt), and the stream path learning a form and then not paying
  for it again.
* `dev/check-links.py` — 25 new pins (141 → 166 checks), so the recognition rules are also verifiable
  on a NAS without pytest.
* Full suite: **646 passed, 5 skipped, 2 failed** — the two failures are the documented pre-existing
  ones (`test_ffmpeg_argv_injects_annexb_when_copying_to_mpegts`, and
  `test_an_existing_install_gets_the_columns_it_is_promised`, whose own setup drops the FK column
  `users.area_id`). `dev/check-links.py`: 5/166 failed, the same five pre-existing drifts as before
  this change.
