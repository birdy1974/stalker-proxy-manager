#!/usr/bin/env bash
# ============================================================================
# Boot smoke test for the all-in-one image.
#
# Intended to be invoked from .github/workflows/docker-publish.yml (smoke job)
# as:  bash dev/smoke.sh
# It runs the freshly built image with the built-in mock portal and verifies:
#   1. the admin GUI answers on /login (HTTP 200)
#   2. the mock portal handshake returns a token
#   3. the "Stalker Proxy Manager" boot marker is in the container log
#
# Reasons this script is more robust than the old inline code:
#   * it removes leftover "spm" containers before/after the run (a previous
#     failed run must never break the next one),
#   * it initialises $code and never relies on an external `seq`,
#   * it bypasses HTTP(S)_PROXY for localhost (--noproxy '*'),
#   * every probe result is printed, and failures dump 'docker ps -a' plus the
#     container logs before exiting, so the CI annotation is self-explanatory,
#   * grep operates on files/herestrings, not on pipes (no pipefail/SIGPIPE
#     surprises).
#
# Environment overrides:  SPM_SMOKE_IMAGE, SPM_SMOKE_NAME, SPM_SMOKE_PORT.
# ============================================================================
set -euo pipefail

IMAGE="${SPM_SMOKE_IMAGE:-spm-smoke}"
NAME="${SPM_SMOKE_NAME:-spm}"
PORT="${SPM_SMOKE_PORT:-8880}"
MAC="00:1A:79:AA:AA:01"
BASE="http://127.0.0.1:${PORT}"
CURL="curl --noproxy '*' --silent --show-error --max-time 5"

summary() { printf '%s\n' "$*" >> "${GITHUB_STEP_SUMMARY:-/dev/null}" 2>/dev/null || true; }

echo "== smoke: remove leftover container (if any)"
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "== smoke: start ${IMAGE} as ${NAME} (host port ${PORT})"
cid="$(docker run -d --name "$NAME" -p "${PORT}:8880" \
    -e SPM_MOCK_PORTAL=1 -e SPM_ADMIN_PASSWORD=ci "$IMAGE")"
echo "   container id: ${cid}"
summary "### Smoke: container \`${cid}\` started"

cleanup() {
    # stop the container, then remove its name so the next run starts clean
    docker logs "$NAME" >/dev/null 2>&1 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker port "$NAME" || { docker ps -a; exit 1; }

# ---------------------------------------------------------------------------
# 1) GUI readiness - poll /login until it answers 200
# ---------------------------------------------------------------------------
code=000
for ((i = 1; i <= 90; i++)); do
    code="$($CURL -s -o /dev/null -w '%{http_code}' "$BASE/login" 2>/dev/null || true)"
    printf '   login probe %3d/90 -> %s\n' "$i" "$code"
    [ "$code" = "200" ] && break
    sleep 1
done
if [ "$code" != "200" ]; then
    echo "!! /login never answered 200 (last code: ${code})" >&2
    summary "### Smoke: FAILED - /login never answered 200 (last code: \`${code}\`)"
    docker ps -a >&2
    docker logs "$NAME" >&2 || true
    exit 1
fi
summary "- \`/login\` -> 200 OK"

# ---------------------------------------------------------------------------
# 2) Mock portal handshake (canonical STB shape: type=stb&action=handshake)
# ---------------------------------------------------------------------------
echo "== smoke: mock portal handshake (${MAC})"
body="$($CURL -sSf "$BASE/mock/c/portal.php?type=stb&action=handshake&mac=${MAC}" 2>/dev/null || true)"
printf '   handshake response: %s\n' "$body"
if ! grep -q '"token"' <<<"$body"; then
    echo "!! handshake response did not contain a token: ${body}" >&2
    summary "### Smoke: FAILED - handshake had no token (response: \`${body}\`)"
    docker logs "$NAME" >&2 || true
    exit 1
fi
summary "- mock handshake -> token OK"

# ---------------------------------------------------------------------------
# 3) Boot marker in the container log
# ---------------------------------------------------------------------------
echo "== smoke: boot marker in container log"
if ! docker logs "$NAME" > /tmp/spm-smoke.log 2>&1 || ! grep -qF "Stalker Proxy Manager" /tmp/spm-smoke.log; then
    echo "!! boot marker missing from container log" >&2
    summary "### Smoke: FAILED - boot marker missing"
    cat /tmp/spm-smoke.log >&2 || true
    exit 1
fi
summary "- boot marker \"Stalker Proxy Manager\" present OK"

echo "smoke OK"
summary "### Smoke: PASSED"
