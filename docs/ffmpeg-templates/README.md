# Quick Zap VAAPI CQP template pack

`quick-zap-vaapi-cqp.json` is an SPM version-1 **FFmpeg section** export for
Intel VAAPI hosts using `/dev/dri/renderD128` whose low-power H.264 encoder
accepts `-rc_mode CQP`.

The imported templates use short, logical names grouped first by client and
then by viewing situation. SPM's shipped templates keep their historical
names because SPM matches and refreshes built-ins by name; changing those
names in code would orphan existing installations and assignments. Where a
pack recipe is identical to a shipped Enigma2 recipe, the pack therefore
provides the same fields and command under the logical name.

## Templates in this pack

| Logical name | What it does |
|---|---|
| `Enigma2 - Live` | Fastest Enigma2 live transcode: VAAPI low-power 1080p H.264 CQP 26, AC3, MPEG-TS, subtitles dropped |
| `Enigma2 - Live (with subtitles)` | Enigma2 live VAAPI low-power 1080p H.264 CQP 26, AC3, MPEG-TS, DVB subtitles |
| `Enigma2 - VOD` | Enigma2 VOD/series default: video, audio and subtitles copied into Matroska; no transcode |
| `Enigma2 - VOD (transcode)` | Enigma2 VOD/series rescue: VAAPI low-power 1080p H.264 CQP 26, AC3, Matroska, subtitles copied |
| `Web player - LAN` | Web-player local-network template: H.264 video copied, audio converted to AAC, MPEG-TS |
| `Web player - Internet (slow)` | VAAPI low-power 720p H.264 CQP 32, AAC 96k, MPEG-TS |
| `Web player - Internet (fast)` | VAAPI low-power 1080p H.264 CQP 25, AAC 192k, MPEG-TS |
| `VLC - Internet (slow)` | VAAPI low-power 720p H.264 CQP 30, AAC 128k, MPEG-TS |
| `VLC - Internet (fast)` | Lean MPEG-TS remux: source video and audio copied |

## Import

1. Download `quick-zap-vaapi-cqp.json`.
2. In SPM, open **FFmpeg templates**.
3. Choose **Import** and select the JSON file.
4. Assign the imported templates to playlist items or area kind defaults.

SPM's merge is additive and imports by template name. On a normal install,
all nine logical templates are added beside the shipped templates. Importing
the same file again reports those nine names as skipped. Importing does not
select a new global default template and does not change existing
assignments.

If an earlier copy of this pack was imported under the old descriptive names,
the logical names are new templates rather than renames of the existing rows.
Switch assignments to the logical templates first, then delete the old custom
rows if they are no longer wanted.

The shipped `Vu+ Duo2 live` preset itself is corrected to CQP in SPM's seeded
defaults; after updating SPM, its field-sourced row is refreshed on boot.
`Enigma2 - Live (with subtitles)` contains that corrected recipe under the
logical name.

For Enigma2, keep the bouquet profile on delivery **Use item template** and
container **Auto**: live uses player 4097/`.ts`; VOD and series use player
5002/`.mkv`. For VLC, use separate SPM users/areas for the slow and fast
playlists so their area kind defaults can differ.

## Complete template list after import

Importing this pack into a normally seeded SPM installation gives **20
templates**: the nine logical templates above plus SPM's eleven shipped
standard templates below.

### SPM shipped standard templates

| Standard SPM name | Main purpose |
|---|---|
| `VAAPI 720p ~1M (DS918+ reference)` | General VAAPI 720p reference transcode |
| `VAAPI 1080p ~2.5M` | General VAAPI 1080p transcode |
| `QSV 720p ~1M` | Intel Quick Sync alternative to VAAPI |
| `Software 720p ~1.2M (libx264)` | CPU-only fallback transcode |
| `Copy / passthrough (no transcode)` | FFmpeg remux with source video/audio copied |
| `Enigma2 VOD - remux + subtitles (MKV)` | Historical built-in name for the recipe also supplied as `Enigma2 - VOD` |
| `Enigma2 VOD - VAAPI 1080p H.264 + AC3 + subtitles (MKV)` | Historical built-in name for the recipe also supplied as `Enigma2 - VOD (transcode)` |
| `Vu+ Duo2 live (Enigma2 / H.264 1080p MPEG-TS)` | Historical built-in name for the recipe also supplied as `Enigma2 - Live (with subtitles)` |
| `Dreambox DM800se (Enigma2 / MPEG2-SD)` | Compatibility transcode for the older Dreambox DM800se |
| `Redirect (bypass ffmpeg)` | Sends the player directly to the original stream with HTTP 302; SPM's initial default |
| `Pass-through proxy (bypass ffmpeg)` | Proxies the original bytes through SPM without starting FFmpeg |

The three overlapping Enigma2 recipes remain available under both their
historical shipped names and the logical pack names. Use the logical names
for new assignments; the shipped rows remain SPM's built-in fallbacks.

## Zap-speed notes

`@passthrough` and `@redirect` remain faster than every FFmpeg template when
the client can play the original codec/container and the original bitrate
fits the link. The pack optimizes the cases where FFmpeg is required:

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

`Web player - LAN` and `VLC - Internet (fast)` are H.264-oriented copy
templates because they render `h264_mp4toannexb`. Use
`Pass-through proxy (bypass ffmpeg)` for HEVC in VLC, and use
`Web player - Internet (fast)` for HEVC in the browser player.
