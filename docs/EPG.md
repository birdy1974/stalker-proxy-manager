# EPG operation and source advice

## What is implemented

- On the first startup after this upgrade, these three external sources are
  added/enabled. Later disabling or deleting one is respected across restarts:
  - `http://www.xmltvepg.nl/rytecNL_Basic.xz`
  - `http://rytecepg.wanwizard.eu/rytecNL_Basic.xz`
  - `http://epg.vuplus-community.net/rytecNL_Basic.xz`
- Settings → **Use source portal EPG** enables Stalker guide ingestion. **Check
  portal EPG** discovers portal-backed sources and checks their authenticated
  guide APIs. The normal refresh also includes them. A portal without a resolved
  endpoint, usable free MAC or enabled playlist inputs reports that condition.
- One bulk `get_epg_info` call is attempted first. If unavailable/incomplete,
  short-guide requests are paced and restricted to enabled playlist inputs,
  with at most 100 short-guide requests and a 90-second budget per portal.
  This is not a full-catalogue crawl and never opens a video stream. A busy MAC
  is skipped, not taken away from another viewer. Portal timezone settings are
  used when interpreting timestamps. Short guides may cover only now/next or
  several hours; a supported portal need not provide a seven-day guide.
- Portal guide IDs use a persisted source namespace plus the portal's channel
  ID, avoiding collisions between providers and surviving normal backup restore
  ID remapping. Portal/source relationships and programme provenance are included
  in complete/individual-table backups; caches can be regenerated.
- Settings → **EPG refresh interval** accepts suggested or custom whole-hour
  intervals (1–168). **0 pauses** automatic refresh; manual refresh still works.
  The default is 24 hours. Settings are re-read each minute without a restart.
  A failed feed cannot abort the others or cause a retry every minute.
- XMLTV, gzip and xz are supported. Downloads and expanded XML have size limits
  (50 MiB / 256 MiB). Prefer country-specific feeds over huge global bundles.
  Last successful fetch and error/progress states remain visible in Settings.

## Matching

**Edit channel → Match EPG** fuzzy-matches the custom channel name. A sole
confident match fills the editor field; multiple possibilities open a chooser.
The channel's **Save** button commits the choice.

**Settings → Match channels** automatically assigns only empty channels with a
single confident candidate. Ambiguous/low-confidence candidates and unknown
existing IDs open a review popup, with **no preselected replacement**. To review
already-valid assignments too, tick **Review existing EPG assignments too**.
Leave a row unchanged to defer it. Concurrently changed assignments are rejected
instead of overwritten. Numbers and `+` variants are treated as significant;
identical IDs supplied by mirrors are deduplicated into one choice.

Only enabled sources are used as matching candidates. After assigning an ID,
cached XMLTV is reprocessed so that programmes for the new ID become available
without downloading again. After restoring a backup without caches, use Refresh
all. Sources with no reliable match still permit manually entering a tvg-id.

## Channel source priority, gap filling and time correction

Open **Playlist → Edit channel → Guide priority & timing**.

- **Add guide source** selects a feed and its exact upstream channel ID. The ID
  field searches that feed's cached channel catalogue; an exact ID can also be
  entered manually. Different feeds may use different IDs for the same channel.
- **Move up / Move down** sets priority, independent of playback-source priority
  and feed refresh order. Disabled sources and disabled/missing portals are skipped.
- **Fill gaps from fallback guides** keeps higher-priority intervals intact and
  uses lower-priority entries only in uncovered intervals. Fallback entries can
  be clipped or split, retaining their original titles/descriptions. This avoids
  overlapping output, but cannot determine whether a provider's programme is
  factually correct. With gap filling off, only the first enabled selected source
  is used—even when it currently has no programmes.
- An empty mapping list means automatic selection: feeds carrying the channel's
  ordinary EPG ID, in ascending source-ID order. Legacy unattributed programmes
  are a final fallback. Removing a configured source does not silently opt the
  channel into arbitrary feeds: choose replacements or explicitly apply an empty
  list to return to automatic mode.
- **Channel correction** and each mapping's **extra correction** accept whole
  minutes from **−1440 to +1440**, added together with the source correction. Positive means later, negative
  means earlier. For example, channel `+60` and mapping `−15` produces `+45`.
  Corrections affect output and health checks only; stored UTC timestamps are
  unchanged, so subsequent refreshes never compound the shift. These are fixed
  corrections, not automatic daylight-saving/timezone rules.
