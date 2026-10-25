# Selected FFmpeg templates — Quick Zap VAAPI

`quick-zap-vaapi-cqp.json` is one directly importable SPM version-1
**FFmpeg section** file. (The filename is retained so existing download
links keep working.) It contains the selected **13-template** set:

- nine logical client/scenario templates for Enigma2, the SPM web player and
  VLC;
- four retained SPM standard templates: Copy, Dreambox DM800se, Redirect and
  Pass-through proxy.

The generic web-player/VLC VAAPI transcodes use the Intel low-power H.264
encoder with `-rc_mode CQP`. **Enigma2 and Dreambox transcodes deliberately
use the standard VAAPI encoder instead: low-power is off and
`-rc_mode VBR` is selected.** The host therefore needs a standard
`h264_vaapi` encoder on `/dev/dri/renderD128` that accepts VBR.

## Templates in the file

| # | Template | What it does |
|---:|---|---|
| 1 | `Enigma2 - Live` | Fastest Enigma2 live transcode: standard VAAPI encoder (low-power off), 1080p H.264 High@4.1 VBR 4000k/4400k/8000k, AC3, MPEG-TS, subtitles dropped |
| 2 | `Enigma2 - Live (with subtitles)` | Enigma2 live standard VAAPI encoder (low-power off), 1080p H.264 High@4.1 VBR 4000k/4400k/8000k, AC3, MPEG-TS, DVB subtitles |
| 3 | `Enigma2 - VOD` | Enigma2 VOD/series default: video, audio and subtitles copied into Matroska; no transcode, so the source H.264 level is preserved. Its stored encoder fields are also VBR with low-power off, inactive while copying |
| 4 | `Enigma2 - VOD (transcode)` | Enigma2 VOD/series rescue: standard VAAPI encoder (low-power off), 1080p H.264 High@4.1 VBR 4000k/4400k/8000k, AC3, Matroska, subtitles copied |
| 5 | `Web player - LAN` | Web-player local-network template: H.264 video copied, audio converted to AAC, MPEG-TS |
| 6 | `Web player - Internet (slow)` | VAAPI low-power 720p H.264 CQP 32, AAC 96k, MPEG-TS |
| 7 | `Web player - Internet (fast)` | VAAPI low-power 1080p H.264 CQP 25, AAC 192k, MPEG-TS |
| 8 | `VLC - Internet (slow)` | VAAPI low-power 720p H.264 CQP 30, AAC 128k, MPEG-TS |
| 9 | `VLC - Internet (fast)` | Lean MPEG-TS remux: source video and audio copied |
| 10 | `Copy / passthrough (no transcode)` | Retained SPM standard: FFmpeg remux with source video/audio copied |
| 11 | `Dreambox DM800se (Enigma2 / MPEG2-SD)` | Retained SPM standard: standard VAAPI encoder (low-power off), 576p H.264 Main@3.1 VBR 1200k/1300k/2400k, MP2, MPEG-TS, DVB subtitles |
| 12 | `Redirect (bypass ffmpeg)` | Retained SPM standard: sends the player directly to the original stream with HTTP 302 |
| 13 | `Pass-through proxy (bypass ffmpeg)` | Retained SPM standard: proxies the original bytes through SPM without starting FFmpeg |

All Enigma2 **transcodes** in this file use H.264 level **4.1**, except the
legacy Dreambox DM800se compatibility template, which intentionally remains
Main@3.1 at 576p for that older receiver. Copy, Redirect and Pass-through do
not encode video, so their stored level field does not alter the source
stream's level.

Every Enigma2 row, including the copy-based `Enigma2 - VOD` row and the
Dreambox row, stores **VAAPI rate control = `VBR`** and **VAAPI low-power
(LP encoder) = off**. On the transcode rows FFmpeg now receives
`-rc_mode VBR` with `-b:v`, `-maxrate` and `-bufsize`, and no `-low_power`
or `-global_quality`. These rows also carry `advice_ignored=low-power-off`;
after SPM itself is updated, its advisor additionally hides that
“turn low-power on” optimisation tip for Enigma2/Duo2 template names. A
genuine host-compatibility warning is not hidden.

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

The same additive rule also means that re-importing a newer copy of this file
does not update a same-named template already in the database. If an earlier
copy was imported, edit each logical Enigma2 row and set **Level** to
`4.1`, **VAAPI rate control** to `VBR`, and **VAAPI low-power (LP encoder)**
to off — or delete those custom rows before importing this file again.
The relevant custom rows are `Enigma2 - Live`, `Enigma2 - Live (with
subtitles)`, `Enigma2 - VOD` and `Enigma2 - VOD (transcode)`. Field-sourced
shipped Enigma2 built-ins, including Dreambox and `Vu+ Duo2 live`, are
refreshed to VBR with low-power off by SPM on boot after updating SPM
itself.

`Enigma2 - Live (with subtitles)` contains the shipped `Vu+ Duo2 live`
recipe under the logical name.

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
- generic web-player/VLC VAAPI transcodes with low-power CQP and
  `async_depth=4`;
- Enigma2/Dreambox VAAPI transcodes with the standard encoder, VBR and
  `async_depth=4`;
- subtitles dropped on the fast-start MPEG-TS templates, except where an
  Enigma2 template deliberately preserves them;
- copy/remux instead of transcoding where the client already accepts the
  source video.

CQP does **not** impose a hard bitrate ceiling. The generic slow-internet
QP values are starting budgets: raise QP by 2 if a high-motion channel
stalls, or lower it by 2 if the picture is too soft and the link has
headroom. Their stored bitrate fields are dormant while CQP is selected.
The Enigma2 VBR fields are active: 1080p uses 4000k target / 4400k maxrate
/ 8000k bufsize, and the Dreambox uses 1200k / 1300k / 2400k.

`Web player - LAN`, `VLC - Internet (fast)` and
`Copy / passthrough (no transcode)` are H.264-oriented copy templates because
they render `h264_mp4toannexb`. Use `Pass-through proxy (bypass ffmpeg)` for
HEVC in VLC, and use `Web player - Internet (fast)` for HEVC in the browser
player.
