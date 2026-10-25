# Selected FFmpeg templates — Quick Zap VAAPI CQP

`quick-zap-vaapi-cqp.json` is one directly importable SPM version-1
**FFmpeg section** file. It contains the selected **13-template** set:

- nine logical client/scenario templates for Enigma2, the SPM web player and
  VLC;
- four retained SPM standard templates: Copy, Dreambox DM800se, Redirect and
  Pass-through proxy.

The VAAPI transcodes are for Intel hosts using `/dev/dri/renderD128` whose
low-power H.264 encoder accepts `-rc_mode CQP`.

## Templates in the file

| # | Template | What it does |
|---:|---|---|
| 1 | `Enigma2 - Live` | Fastest Enigma2 live transcode: VAAPI low-power 1080p H.264 CQP 26, AC3, MPEG-TS, subtitles dropped |
| 2 | `Enigma2 - Live (with subtitles)` | Enigma2 live VAAPI low-power 1080p H.264 CQP 26, AC3, MPEG-TS, DVB subtitles |
| 3 | `Enigma2 - VOD` | Enigma2 VOD/series default: video, audio and subtitles copied into Matroska; no transcode |
| 4 | `Enigma2 - VOD (transcode)` | Enigma2 VOD/series rescue: VAAPI low-power 1080p H.264 CQP 26, AC3, Matroska, subtitles copied |
| 5 | `Web player - LAN` | Web-player local-network template: H.264 video copied, audio converted to AAC, MPEG-TS |
| 6 | `Web player - Internet (slow)` | VAAPI low-power 720p H.264 CQP 32, AAC 96k, MPEG-TS |
| 7 | `Web player - Internet (fast)` | VAAPI low-power 1080p H.264 CQP 25, AAC 192k, MPEG-TS |
| 8 | `VLC - Internet (slow)` | VAAPI low-power 720p H.264 CQP 30, AAC 128k, MPEG-TS |
| 9 | `VLC - Internet (fast)` | Lean MPEG-TS remux: source video and audio copied |
| 10 | `Copy / passthrough (no transcode)` | Retained SPM standard: FFmpeg remux with source video/audio copied |
| 11 | `Dreambox DM800se (Enigma2 / MPEG2-SD)` | Retained SPM standard: compatibility transcode for the older Dreambox DM800se |
| 12 | `Redirect (bypass ffmpeg)` | Retained SPM standard: sends the player directly to the original stream with HTTP 302 |
| 13 | `Pass-through proxy (bypass ffmpeg)` | Retained SPM standard: proxies the original bytes through SPM without starting FFmpeg |

## Removed from this file

These seven templates from the earlier combined 20-template list are **not**
in the JSON:

- `VAAPI 720p ~1M (DS918+ reference)`
- `VAAPI 1080p ~2.5M`
- `QSV 720p ~1M`
- `Software 720p ~1.2M (libx264)`
- `Enigma2 VOD - remux + subtitles (MKV)`
- `Enigma2 VOD - VAAPI 1080p H.264 + AC3 + subtitles (MKV)`
- `Vu+ Duo2 live (Enigma2 / H.264 1080p MPEG-TS)`

The last three shipped Enigma2 recipes are not lost: the same fields and
commands are supplied under `Enigma2 - VOD`, `Enigma2 - VOD (transcode)` and
`Enigma2 - Live (with subtitles)`.

## Import

1. Download `quick-zap-vaapi-cqp.json`.
2. In SPM, open **FFmpeg templates**.
3. Choose **Import** and select the JSON file.
4. Assign the imported templates to playlist items or area kind defaults.

The file contains all 13 selected templates. SPM's importer is additive and
matches by name, so the visible import result depends on what is already in
the database:

- **Empty FFmpeg-template table:** imports all 13 templates.
- **Normal seeded SPM installation:** imports the nine logical templates and
  skips Copy, Dreambox, Redirect and Pass-through because those four shipped
  names are already present.
- **Same file imported again:** imports 0 and skips all 13 names.

An additive import cannot delete templates already installed in SPM. The
seven removed names are absent from this file, but current SPM still seeds
them as built-in templates on boot; importing this file does not remove or
disable those existing built-in rows. All rows in the file have
`is_default=false`, so importing does not change the selected default or any
existing assignments.

The shipped `Vu+ Duo2 live` preset itself is corrected to CQP in SPM's seeded
defaults; after updating SPM, its field-sourced row is refreshed on boot.
`Enigma2 - Live (with subtitles)` contains that corrected recipe under the
logical name.

For Enigma2, keep the bouquet profile on delivery **Use item template** and
container **Auto**: live uses player 4097/`.ts`; VOD and series use player
5002/`.mkv`. For VLC, use separate SPM users/areas for the slow and fast
playlists so their area kind defaults can differ.

## Zap-speed notes

`Pass-through proxy (bypass ffmpeg)` and `Redirect (bypass ffmpeg)` remain
faster than every FFmpeg template when the client can play the original
codec/container and the original bitrate fits the link. The selected set
optimizes the cases where FFmpeg is required:

- bounded input analysis (`-analyzeduration 1000000 -probesize 1000000`);
- VAAPI low-power H.264 with explicit CQP and `async_depth=4`;
- subtitles dropped on the fast-start MPEG-TS templates, except where an
  Enigma2 template deliberately preserves them;
- copy/remux instead of transcoding where the client already accepts the
  source video.

CQP does **not** impose a hard bitrate ceiling. The slow-internet QP values
are starting budgets: raise QP by 2 if a high-motion channel stalls, or lower
it by 2 if the picture is too soft and the link has headroom. The stored
bitrate fields are dormant while CQP is selected.

`Web player - LAN`, `VLC - Internet (fast)` and
`Copy / passthrough (no transcode)` are H.264-oriented copy templates because
they render `h264_mp4toannexb`. Use `Pass-through proxy (bypass ffmpeg)` for
HEVC in VLC, and use `Web player - Internet (fast)` for HEVC in the browser
player.
