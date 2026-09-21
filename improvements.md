Quick check that I give you the exact marker text:

Verified — 4 marked blocks in stream_manager.py, 1 in conftest.py. Paste this in a future session and I'll handle the rest:

Delete the redirect-guard experiment: remove app/services/redirect_guard.py and tests/test_redirect_guard.py, then remove every block between the # >>> redirect-guard and # <<< redirect-guard marker comments in app/services/stream_manager.py and tests/conftest.py. Verify with grep that no redirect-guard markers or references remain, then run the full test suite and confirm it's green apart from the 2 known pre-existing failures.

That's all I need — the markers make everything self-locating, so no line numbers or extra context required. When you say that, I will delete the two files, cut the five marked blocks, grep to prove nothing is left, and re-run the suite as confirmation.

Two notes:

Say "delete" only if you mean the code gone. If you just want it off (reversible, no code change), the instruction instead is: "disable the redirect guard via SPM_REDIRECT_VALIDATE=0 / SPM_REOPEN_DEMOTE=0" — that leaves everything in place but inert.
Leave the gc.collect() line in pool_errors alone — that's the suite-flake fix, not part of the experiment, and deleting it would bring the flake back.



- epg
- tv archive, like https://github.com/kiddac/EStalker for xtream users
- replace 1 portal by another portal



---= DONE =---

2026-09-21 (the connection a zap finds is already open)
- httpx closes an idle connection after **5 s** by default (`httpx.Limits()`), and a pooled portal session idles exactly like that between plays. Every `create_link` after a quiet moment therefore paid a fresh TCP (+TLS) handshake first - two round trips on a WAN panel, before the panel saw the request at all. That is pure added zap latency, and it is invisible in any portal-side timing.
  -> pooled portal clients now use `httpx.Limits(keepalive_expiry=SPM_PORTAL_KEEPALIVE_S, max_keepalive_connections=8, max_connections=SPM_PORTAL_MAX_CONNECTIONS)`, default 90 s (0 = never expire) - what a real set-top box does, and it costs the panel nothing (one idle socket instead of a socket per zap).
  -> measured in the sandbox: with the mock portal on loopback there is no handshake to save (plain HTTP, ~0 ms), so no local number is quoted here; the saving is the TCP+TLS setup on a real WAN panel, 2 RTT plus the TLS exchange.
  -> `tests/test_portal_tls_and_errors.py`: the session asks for the window (and `None` when switched off).

2026-09-21 (multi-MAC portals: take the free MAC, like the reference proxy does)
- "multiple MACs per portal, streams per MAC = 1 - how come zapping works in STB-Proxy?" -> because its `/play` loop walks the MAC list and plays the first one `isMacFree()` calls free: the zap lands on the *other* MAC, whose panel slot is free right now, while the MAC it left is still counted by the panel. SPM walked its chain in affinity order and took back the MAC that just played (a deliberate earlier decision, from a log where the *other* MAC produced no data).
  -> new `Settings -> Zap takes a free MAC first` (`prefer_free_mac`, default **on**, env `SPM_PREFER_FREE_MAC`): candidates are ranked untouched MACs -> this user's own post-302 lease -> somebody else's stream, stable within a rank, so affinity still decides between equals and the zap-back link cache keeps working. Off restores the older rule (both `tests/test_mac_availability.py` tests that pin it now run in that mode).
  -> the pump waits for the chain **once** (BUSY_WAIT_S) instead of per busy MAC: with two MACs where the first is held, waiting per MAC burned seconds while a free MAC sat in the same chain. Measured on the demo instance (2 MACs, user anna holding MAC1): box zaps to another channel -> 200 in **556 ms on MAC2**.
  -> when nothing is free the answer is 503 + Retry-After after that one wait (measured: 503 in 3.018 s with both MACs held by another user, log names both holders).
  -> the link cache is now keyed per (item, MAC, **user**): the URL only carries the panel's MAC session, so replaying user A's link for user B was a sharing decision the cache should not make (the zap-back case is always the same user, so nothing is lost).
  -> `portal_first` + a portal with several MACs now logs a warning: that strategy uses one MAC per portal per pass, so a zap asks the same MAC every time - the exact shape a panel's one-connection slot refuses. `macs_first` walks them all.
