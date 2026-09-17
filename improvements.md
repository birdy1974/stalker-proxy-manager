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
