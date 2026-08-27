#!/usr/bin/env bash
# Seed a fresh SPM database with a full demo setup against the built-in mock
# portal — meant for mockup/preview/dev use only (run with SPM_SKIP_LOGIN=1).
#
# Usage:  dev/seed-demo.sh [BASE_URL]     (default http://127.0.0.1:8880)
#
# Requires the app already running (mock portal is served by the app itself
# at /mock/c/). Idempotent: skips the portal if it already exists.
set -euo pipefail
BASE="${1:-http://127.0.0.1:8880}"

py() { python3 -c "$1" "$2"; }

echo "seed-demo: using $BASE"

# -- 1. mock portal -----------------------------------------------------------
PORTAL_ID=$(curl -sf "$BASE/api/portals?per_page=200" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(next((p["id"] for p in d.get("items",d if isinstance(d,list) else []) if "/mock/c/" in (p.get("base_url") or "")), ""))' 2>/dev/null || true)

if [[ -z "${PORTAL_ID}" ]]; then
  PORTAL_ID=$(curl -sf -X POST "$BASE/api/portals" -H 'Content-Type: application/json' -d '{
    "name": "Mock Cinema (demo)",
    "base_url": "http://127.0.0.1:8880/mock/c/",
    "macs": "00:1A:79:AA:AA:01, 00:1A:79:AA:AA:02",
    "enabled": true}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  echo "seed-demo: created portal id=$PORTAL_ID"
else
  echo "seed-demo: mock portal already exists (id=$PORTAL_ID)"
fi

# -- 2. fetch pass #1: only the genre CATALOG is synced on first fetch -------
# (fresh genres are created disabled; item import happens for enabled genres)
wait_job_done() {
  for i in $(seq 1 60); do
    sleep 1
    J=$(curl -sf "$BASE/api/portals" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("running" if any(j["status"] in ("pending","running") for j in d.get("jobs", [])) else "idle")')
    [[ "$J" == "idle" ]] && return 0
  done
  return 1
}
echo "seed-demo: fetch pass 1 (genre catalog)…"
curl -sf -X POST "$BASE/api/portals/$PORTAL_ID/fetch" >/dev/null
wait_job_done || echo "seed-demo: WARN pass 1 slow"

# -- 3. enable all genres ----------------------------------------------------
curl -sf "$BASE/api/portals/$PORTAL_ID/genres" | python3 -c '
import json,sys
g=json.load(sys.stdin)
ids=[x["id"] for lst in g.values() if isinstance(lst,list) for x in lst]
print(" ".join(map(str,ids)))' > /tmp/spm_genre_ids
if [[ -s /tmp/spm_genre_ids ]]; then
  for KIND in live vod series; do
    GJSON=$(curl -sf "$BASE/api/portals/$PORTAL_ID/genres" | python3 -c "
import json,sys
g=json.load(sys.stdin)
ids=[x['id'] for x in g.get('$KIND', [])]
print(','.join(map(str, ids)))")
    [[ -z "$GJSON" ]] && continue
    curl -sf -X POST "$BASE/api/portals/$PORTAL_ID/genres/toggle" -H 'Content-Type: application/json' \
      -d "{\"kind\": \"$KIND\", \"ids\": [${GJSON}], \"enabled\": true}" >/dev/null
  done
  echo "seed-demo: all genres enabled (live/vod/series)"
fi

# -- 2b. fetch pass #2: now the enabled genres are actually imported ----------
echo -n "seed-demo: fetch pass 2 (items)"
curl -sf -X POST "$BASE/api/portals/$PORTAL_ID/fetch" >/dev/null
for i in $(seq 1 60); do
  sleep 1; echo -n .
  L=$(curl -sf "$BASE/api/sources/live?per_page=1"    | python3 -c 'import json,sys;print(json.load(sys.stdin)["total"])' || echo 0)
  S=$(curl -sf "$BASE/api/sources/series?per_page=1"  | python3 -c 'import json,sys;print(json.load(sys.stdin)["total"])' || echo 0)
  [[ "$L" -gt 0 && "$S" -gt 0 ]] && break
done
echo " (live=$L series=$S)"

# -- 4. enable a healthy chunk of sources ------------------------------------
enable_chunk() { # kind, limit
  curl -sf "$BASE/api/sources/$1?per_page=500" | python3 -c '
import json,sys
d=json.load(sys.stdin)
ids=[r["id"] for r in d["items"]][:'"$2"']
print(",".join(map(str,ids)))' | while read -r IDS; do
    [[ -z "$IDS" ]] && continue
    curl -sf -X POST "$BASE/api/sources/toggle" -H 'Content-Type: application/json' \
      -d "{\"kind\": \"$1\", \"ids\": [${IDS}], \"enabled\": true}" >/dev/null
    echo "seed-demo: enabled $1 ($(echo "$IDS" | tr ',' '\n' | wc -l) items)"
  done
}
enable_chunk live 40
enable_chunk vod 14
enable_chunk series 10

# -- 4b. fetch pass 3: seasons/episodes need the series ENABLED at fetch time -
curl -sf -X POST "$BASE/api/portals/$PORTAL_ID/fetch" >/dev/null
wait_job_done || echo "seed-demo: WARN pass 3 slow"
echo "seed-demo: fetch pass 3 done (seasons/episodes for enabled series)"

# -- 5. build the output playlist --------------------------------------------
# live: 10 custom channels, every 3rd gets a second source as fallback demo
curl -sf "$BASE/api/sources/live?per_page=40&enabled=true" | python3 -c '
import json,sys,urllib.request
d=json.load(sys.stdin); rows=d["items"]; base=sys.argv[1]
for i,r in enumerate(rows[:10]):
    chain=[r["id"]]
    if i%3==2 and len(rows)>i+1: chain.append(rows[(i+13)%len(rows)]["id"])
    body=json.dumps({"custom_name":r["name"],"group_name":"Demo Group "+str(i%3+1),
                     "epg_id":(r.get("cmd") or "")[-6:] or None,
                     "logo":r.get("logo"),"source_ids":chain}).encode()
    req=urllib.request.Request(base+"/api/playlist/live",data=body,headers={"Content-Type":"application/json"})
    urllib.request.urlopen(req)
print("seed-demo: %d custom live channels created" % min(10,len(rows)))
' "$BASE"

# vod + series: add-from-source for the enabled ones
curl -sf "$BASE/api/sources/vod?per_page=100&enabled=true"    | python3 -c '
import json,sys,urllib.request
d=json.load(sys.stdin); base=sys.argv[1]
for r in d["items"]:
    body=json.dumps({"kind":"vod","source_id":r["id"]}).encode()
    urllib.request.urlopen(urllib.request.Request(base+"/api/playlist/add-from-source",
        data=body,headers={"Content-Type":"application/json"}))
print("seed-demo: %d vod items in playlist" % len(d["items"]))
' "$BASE"
curl -sf "$BASE/api/sources/series?per_page=100&enabled=true" | python3 -c '
import json,sys,urllib.request
d=json.load(sys.stdin); base=sys.argv[1]
for r in d["items"]:
    body=json.dumps({"kind":"series","source_id":r["id"]}).encode()
    urllib.request.urlopen(urllib.request.Request(base+"/api/playlist/add-from-source",
        data=body,headers={"Content-Type":"application/json"}))
print("seed-demo: %d series in playlist" % len(d["items"]))
' "$BASE"

# -- 6. local demo video (tiny generated mpegts, if ffmpeg available) --------
FF=$(python3 -c '
try:
    import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())
except Exception: pass' 2>/dev/null || true)
if [[ -n "$FF" ]]; then
  MDIR="$(pwd)/data/media"
  mkdir -p "$MDIR/demo"
  "$FF" -nostdin -y -f lavfi -i "testsrc=size=640x360:rate=25" -f lavfi -i "sine=frequency=440:sample_rate=48000" \
        -t 20 -c:v libx264 -pix_fmt yuv420p -c:a aac "$MDIR/demo/demo-card.ts" >/dev/null 2>&1 || true
  "$FF" -nostdin -y -f lavfi -i "testsrc=size=1280x720:rate=25" -f lavfi -i "sine=frequency=660:sample_rate=48000" \
        -t 20 -c:v libx264 -pix_fmt yuv420p -c:a aac "$MDIR/demo/patterns-720p.ts" >/dev/null 2>&1 || true
  "$FF" -nostdin -y -f lavfi -i "testsrc=size=1920x1080:rate=50" -f lavfi -i "sine=frequency=520:sample_rate=48000" \
        -t 20 -c:v libx264 -pix_fmt yuv420p -c:a aac "$MDIR/demo/intro-1080p50.mp4" >/dev/null 2>&1 || true
  if [[ -s "$MDIR/demo/demo-card.ts" ]]; then
    curl -sf -X POST "$BASE/api/sources/local/dirs" -H 'Content-Type: application/json' \
      -d "{\"directory\": \"$MDIR\", \"recursive\": true}" >/dev/null && echo "seed-demo: local dir added"
    curl -sf -X POST "$BASE/api/sources/local/scan" -H 'Content-Type: application/json' -d '{}' >/dev/null
    sleep 2
    curl -sf "$BASE/api/sources/local/files?per_page=100" | python3 -c '
import json,sys,urllib.request
d=json.load(sys.stdin); base=sys.argv[1]
ids=[r["id"] for r in d["items"]]
for i in ids:
    body=json.dumps({"kind":"localfile","source_id":i}).encode()
    urllib.request.urlopen(urllib.request.Request(base+"/api/playlist/add-from-source",
        data=body,headers={"Content-Type":"application/json"}))
print(f"seed-demo: {len(ids)} local files added to playlist")
' "$BASE" || true
  fi
else
  echo "seed-demo: no ffmpeg binary found — skipping local demo files"
fi

# -- 7. demo output user (shows the M3U/Xtream URLs on the Users page) -------
if ! curl -sf "$BASE/api/users" | grep -q '"name":"demo"'; then
  curl -sf -X POST "$BASE/api/users" -H 'Content-Type: application/json' -d '{
    "name": "demo", "password": "demo123", "m3u_enabled": true,
    "xtream_enabled": true, "enabled": true, "max_connections": 2}' >/dev/null     && echo "seed-demo: demo user created (demo / demo123)"
fi

echo "seed-demo: DONE"