- suite: 1214 passed, 2 skipped, the same single pre-existing failure as main.

2026-09-21 (zap robustness: occupancy is a hint, never a veto)
- "switching to another channel will not play / switching back sometimes plays the first channel / it takes a long time before the channel plays (also with direct and copy)" -> root cause found and fixed, ported from the reference app whose zaps nobody complains about (STB-Proxy keeps *no* state across requests; SPM keeps state on purpose, so it now behaves as if it did not). Analysis with measurements and the comparison: docs/STREAM-FETCH-ANALYSIS.md.
  -> **no more instant 404 because of our own bookkeeping.** `open()` used to answer 404 in 13 ms when every MAC was busy - which is exactly what a fast Enigma2 zap hits (its own pipe/lease on the channel it just left). It now waits `SPM_BUSY_WAIT_S` (default 3 s) for a MAC and *takes over this user's own previous stream* (`preempt_own`: same rule as the redirect lease always had - never another user's). Measured on the demo instance: zap -> ch2 with ch1's pipe live = 200 in 566 ms (was 502 after 2.5 s); same channel again = 200 in 554 ms (was 404 in 13 ms).
  -> **the panel's own busy slot is retried, not burned.** `SLOT_BUSY_CODES` (`limit`, `account_is_in_use`, `max_connections`) are no longer treated as auth failures: `_get` reads the refusal *body* before reacting to a 403, so a busy MAC no longer throws the bearer away and re-handshakes (which could kick the session the box was still using). `_create_link_with_backoff` re-asks the SAME MAC after 0.5/1.0/2.0 s (`SPM_BUSY_BACKOFF`) - on a single-MAC portal it is the only candidate there is.
  -> **"everything was busy" is 503 + Retry-After: 2, not 404/502.** Players retry a 503; a 404 tells an Enigma2 box the channel does not exist. Applies to both paths (the redirect path reports it through `resolve(out=...)`, the proxy path through `handle.busy`, including the case where the engine only finds out after walking the chain).
  -> **a candidate that just failed is tried last** (`FAILURE_DEMOTE_S`, 120 s): route affinity says "this worked 20 minutes ago", a fresh failure outranks it. STB-Proxy's `moveMac`, per route and per process instead of a global config file.
  -> **zap-back is free** (`SPM_LINK_CACHE_S`, 90 s, live only): a link resolved seconds ago is replayed for the next 302 (probed first), so away-and-back costs no `create_link` at all. Log: "replaying the link resolved 0s ago ... (no create_link)".
  -> **R2b: the ffmpeg path now plays a permanent stored link too** (`SPM_FFMPEG_USE_STORED_LINK`, default on). `link_policy(ffmpeg=True)` used to mean "always ask" - a portal round trip *and* a panel slot allocation on every play of a channel the panel already called permanent, which is the collision the zap then loses. tmp/load-balanced channels, template cmds, URLs carrying a session token and cds with no URL still ask (tests pin all four). Measured: 302 with no panel call vs create_link + pipe.
  -> **new per-portal setting "Streams per MAC"** (`portals.streams_per_mac`, blank = 1, in the portal form): some panels allow two or three concurrent links on one MAC and serialising those to one costs a zap every time. `mac_locks` is a set per MAC now (the old single-id value is still accepted). STB-Proxy calls the same knob "streams per mac".