- **Apply to channel editor** stages the policy; **Save** on the channel commits
  it together with the other channel changes. Cancelling the channel discards it.
  Cached guides are reprocessed after saving to ingest newly selected IDs. If
  there is no cache, manually refresh the source to acquire its programmes.

Each feed now retains its own programme snapshot. Successful refreshes replace
only that source's snapshot, including an empty snapshot; failed imports retain
its previous programmes. Duplicate events in two feeds no longer overwrite one
another. Selected programme ingestion is bounded to 250,000 entries per feed.

## Per-source refresh and stale thresholds

In **Settings → EPG sources and matching**, each source has a **Schedule & timing** button:

| Choice | Behaviour |
|---|---|
| Inherit global interval | Uses the general EPG refresh interval (default 24 hours). |
| Manual refresh only | No scheduled download for this source; existing data remains usable. |
| Custom interval | Whole-hour interval, **1–168 hours**. |
| Stale after | **1–720 hours**, or blank for automatic: the greater of 48 hours and twice the effective interval. |

The global interval **0 is a master pause**, including source-specific overrides.
Manual refresh remains available. The scheduler checks each minute; failed
attempts are rate-limited separately from successful fetches. Settings shows the
next eligible refresh time; a busy importer can delay it. Reprocessing cached
XML is not a successful download and does not reset guide freshness or clear a
previous download error. Schedules are elapsed intervals, not daily wall-clock
appointments. With automatic refresh paused, the automatic stale threshold is
48 hours unless explicitly overridden.

## Source-wide timezone and time correction

Open **Settings → EPG sources and matching → Schedule & timing → Source timing**.
Defaults are **Automatic**, **0 minutes**; existing feeds keep their previous
interpretation after upgrading. All stored programme times are UTC. The server
and browser do not have to be in the same timezone as a source.

| Timezone mode | Interpretation |
|---|---|
| Automatic | Trust explicit offsets. XMLTV without an offset means UTC. Naive portal times use the portal's advertised timezone. |
| Use timezone when offset is missing | Interpret only offset-less clock times using the selected named zone. Preserve explicit offsets. |
| Override supplied timezone | Ignore a supplied offset and interpret the same wall-clock numbers using the selected named zone. Use only to repair incorrectly labelled guide timestamps. |

**Source timezone** accepts searchable IANA names, such as `Europe/Amsterdam`,
`Europe/London`, `America/New_York` or `UTC`. The field is disabled in Automatic
mode. Named zones follow winter/summer time. A nonexistent spring-forward clock
time is excluded, not silently invented. A repeated autumn clock time uses the
first occurrence; the preview warns about the ambiguity. Correct explicit
upstream offsets disambiguate it in Automatic and missing-offset modes.

Portal Unix timestamps are absolute instants and are **never reinterpreted by a
timezone mode**. Use a fixed correction if those instants are consistently wrong.
New portal caches preserve the original timestamp inputs and advertised timezone
alongside normalized XMLTV times, including inferred programme end times. Older
portal caches lack these inputs: switching them to a non-Automatic mode requires
one fresh portal guide download rather than guessing their original clocks.
These settings affect the imported/output guide, not the portal's STB identity
or its separate on-demand now/next tooltip calls.

**Source correction (minutes)** is a fixed adjustment from **−1440 to +1440**.
Positive moves programmes later; negative moves them earlier. It applies to
all channels using this feed, including its fallback fragments. The calculation is:

```text
final time = interpreted UTC + source correction + channel correction + mapping correction
```

Fixed corrections never rewrite stored programme timestamps. Timezone changes
reprocess the cached original guide asynchronously without downloading it. Until
successful reprocessing, the previous snapshot remains available and the source
shows **Timezone change pending** in Settings/health alerts. A disabled source
is reprocessed when enabled. If no cache exists, manually refresh the source.
Source corrections apply immediately; cached reprocessing also recovers older
entries that a larger correction brings into the output window. A cache refresh
does not update the last successful download time or erase download errors.
The combined correction can be up to 72 hours; ingestion keeps 78 hours of
history and seven days ahead, while output remains the current/upcoming 48 hours.

