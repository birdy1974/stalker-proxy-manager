#!/usr/bin/env bash
# ============================================================================
# PUID / PGID smoke test for the all-in-one image  (CI: docker-publish > smoke)
#
#   bash dev/smoke-puid.sh
#
# Reproduces the classic bind-mount failure this image used to have:
#
#     PermissionError: [Errno 13] Permission denied: '/media'
#
# A host folder keeps its HOST ownership inside the container, so with the
# built-in spm user (uid/gid 2000) the app cannot list a ./media directory that
# belongs to, say, uid 1000. This script
#
#   1. creates a throw-away host directory owned by $SPM_TEST_UID (mode 750)
#   2. starts the image with that directory bind-mounted at /media and **no**
#      PUID/PGID, and asserts the app user really cannot list it (the bug)
#   3. starts the image again with PUID/PGID set to that owner and asserts
#         a. PID 1 in the container runs as PUID/PGID (entrypoint dropped root)
#         b. the app user CAN list /media                      (the fix)
#         c. /config (state volume) is chowned to PUID:PGID    (stays writable)
#         d. the GUI still answers /login                      (nothing broken)
#         e. the entrypoint reports all of this on STDOUT (single-stream rule,
#            see app/config.py - `docker logs <c> | grep entrypoint` must work)
#
# Needs root (or passwordless sudo) to create the owned test directory; it
# skips with a clear message instead of failing when neither is available.
#
# Overrides: SPM_SMOKE_PUID_IMAGE, SPM_SMOKE_PUID_NAME, SPM_SMOKE_PUID_PORT,
#            SPM_TEST_UID, SPM_TEST_GID.
# ============================================================================
set -euo pipefail

IMAGE="${SPM_SMOKE_PUID_IMAGE:-spm-smoke}"
NAME="${SPM_SMOKE_PUID_NAME:-spm-puid}"
PORT="${SPM_SMOKE_PUID_PORT:-8881}"
TEST_UID="${SPM_TEST_UID:-15000}"
TEST_GID="${SPM_TEST_GID:-15000}"
BASE="http://127.0.0.1:${PORT}"
CURL=(curl --noproxy '*' --silent --show-error --max-time 10)
LOG_ALL="$(mktemp)"; LOG_OUT="$(mktemp)"

step()    { printf '\n== smoke-puid: %s\n' "$*"; }
say()     { printf '   %s\n' "$*"; }
summary() { printf '%s\n' "$*" >> "${GITHUB_STEP_SUMMARY:-/dev/null}" 2>/dev/null || true; }

SUDO=""
if [ "$(id -u)" = "0" ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    SUDO="sudo"
else
    echo "smoke-puid SKIPPED: needs root or passwordless sudo to create a test mount owned by uid ${TEST_UID}"
    summary "- PUID/PGID smoke: SKIPPED (no root/sudo on this host)"
    exit 0
fi

dump_and_die() {  # dump_and_die "why" [container]
    local c="${2:-$NAME}"
    printf '%s: %s\n' "!! smoke-puid FAILED" "$1" >&2
    summary "### Smoke PUID/PGID: FAILED - $1"
    docker ps -a >&2 || true
    [ -n "$c" ] && { docker inspect --format '{{.State.Status}} exit={{.State.ExitCode}} err={{.State.Error}}' "$c" >&2 || true; }
    [ -n "$c" ] && { say "container log of ${c} follows" >&2; docker logs "$c" >&2 2>&1 || true; }
    exit 1
}

summary "### Smoke PUID/PGID: image \`${IMAGE}\`, test ids ${TEST_UID}:${TEST_GID}"

# ---------------------------------------------------------------------------
# 0) a host directory that only uid $TEST_UID may list (mode 750)
# ---------------------------------------------------------------------------
step "creating a test media directory owned by ${TEST_UID}:${TEST_GID} (mode 750)"
MEDIA_HOST="$(mktemp -d)/media"
$SUDO mkdir -p "$MEDIA_HOST/movies"
$SUDO chown -R "${TEST_UID}:${TEST_GID}" "$MEDIA_HOST"
$SUDO chmod 750 "$MEDIA_HOST" "$MEDIA_HOST/movies"
say "mount source: ${MEDIA_HOST}"
$SUDO ls -ldn "$MEDIA_HOST"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rm -f "${NAME}-wrong" >/dev/null 2>&1 || true
    [ -n "${MEDIA_HOST:-}" ] && $SUDO rm -rf "$(dirname "$MEDIA_HOST")" 2>/dev/null || true
    rm -f "$LOG_ALL" "$LOG_OUT" || true
}
trap cleanup EXIT

wait_booted() {  # wait_booted <container> <seconds> - until the sqlite db exists
    local c="$1" max="${2:-90}" i
    for ((i = 1; i <= max; i++)); do
        if docker exec "$c" sh -c 'test -f /config/spm.db' >/dev/null 2>&1; then
            say "${c}: booted after ${i}s"
            return 0
        fi
        if ! docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null | grep -qx true; then
            dump_and_die "${c} is not running any more" "$c"
        fi
        sleep 1
    done
    return 1
}

