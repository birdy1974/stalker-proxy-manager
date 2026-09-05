2026-09-05
- add favicon (for all tabs)
- on "input source" tab change the header name of field "Playlist" (between "channel" and "portal") to "Custom Channel Name". also make the width of column "channel" smaller and the width of column "Custom Channel Name" bigger
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


- epg
- tv archive, like https://github.com/kiddac/EStalker for xtream users
- replace 1 portal by another portal



---= DONE =---

2026-09-03
- it looks like vlc playlist cannot handle a minus "-" in the title, make sure "-" is handled correctly in the output Playlist for vlc.
- Input source - local: on "Scanned video files (used by the Local playlist builder)" add possibility to select multiple files and to enable/disable them in 1 go
- advise how to implement that I can assign different ffmpeg templates for different users to be used for the same custom channel (live, vod, series and local)
