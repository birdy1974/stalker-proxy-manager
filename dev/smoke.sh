#!/usr/bin/env bash
# ============================================================================
# Boot smoke test for the all-in-one image   (CI: docker-publish.yml > smoke)
#
#   bash dev/smoke.sh
#
# Checks, in order:
#   1. the container starts and the admin GUI answers GET /login with 200
#   2. the built-in mock portal handshake returns a token
#   3. the "Stalker Proxy Manager" boot marker is in the container log
#   4. ...and that it is on container STDOUT (single-stream logging contract,
#      see app/config.py: the Docker logging driver keeps stdout and stderr
#      apart and the CLI re-emits them separately, so anything logged on
#      stderr is invisible to `docker logs <c> | grep ...`)
#
# Robustness rules in this script - each one maps to a real failure this job
# has produced in the past:
#   * leftover containers are removed before AND after the run, so a failed
#     run can never break the next one;
#   * readiness is *polled*, never a fixed sleep: uvicorn binds the port only
#     after init_db/seed/migrations have finished (~1.5 s), so the first
#     probes legitimately get connection-refused;
#   * $code is initialised and nothing relies on an external `seq`;
#   * curl bypasses HTTP(S)_PROXY for localhost (--noproxy '*');
#   * the container log is captured to a FILE with `2>&1` before grepping:
#     under `pipefail` a `docker logs | grep -q` pipeline dies on SIGPIPE, and
#     a bare pipe silently drops the container's stderr;
#   * every probe is printed, and any failure dumps `docker ps -a`, the
#     container state and the captured log, so the CI annotation explains
#     itself without re-running anything.
#
# Overrides: SPM_SMOKE_IMAGE, SPM_SMOKE_NAME, SPM_SMOKE_PORT, SPM_SMOKE_MAC.
# ============================================================================
set -euo pipefail

IMAGE="${SPM_SMOKE_IMAGE:-spm-smoke}"
NAME="${SPM_SMOKE_NAME:-spm}"
PORT="${SPM_SMOKE_PORT:-8880}"
MAC="${SPM_SMOKE_MAC:-00:1A:79:AA:AA:01}"
MARKER="Stalker Proxy Manager"
BASE="http://127.0.0.1:${PORT}"
CURL=(curl --noproxy '*' --silent --show-error --max-time 10)
# full merged log (stdout+stderr) and stdout-only log, for checks 3 and 4
LOG_ALL="$(mktemp)"; LOG_OUT="$(mktemp)"

step()  { printf '\n== smoke: %s\n' "$*"; }
say()   { printf '   %s\n' "$*"; }
summary() { printf '%s\n' "$*" >> "${GITHUB_STEP_SUMMARY:-/dev/null}" 2>/dev/null || true; }

dump_and_die() {  # dump_and_die "why" [extra-file]
    printf '%s: %s\n' "!! smoke FAILED" "$1" >&2
    summary "### Smoke: FAILED - $1"
    capture_logs || true            # fresh log, so the dump matches the verdict
    docker ps -a >&2 || true
    docker inspect --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} err={{.State.Error}}' "$NAME" >&2 || true
    say "container log (stdout+stderr) follows" >&2
    cat "$LOG_ALL" >&2 || true
    exit 1
}

summary "### Smoke: image \`${IMAGE}\`, container \`${NAME}\`, port ${PORT}"

step "removing leftover container (if any)"
docker rm -f "$NAME" >/dev/null 2>&1 || true

step "starting ${IMAGE} (host port ${PORT}, mock portal on)"
cid="$(docker run -d --name "$NAME" -p "${PORT}:8880" \
    -e SPM_MOCK_PORTAL=1 -e SPM_ADMIN_PASSWORD=ci "$IMAGE")"
say "container id: ${cid}"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -f "$LOG_ALL" "$LOG_OUT" || true
}
trap cleanup EXIT

# refresh both log captures (merged + stdout-only); never a pipe into grep
capture_logs() {
    docker logs "$NAME" >"$LOG_ALL" 2>&1 || true
    docker logs "$NAME" 2>/dev/null >"$LOG_OUT" || true
}
# NB: no log assertions here - right after `docker run -d` the log is legally
# still empty (the image boots in ~1 s); an empty/broken CMD surfaces as a
# failed readiness probe below, where the dump explains it.

# ---------------------------------------------------------------------------
# 1) GUI readiness
# ---------------------------------------------------------------------------
step "waiting for the GUI on ${BASE}/login"
code=000
for ((i = 1; i <= 90; i++)); do
    code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/login" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
        say "probe ${i}/90 -> 200 (GUI ready)"
        break
    fi
    # readable CI log: show the first probe and every 10th only
    if (( i == 1 || i % 10 == 0 )); then
        say "probe ${i}/90 -> ${code}"
    fi
    if ! docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -qx true; then
        dump_and_die "container is not running any more (GUI never answered; last code ${code})"
    fi
    sleep 1
done
capture_logs
[ "$code" = "200" ] || dump_and_die "/login never answered 200 (last code: ${code})"
summary "- \`/login\` -> 200 OK"

# ---------------------------------------------------------------------------
# 2) Mock portal handshake (canonical STB shape)
# ---------------------------------------------------------------------------
step "mock portal handshake (${MAC})"
body="$("${CURL[@]}" -f "$BASE/mock/c/portal.php?type=stb&action=handshake&mac=${MAC}" 2>/dev/null || true)"
say "handshake response: ${body:-<empty>}"
grep -q '"token"' <<<"$body" || dump_and_die "handshake returned no token: ${body:-<empty>}"
summary "- mock handshake -> token OK"

# ---------------------------------------------------------------------------
# 3+4) Boot marker: present, and on stdout (the single-stream contract)
# ---------------------------------------------------------------------------
step "boot marker in the container log"
capture_logs
grep -qF "$MARKER" "$LOG_ALL" || dump_and_die "boot marker \"${MARKER}\" missing from the container log"
say "marker found in stdout+stderr log ($(wc -l <"$LOG_ALL") lines)"
# If this fails, something is logging to stderr again: fix the app (all
# records go to stdout), do not work around it here by suppressing uvicorn.
grep -qF "$MARKER" "$LOG_OUT" \
    || dump_and_die "boot marker is not on container stdout - \`docker logs <c> | grep\` cannot see it (see app/config.py)"
say "marker found on stdout too (single-stream logging OK)"

echo
echo "smoke OK"
summary "### Smoke: PASSED"