start_container() {  # start_container <name> <port> [extra docker run args...]
    local c="$1" p="$2"; shift 2
    docker rm -f "$c" >/dev/null 2>&1 || true
    docker run -d --name "$c" -p "${p}:8880" \
        -e SPM_MOCK_PORTAL=1 -e SPM_ADMIN_PASSWORD=ci \
        -v "${MEDIA_HOST}:/media" "$@" "$IMAGE"
}

# ---------------------------------------------------------------------------
# 1) the bug: default ids -> the app user must NOT be able to list /media
# ---------------------------------------------------------------------------
step "starting ${IMAGE} WITHOUT PUID/PGID (expecting the permission problem)"
start_container "${NAME}-wrong" "$((PORT + 1))"
wait_booted "${NAME}-wrong" 90 || dump_and_die "${NAME}-wrong never booted" "${NAME}-wrong"
if docker exec -u 2000:2000 "${NAME}-wrong" sh -c 'ls -1 /media >/dev/null' 2>/dev/null; then
    dump_and_die "/media is readable as uid 2000 - the test mount is not restrictive (mode/owner)" "${NAME}-wrong"
fi
say "OK: as uid 2000 the media mount is NOT listable (bug reproduced)"
summary "- default uid 2000 cannot list /media (bug reproduced)"

# ---------------------------------------------------------------------------
# 2) the fix: PUID/PGID = owner of the mount
# ---------------------------------------------------------------------------
step "starting ${IMAGE} WITH PUID=${TEST_UID} PGID=${TEST_GID}"
start_container "$NAME" "$PORT" -e "PUID=${TEST_UID}" -e "PGID=${TEST_GID}"
wait_booted "$NAME" 90 || dump_and_die "$NAME never booted" "$NAME"

step "PID 1 runs as PUID/PGID"
app_uid="$(docker exec "$NAME" sh -c 'grep "^Uid:" /proc/1/status | cut -f2')"
app_gid="$(docker exec "$NAME" sh -c 'grep "^Gid:" /proc/1/status | cut -f2')"
say "PID 1 -> uid=${app_uid} gid=${app_gid}"
[ "$app_uid" = "$TEST_UID" ] || dump_and_die "PID 1 runs as uid ${app_uid}, expected ${TEST_UID}"
[ "$app_gid" = "$TEST_GID" ] || dump_and_die "PID 1 runs as gid ${app_gid}, expected ${TEST_GID}"
summary "- PID 1 runs as \`${TEST_UID}:${TEST_GID}\`"

step "the app user can list /media"
docker exec -u "${TEST_UID}:${TEST_GID}" "$NAME" sh -c 'ls -1 /media >/dev/null' \
    || dump_and_die "the app user still cannot list /media with PUID/PGID set"
say "OK: /media is listable as uid ${TEST_UID}"
summary "- /media listable as \`${TEST_UID}:${TEST_GID}\`"

step "/config was chowned to PUID:PGID"
cfg_ids="$(docker exec "$NAME" sh -c 'stat -c "%u:%g" /config/spm.db')"
say "/config/spm.db -> ${cfg_ids}"
[ "$cfg_ids" = "${TEST_UID}:${TEST_GID}" ] \
    || dump_and_die "/config/spm.db is owned by ${cfg_ids}, expected ${TEST_UID}:${TEST_GID}"
summary "- /config chowned to \`${TEST_UID}:${TEST_GID}\`"

step "GUI still answers on ${BASE}/login"
code=000
for ((i = 1; i <= 90; i++)); do
    code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/login" 2>/dev/null || true)"
    [ "$code" = "200" ] && break
    if ! docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -qx true; then
        dump_and_die "container is not running any more (last code ${code})"
    fi
    sleep 1
done
say "probe -> ${code}"
[ "$code" = "200" ] || dump_and_die "/login never answered 200 (last code: ${code})"
summary "- \`/login\` -> 200 OK"

step "entrypoint diagnostics on container STDOUT (single-stream rule)"
docker logs "$NAME" >"$LOG_ALL" 2>&1 || true
grep -qF '[entrypoint]' "$LOG_ALL" || dump_and_die "no [entrypoint] lines in the container log"
grep -qF "[entrypoint] /media -> read" "$LOG_ALL" \
    || dump_and_die "the entrypoint did not report media access (see log dump)"
# never pipe `docker logs` into grep: under `pipefail` a `grep -q` that exits
# early kills the pipeline with SIGPIPE - capture to a file first (see smoke.sh)
docker logs "$NAME" 2>/dev/null >"$LOG_OUT" || true
grep -qF '[entrypoint]' "$LOG_OUT" \
    || dump_and_die "the [entrypoint] lines are not on container STDOUT (see app/config.py)"
say "found on stdout: $(grep -F '[entrypoint]' "$LOG_ALL" | head -1)"
summary "- entrypoint diagnostics on STDOUT"

echo
echo "smoke-puid OK"
summary "### Smoke PUID/PGID: PASSED"