**Timing preview** is offline and does not save anything. It loads an original
cached example, or accepts a manually entered timestamp, and shows Automatic UTC,
selected interpretation UTC, corrected UTC and browser-local time. It includes
only the source correction, not any channel/mapping adjustments. XMLTV examples
use `YYYYMMDDhhmmss +HHMM` (offset optional); portal examples accept ISO clock
times or Unix seconds. **Reset timing to automatic / zero** resets only timing,
not refresh/stale settings. Press **Save source settings** to commit; Cancel
leaves the stored settings unchanged. Explanations are available in help tooltips.

## Missing/stale-guide alerts

**Dashboard → EPG guide health** shows a summary. **Settings → Guide alerts →
Guide alert details** lists affected enabled channels and enabled sources.
Checks refresh every 30 seconds and use the same source selection, time shifts
and gap filling as XMLTV:

- **Missing guide:** no usable programme overlapping the next 48 hours.
- **No programme now:** upcoming data exists, but no programme covers now.
- **Stale guide (channel):** all feeds contributing its programmes are stale or
  have never completed a successful fetch. A partially stale fallback can still
  appear as a source warning even when the channel has a fresh primary.
- **Source warnings:** never fetched, stale since the last successful fetch,
  a failed refresh, or a timezone change awaiting reprocessing. A failure alone does not mean retained programmes are
  unusable. Disabled sources/portals are excluded from these warnings.

These are **in-app alerts**, not email, push notifications or external monitoring.
Freshly fetched but empty/incomplete guides are detected by channel coverage,
not just by the source's successful-fetch timestamp.

## Upgrade, backup and API

Startup upgrades the programme uniqueness key to include its source, preserving
existing programme IDs/data. SQLite rebuilds only the programme table; legacy
rows referencing removed sources become unattributed rather than blocking the
upgrade. PostgreSQL replaces the unique constraint. A one-time offline cache
reindex recovers independent mirror snapshots where caches exist; previously
superseded programmes cannot be reconstructed without a cache or a fresh fetch.

Complete and individual-table backups include source scheduling/timezone/correction fields,
last-applied interpretation markers, channel
policy fields, mappings (`epg_channel_sources`) and per-source programmes. A
mapping-only add-only restore derives custom output IDs without overwriting an
existing channel's stored settings. Restore the Live playlist table too when
restoring the channel-wide offset/gap-fill preferences onto a new installation.

Admin API:

- `GET /api/epg`: source intervals, effective thresholds, last attempt/error and
  next eligible refresh.
- `PATCH /api/epg/sources/{id}`: `refresh_hours` (`null` inherit, `0` manual,
  `1..168` override), `stale_hours` (`null` automatic, `1..720` override), `enabled`,
  `timezone_mode` (`auto`, `missing`, `override`), `timezone_name` (IANA name or
  `null` in Automatic mode), and `offset_minutes` (integer `-1440..1440`).
- `GET /api/epg/timezones`: available named timezones.
- `POST /api/epg/sources/{id}/timing-preview`: read-only source timing preview;
  accepts the three timing fields plus optional `sample_start` / `sample_stop`.
  Without a start, uses a cached example; it never downloads a guide.
- `GET /api/epg/health`: channel/source warnings and summary counts.
- `GET` / `PUT /api/epg/policy/{live_id}`: channel policy. Example PUT body:

  ```json
  {
    "gap_fill": true,
    "offset_minutes": 60,
    "mappings": [
      {"source_id": 1, "tvg_id": "primary.nl", "offset_minutes": 0},
      {"source_id": 2, "tvg_id": "alternate.id", "offset_minutes": -15}
    ]
  }
  ```

- Live playlist create/update also accepts the same object as `epg_policy` for
  an atomic channel-and-policy save. At most 32 ordered mappings are allowed;
  duplicate source/ID pairs and unknown sources are rejected.

## Output for external applications

Open **Users → output URLs → Copy EPG URL**. Configure **Public base URL override**
with the LAN/reverse-proxy URL external players can reach, when necessary.

```text
http://<server>:8880/xmltv.php?username=<user>&password=<password>
http://<server>:8880/epg.xml?u=<user>&p=<password>
```

Use the generated URL so credentials containing `&`, `+` or other reserved
characters are encoded correctly. This URL is a credential: share it only with
trusted applications and use HTTPS outside a trusted LAN.