- suite: 1210 passed, 2 skipped, 1 pre-existing failure on this box (`test_stb_identity::test_an_existing_install_gets_the_columns_it_is_promised` fails on main too - sqlite DROP COLUMN on a FK'd column).


2026-09-17 (ffmpeg filters)
- selectable extra video filters for fault-finding (Enigma2 audio-plays-but-black-picture): new template field "Video filter" (None + 16 presets: 5x VAAPI deinterlace, 2x QSV deinterlace, 4x software yadif/bwdif incl. bob, GPU round-trip no-op test, 8-bit 4:2:0 force, 2x field-flag force, null sanity check). Spliced first into -vf with per-decode-path snippets, full build/parse round-trip, mismatch/CPU/bob warnings, tooltips + Demo-button workflow. All stored commands byte-identical (default None). Suite: 772 passed, same 2 pre-existing failures as main.

2026-09-17 (CI)
- the suite only runs when I run it by hand - can GitHub run it on every change?
  -> new `dev/ci.yml.example`, installed with `cp dev/ci.yml.example .github/workflows/ci.yml` (the repo's bot is not allowed to create workflow files: GitHub refuses the push). The `tests` workflow runs on every pull request, on pushes to main and on demand: installs requirements-dev.txt, runs dev/check-yaml.sh and dev/check-js.js, then pytest - the same 716 passed / 5 skipped as locally, `-n auto` and the 120 s timeout come from pytest.ini. dev/check-yaml.sh now verifies BOTH examples stay byte-identical to their installed workflow and prints the install hint while ci.yml is missing.
  -> the two tests that fail on main are deselected inside the workflow (a permanently red check teaches nobody anything); dev/check-links.py is left out until its 5 pre-existing failures (EPG now/next) are fixed.

2026-09-17 (suite speed)
- the full test suite takes a really long time: can it be sped up with a timeout, skipping tests, or something else?
  -> measured first: 102.5 s, of which 72.5 s was fixture setup - the autouse fixture dropped and re-created 31 tables for every one of 700+ tests (~58 ms each, ~40 s total). It now deletes rows instead (~6 ms) and rebuilds the schema only when a test actually changed it (fingerprinted via sqlite_master; the migration tests still trigger a rebuild). Same tests, same order, same coverage: 29.1 s serial.
  -> three long `sleep`s in tests were trimmed to the smallest value that still proves the same thing (demo timeout 2 s -> 0.5 s, simulated page latency 50 ms -> 20 ms, and the "silent ffmpeg" stub now `exec`s its sleep so no orphan child holds the pipes and burns the 3 s bounded reap). Slowest test: 3.71 s -> 1.37 s.
  -> pytest-xdist + pytest-timeout are now in requirements-dev and configured in pytest.ini: `-n auto` (measured ~19 s with `-n 3` on a 2-core box) and a 120 s per-test deadlock timeout with `timeout_method = signal`, verified to fail a hung async test in place instead of never returning. Skipping/deselecting tests was NOT needed: individual tests are not the bottleneck, and no test is slow enough to justify dropping it.
  -> bonus, found while chasing a suite hang under xdist: `run_uncancelled` could lose a cancellation that `asyncio.wait_for` swallowed when the shielded work finished in the same loop turn as the cancel (the task kept running in state CANCELLING with no exception). The log writer parked on `await q.get()` forever and the event-loop teardown that waits for it (`asyncio.Runner.close()` in tests, uvicorn's shutdown in production) waited forever too. `_cancel_shield` now hands that cancellation back; regression test: tests/test_stream_disconnect.py::test_run_uncancelled_does_not_lose_a_cancellation_that_races_the_result.

2026-09-17
- can we see/test if a MAC is still available and not already used by another user before connecting to that portal/MAC?
  -> Portals: per-MAC runtime badge (free here / streaming · user / leased Ns) + per-MAC "Test" button (asks the PANEL: available / in-use / unusable / no-data), API: POST /api/portals/{id}/macs/{mac}/probe and /macs/probe for a whole portal.
- how does zapping work: is the portal connection rebuilt per channel, or reused with a different stream? Is the old stream still holding the MAC?
  -> per play: new create_link (new play_token) on the pooled session; a 302 play leaves a 180s lease. Same user's zap now TAKES OVER its own lease (the channel it just left) instead of skipping that MAC; another user's MAC is still skipped. ffmpeg pipes are never taken over.
- the NPO1 failure (mac 6D busy -> skip, then 502 "produced no data within 25s"):
  -> two fixes: (1) the redirect lease of the same user no longer blocks the zap, so the working MAC is used; (2) the first-chunk guard follows the fallback engine's budget (SPM_STREAM_START_BUDGET, default 75s) instead of a fixed 25s, and the 502/log now list every MAC tried and why (plus ffmpeg's last stderr words on a silent stall).

2026-09-13
- check output stream or ffmpeg template to enigma2 box as the stream is not working on my enigma2 box
- on dashboard change positions of "Background jobs" and "Messages" with each other
- check time stamp in message pane (2 hours behind). check for all time stamps.
- move "Quick actions" under "Background jobs"
- remove the following message from msglog "[spm.api] GET /api/dashboard -> 200", "[spm.api] GET /api/streams -> 200", "[spm.api] GET /login -> 200". also remove them from "API status" in dashboard
- tab "input source" add option to bulk change the selected "custom group names", select from list and free text.
- Edit portal popup, make detailed gerne list at the bottom the same width as the popup window. so I prefer the scroll down and up instead of left and right
- make default "Portal identity we advertise" value "minimal"
- is there a possibility / any posibility to check if there is a client streaming if "Redirect (bypass ffmpeg)" is used, so we can still use fallback channels? only give advise no implementation yet
- give advise how to add possibility to force (Override) a specific Mac and or portal to be used as primary source

2026-09-05
- add favicon (for all tabs)
- on "input source" tab change the header name of field "Playlist" (between "channel" and "portal") to "Custom Channel Name". also make the width of column "channel" smaller and the width of column "Custom Channel Name" bigger
- if you click on the "Custom Channel Name" information it is directly selected so the user can overwrite the value, but when it is selected and the user clicks it again, the user should be able to change the existing value, now he can only type a new value.
- in case a live channel is enabled in "input source" tab, it directly needs to be added as as a new custom channel, where the custom channel name is the same as the original channel name. after the channel is added the channel name should appear in the "Custom Channel Name" column so the user can edit it.
- on "input source" tab also add the column "Custom Group" which is the same information of the "Group" in the Playlist tab. this field will have the same functionality as the "custom channel name" only for the group/genre information.
- it takes a really long time before local files are playing (no ffmpeg transcoding assigned))
- playing local files (mp4 or avi) on enigma2 with ffmpeg: Enigma2 VOD - remux + subtitles (MKV) gives only sound, no picture. stream shows still .ts
2026-09-04 16:53:54	DEBUG	ffmpeg	spawn command: /usr/bin/ffmpeg -fflags +genpts+discardcorrupt -err_detect ignore_err -re -i /media/1-Giebel/2b.kraantje lek 1976.mp4 -map 0:v:0 -map 0:a:0? -dn -sn -c:v copy -c:a copy -bsf:v h264_mp4toannexb max_interleave_delta 2000000 -metadata title=2b.kraantje lek 1976.mp4 -f mpegts -mpegts_flags +resend_headers -flush_packets 1 pipe:1
- give me option to improve the access to the streams like Playlist, live channels, vod, series, local files. now it take quite some time to load the Playlist, start the stream for enigma2 and vlc
- explain how area is working together with the Playlist. I was expecting the area included in the URL of the Playlist as well (I don't see anything in the "Output for user" popup). how does it work for the xtream user? and which area is selected for the enigma2 (or can you assign the area in the enigma2 tab)
- advise on a better visualization to compare genres of Mac addresses to quickly see the differences between the genres of the different macs of the same portal. perhaps first open popup where user can select which macs to compare before actual fetch/compare, perhaps separate tabs for live, vod, series. perhaps list with the macs in the header and 1 row per genre with the indication which Mac has that genre. perhaps add filter possibility to only filter some genres. but give me some options
- after the "compare genres across macs" is done put the information on the number of genres in the persistent database and show it on the portal list (per Mac)
- xtream users: I get the xtream Playlist correct in the "smarters player" android application, but I cannot play the streams. it does not connect or I don't get data
- explain where the xtream user can find the local files in the android application. is it mapped to vod or series

2026-09-03
- it looks like vlc playlist cannot handle a minus "-" in the title, make sure "-" is handled correctly in the output Playlist for vlc.
- Input source - local: on "Scanned video files (used by the Local playlist builder)" add possibility to select multiple files and to enable/disable them in 1 go
- advise how to implement that I can assign different ffmpeg templates for different users to be used for the same custom channel (live, vod, series and local)
