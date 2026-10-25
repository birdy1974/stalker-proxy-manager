# Quick Zap VAAPI CQP template pack

`quick-zap-vaapi-cqp.json` is an SPM version-1 **FFmpeg section** export for
Intel VAAPI hosts using `/dev/dri/renderD128` whose low-power H.264 encoder
accepts `-rc_mode CQP`. It is designed to merge with the shipped templates:
scenario templates with new names are added, while templates that are already
present are skipped and left unchanged by SPM's additive importer.

## Import

1. Download `quick-zap-vaapi-cqp.json`.
2. In SPM, open **FFmpeg templates**.
3. Choose **Import** and select the JSON file.
4. Assign the imported templates to playlist items or area kind defaults.

Importing does not select a new global default template and does not change
existing assignments.

On a normal current install, the two Enigma2 VOD templates in the pack are
already shipped defaults and will be reported as skipped. The corrected Duo2
live template and the Web/VLC scenario templates have distinct names and will
be added. The shipped `Vu+ Duo2 live` preset itself is corrected to CQP in
SPM's seeded defaults; after updating SPM, its field-sourced row is refreshed
on boot.

## Recommended assignments

| Situation | Template |
|---|---|
| Enigma2 Vu+ Duo2 live, with DVB subtitles | `Vu+ Duo2 live - LP CQP (custom)` |
| Enigma2 Vu+ Duo2 live, fastest FFmpeg transcode | `Vu+ Duo2 live - Quick Zap no subs (VAAPI LP CQP26)` |
| Enigma2 VOD/series default | `Enigma2 VOD - remux + subtitles (MKV)` |
| Enigma2 VOD/series HEVC/4K exception | `Enigma2 VOD - VAAPI 1080p H.264 + AC3 + subtitles (MKV)` |
| SPM web player, local H.264 source | `Web LAN - Copy video + AAC (fast start)` |
| SPM web player, slow internet | `Web Internet Slow - VAAPI LP 720p CQP32` |
| VLC, slow internet | `VLC Internet Slow - VAAPI LP 720p CQP30` |
| SPM web player, fast internet / HEVC fallback | `Web Internet Fast - VAAPI LP 1080p CQP25` |
| VLC, fast internet live | `VLC Internet Fast - Copy MPEG-TS (fast start)` |

For Enigma2, keep the bouquet profile on delivery **Use item template** and
container **Auto**: live uses player 4097/`.ts`; VOD and series use player
5002/`.mkv`. For VLC, use separate SPM users/areas for the slow and fast
playlists so their area kind defaults can differ.

## Zap-speed notes

`@passthrough` and `@redirect` remain faster than every FFmpeg template when
the client can play the original codec/container and the original bitrate
fits the link. The pack optimizes the cases where FFmpeg is required:

- bounded input analysis (`-analyzeduration 1000000 -probesize 1000000`);
- VAAPI low-power H.264 with explicit CQP and `async_depth=4`;
- subtitles dropped on the fast-start MPEG-TS templates, except where the
  Enigma2 templates deliberately preserve them;
- copy/remux instead of transcoding where the client already accepts the
  source video.

CQP does **not** impose a hard bitrate ceiling. The slow-internet QP values
are starting budgets: raise QP by 2 if a high-motion channel stalls, or lower
it by 2 if the picture is too soft and the link has headroom. The stored
bitrate fields are dormant while CQP is selected.

The two lean copy-to-MPEG-TS templates are H.264-oriented because they render
`h264_mp4toannexb`. Use `@passthrough` for HEVC in VLC, and use the Web
Internet Fast VAAPI template for HEVC in the browser player.