The guide contains **enabled Live playlist channels allowed by that user's Live
group selection**, not the entire upstream catalogue. M3U-only and Xtream users
can both retrieve it. Empty Live group selections expose no channels.

M3U `tvg-id`, Xtream `epg_channel_id`, XMLTV `<channel id>` and the XMLTV programme
references agree. Unassigned channels have an explicit stable `spm.live.<id>`
output ID (but no invented programme data). Duplicate playlist aliases sharing
an EPG ID with automatic policies produce one XMLTV channel and one set of
programmes. A custom source/timing policy gets its own `spm.live.<id>` output ID,
so two aliases can use different feeds or corrections. Refresh the playlist in
external players after changing policy, so they pick up the new output ID.
XMLTV exports programmes overlapping the current/upcoming 48 hours; ingestion
retains interpreted UTC events from 78 hours ago through seven days ahead to allow corrections.
The app deliberately does not add an automatic EPG-fetch header to M3U, since
some players block playlist loading while downloading a guide.

## Free-source research — checked 19 September 2026

### Best direct lead for Dutch Viaplay linear channels

```text
https://epgshare01.online/epgshare01/epg_ripper_NL1.xml.gz
```

The [current NL channel list](https://epgshare01.online/epgshare01/epg_ripper_NL1.txt)
contains `Viaplay.TV.nl` and `Viaplay.TV+.nl` (and a generic `Viaplay.nl` entry).
The [publisher directory](https://epgshare01.online/epgshare01/) lists a feed
updated on 19 September 2026. Listed channel IDs are not a guarantee of complete,
accurate programme data. The generic Viaplay entry may represent a package
rather than the exact linear channel; prefer the specific TV/TV+ match.

For actual **Danish** Viaplay Sports channels, the separate feed
`https://epgshare01.online/epgshare01/epg_ripper_DK1.xml.gz` has
[listed IDs](https://epgshare01.online/epgshare01/epg_ripper_DK1.txt) for
`Viaplay.Sport.1.dk`, `.2.dk`, `.3.dk` and `Viaplay.Sport.News.dk`.
Do not apply Danish listings to Dutch channels just because names look similar.
Neither feed was silently added: only the three requested Rytec URLs are enabled
by this change. Observe the publisher's terms and refresh responsibly.

### Free self-hosted alternatives

- [iptv-org/epg](https://github.com/iptv-org/epg) provides open-source guide
  generation, including [ZiggoGo channel mappings](https://github.com/iptv-org/epg/blob/master/sites/ziggogo.tv/ziggogo.tv.channels.xml).
  Run the grabber/Docker container and add your hosted `guide.xml` URL here.
  This is software to run, not a promise of a free hosted guide endpoint.
- [jbogers/ziggogo-epg](https://github.com/jbogers/ziggogo-epg) is another
  XMLTV-producing ZiggoGo grabber. Its maintainer recommends no more than two
  source fetches per day. Suitability/coverage must be checked on your network.

Numbered, event-only Viaplay streams supplied by a portal do not necessarily
have a fixed correspondence to public TV guide channels. Prefer that portal's
own EPG for those, and review the actual event/channel before assigning a linear
TV guide.

### Sources not recommended as verified current feeds

- The three requested Rytec **HTTP** downloads did not return usable responses
  from the test sandbox. They remain enabled as requested, but live availability
  and programme freshness were not confirmed. A sandbox/network failure is not
  proof the mirrors are down for a NAS in the Netherlands.
- The old [Bevy guide page](https://www.bevy.be/epg-guide/) returned “Page not
  found” during this check.
- [globetvapp's Netherlands folder](https://github.com/globetvapp/epg/tree/main/Netherlands)
  showed its last update as 26 October 2025. It is not a verified fresh feed for
  September 2026.
- iptv-org's current [hosted guide status list](https://github.com/iptv-org/epg/blob/master/GUIDES.md)
  showed its listed servers unavailable. Avoid copying old GitHub Pages guide
  URLs from third-party articles without checking them.

## Further improvements worth considering

- Daily-at-a-specific-time schedules and HTTP conditional requests
  (ETag/Last-Modified).
- Shorter retry backoff specifically for temporarily busy portal MACs.
- Provider-rename alias dictionaries and optional external alert delivery.
- A selectable XMLTV export horizon, compressed XMLTV output, and an optional
  M3U guide-URL header for clients that do not block while fetching it.
