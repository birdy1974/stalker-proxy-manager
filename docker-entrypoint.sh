#!/usr/bin/env bash
# ============================================================================
# Stalker Proxy Manager - container entrypoint (PUID / PGID support)
# ----------------------------------------------------------------------------
# Starts as root for a few milliseconds, then hands the CMD to the
# unprivileged app user. Two jobs, both caused by *bind mounts*:
#
#   1. PUID / PGID
#      The image ships a built-in `spm` account (uid/gid 2000), but a host
#      directory bind-mounted into the container keeps its HOST ownership. If
#      ./media belongs to uid 1000 with mode 750, the app cannot even list it:
#
#          PermissionError: [Errno 13] Permission denied: '/media'
#
#      Setting PUID/PGID to the owner of the mount
#          stat -c '%u:%g' ./media          # or: id <your-host-user>
#      re-aligns the in-container user with the host one (same idea as the
#      linuxserver.io images).
#
#   2. chown the data dir (state volume) so it stays writable after an id
#      change, and join the groups hardware transcoding needs (/dev/dri).
#
# Everything is logged to STDOUT on purpose - see the "single-stream rule" in
# app/config.py: `docker logs <c> | grep ...` only ever searches stdout, so a
# warning on stderr would be invisible to exactly the command you would use to
# debug this.
#
# Environment:
#   PUID / PGID                  uid/gid to run as (default 2000/2000 = the
#                                image's built-in spm user, i.e. unchanged
#                                behaviour when you do not set them)
#   SPM_SKIP_CHOWN=1             never chown anything at boot (read-only or
#                                root-squashed NFS/SMB mounts)
#   SPM_CHOWN_MEDIA=1            also chown the media root itself
#                     =recursive ... and everything below it (slow on huge
#                                libraries; off by default)
#   SPM_CHOWN_EXTRA="/a /b"      extra paths to chown (recursive)
#   SPM_AUTO_DRI_GROUP=0         do not join the group owning the VAAPI
#                                render node (default: 1 = join it)
#   SPM_EXTRA_GROUPS=44,989      extra group ids for the app user
#   SPM_USER=spm                 override the app account name (rare)
#
# When the container is started with a `user:` / `--user` override we are not
# root and cannot do any of this: the ids are applied by Docker instead, so we
# just exec the CMD.
# ============================================================================
set -euo pipefail

APP_USER="${SPM_USER:-spm}"
DEFAULT_UID="${SPM_DEFAULT_UID:-2000}"
DEFAULT_GID="${SPM_DEFAULT_GID:-2000}"
DATA_DIR="${SPM_DATA_DIR:-/config}"
MEDIA_ROOT="${SPM_MEDIA_ROOT:-/media}"
VAAPI_DEVICE="${SPM_VAAPI_DEVICE:-/dev/dri/renderD128}"

_ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log()  { printf '%s [entrypoint] %s\n' "$(_ts)" "$*"; }
warn() { printf '%s [entrypoint] WARNING: %s\n' "$(_ts)" "$*"; }   # stdout, see header

# ---------------------------------------------------------------------------
# 0) nothing to do when we are not root (docker run --user / compose `user:`)
# ---------------------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
    log "already running as uid=$(id -u) gid=$(id -g) - skipping PUID/PGID setup"
    log "(remove the user: override to let the entrypoint apply PUID/PGID)"
    [ "$#" -gt 0 ] || set -- python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${SPM_PORT:-8880}"
    exec "$@"
fi

# ---------------------------------------------------------------------------
# 1) resolve PUID / PGID (numeric, or a name that exists inside the image)
# ---------------------------------------------------------------------------
# Result lands in $RESOLVED, not on stdout: warn() also writes to stdout (the
# single-stream rule), and a captured `$(resolve_id ...)` would swallow it.
RESOLVED=""
resolve_id() {  # $1 raw value, $2 fallback, $3 id flag (u|g), $4 label
    local raw="${1:-}" fallback="$2" flag="$3" label="$4" out=""
    if [ -z "$raw" ]; then RESOLVED="$fallback"; return 0; fi
    if [[ "$raw" =~ ^[0-9]+$ ]]; then RESOLVED="$raw"; return 0; fi
    if out="$(id "-$flag" -- "$raw" 2>/dev/null)"; then RESOLVED="$out"; return 0; fi
    warn "$label='$raw' is neither numeric nor a known name in the image - using $fallback"
    RESOLVED="$fallback"
}

resolve_id "${PUID:-}" "$DEFAULT_UID" u PUID; TARGET_UID="$RESOLVED"
resolve_id "${PGID:-}" "$DEFAULT_GID" g PGID; TARGET_GID="$RESOLVED"

# PUID=0 means "run as root": no chown (pointless) and no privilege drop.
if [ "$TARGET_UID" = "0" ]; then
    log "PUID=0 - running as root (privileges are NOT dropped)"
    mkdir -p "$DATA_DIR" 2>/dev/null || true
    [ -n "$MEDIA_ROOT" ] && { mkdir -p "$MEDIA_ROOT" 2>/dev/null || true; }
    [ "$#" -gt 0 ] || set -- python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${SPM_PORT:-8880}"
    exec "$@"
fi

if ! id -u -- "$APP_USER" >/dev/null 2>&1; then
    warn "user '$APP_USER' does not exist in this image - running as root instead"
    [ "$#" -gt 0 ] || set -- python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${SPM_PORT:-8880}"
    exec "$@"
fi

# ---------------------------------------------------------------------------
# 2) move the app account (+ its primary group) to PUID / PGID
# ---------------------------------------------------------------------------
CUR_UID="$(id -u -- "$APP_USER")"
CUR_GID="$(id -g -- "$APP_USER")"
# Non-fatal on purpose: usermod/groupmod can be refused ("user is currently
# used by process"), and a container that refuses to boot is worse than one
# that boots with a warning - the process is set to PUID/PGID either way
# (setpriv/gosu does not need /etc/passwd to agree).
[ "$TARGET_UID" = "$CUR_UID" ] || {
    if usermod -o -u "$TARGET_UID" -- "$APP_USER" 2>/dev/null; then
        log "$APP_USER: uid $CUR_UID -> $TARGET_UID"
    else
        warn "could not move $APP_USER to uid $TARGET_UID (running as it anyway; files outside $DATA_DIR keep uid $CUR_UID)"
    fi
}
[ "$TARGET_GID" = "$CUR_GID" ] || {
    if groupmod -o -g "$TARGET_GID" -- "$APP_USER" 2>/dev/null; then
        log "$APP_USER: gid $CUR_GID -> $TARGET_GID"
    else
        warn "could not move group $APP_USER to gid $TARGET_GID (running as it anyway)"
    fi
}

# ---------------------------------------------------------------------------
# 3) supplementary groups: the VAAPI render node (+ anything extra)
#    Without this, a custom PUID loses Quick Sync: /dev/dri/renderD128 is
#    usually root:<render group> 0660 on the host.
# ---------------------------------------------------------------------------
SUP_GROUPS="$TARGET_GID"
add_group() {
    local g="${1:-}"
    [ -n "$g" ] || return 0
    case ",$SUP_GROUPS," in *",$g,"*) return 0 ;; esac
    SUP_GROUPS="${SUP_GROUPS},${g}"
    log "joining group $g (${2:-unspecified})"
}
if [ "${SPM_AUTO_DRI_GROUP:-1}" = "1" ]; then
    for dev in "$VAAPI_DEVICE" /dev/dri/renderD128 /dev/dri/renderD129; do
        [ -e "$dev" ] || continue
        dri_gid="$(stat -c '%g' "$dev" 2>/dev/null || true)"
        [ -n "$dri_gid" ] && [ "$dri_gid" != "0" ] && add_group "$dri_gid" "owner of $dev"
        break
    done
fi
# shellcheck disable=SC2086  # commas -> word splitting is the point here
_spm_extra="${SPM_EXTRA_GROUPS:-}"
for g in ${_spm_extra//,/ }; do add_group "$g" "SPM_EXTRA_GROUPS"; done
# keep /etc/group in the picture too, so `su` (the fallback launcher below)
# and anything resolving names gets the same membership
{
    IFS=',' read -r -a _gids <<<"$SUP_GROUPS" || true
    for g in "${_gids[@]}"; do
        [ -n "$g" ] || continue
        getent group "$g" >/dev/null 2>&1 || groupadd -o -g "$g" "spm-gid-$g" >/dev/null 2>&1 || true
        usermod -aG "$g" -- "$APP_USER" >/dev/null 2>&1 || true
    done
}

# ---------------------------------------------------------------------------
# 4) ownership: data dir (ours) yes - media root only when explicitly asked
#    The media mount is the user's data: silently chowning it would rewrite
#    host ownership, so PUID/PGID is the fix and SPM_CHOWN_MEDIA the opt-in.
# ---------------------------------------------------------------------------
APP_HOME="$(getent passwd -- "$APP_USER" | cut -d: -f6)"
[ -n "$APP_HOME" ] || APP_HOME="/tmp"

if [ "${SPM_SKIP_CHOWN:-0}" = "1" ]; then
    log "SPM_SKIP_CHOWN=1 - leaving all ownership untouched"
else
    chown_dirs=("$DATA_DIR")
    [ -d "$APP_HOME" ] && [ "$APP_HOME" != "/" ] && chown_dirs+=("$APP_HOME")
    for extra in ${SPM_CHOWN_EXTRA:-}; do chown_dirs+=("$extra"); done
    for d in "${chown_dirs[@]}"; do
        [ -n "$d" ] && [ "$d" != "/" ] || continue      # never chown /
        mkdir -p "$d" 2>/dev/null || { warn "cannot create $d"; continue; }
        if chown -R "$TARGET_UID:$TARGET_GID" "$d" 2>/dev/null; then
            log "chown -R $TARGET_UID:$TARGET_GID $d"
        else
            warn "could not chown $d (read-only mount, or NFS/SMB root squash) - the app may not be able to write there"
        fi
    done
    case "${SPM_CHOWN_MEDIA:-0}" in
        1|true|yes)   if chown "$TARGET_UID:$TARGET_GID" "$MEDIA_ROOT" 2>/dev/null; then
                          log "chown $TARGET_UID:$TARGET_GID $MEDIA_ROOT (top level only)"
                      else
                          warn "could not chown $MEDIA_ROOT (read-only mount, or NFS/SMB root squash)"
                      fi ;;
        recursive|all) if chown -R "$TARGET_UID:$TARGET_GID" "$MEDIA_ROOT" 2>/dev/null; then
                          log "chown -R $TARGET_UID:$TARGET_GID $MEDIA_ROOT"
                      else
                          warn "could not chown $MEDIA_ROOT recursively"
                      fi ;;
    esac
fi

# a mount point we had to create ourselves is owned by root - hand it over
[ -n "$MEDIA_ROOT" ] && [ ! -d "$MEDIA_ROOT" ] && {
    mkdir -p "$MEDIA_ROOT" 2>/dev/null || true
    chown "$TARGET_UID:$TARGET_GID" "$MEDIA_ROOT" 2>/dev/null || true
}
[ ! -d "$DATA_DIR" ] && { mkdir -p "$DATA_DIR" 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# 5) helpers to act *as* the app user (also used for the boot diagnostics)
# ---------------------------------------------------------------------------
as_app() {
    if command -v setpriv >/dev/null 2>&1; then
        setpriv --reuid "$TARGET_UID" --regid "$TARGET_GID" \
                --groups "$SUP_GROUPS" "$@"
    elif command -v gosu >/dev/null 2>&1; then
        gosu "$TARGET_UID:$TARGET_GID" "$@"
    else
        su -s /bin/sh "$APP_USER" -c 'exec "$@"' sh "$@"
    fi
}

access_of() {  # path -> "read+write" / "read" / "NONE"
    local p="$1" out=""
    out="$(as_app /bin/sh -c '
        p="$1"; out=""
        if [ -r "$p" ] && [ -x "$p" ]; then out="read"; fi
        if [ -w "$p" ]; then out="${out:+$out+}write"; fi
        printf "%s" "${out:-NONE}"
    ' sh "$p" 2>/dev/null)" || out="unknown"
    printf '%s' "${out:-unknown}"
}

data_acc="$(access_of "$DATA_DIR")"
log "running as uid=$TARGET_UID gid=$TARGET_GID groups=$SUP_GROUPS; $DATA_DIR -> ${data_acc}"
if [ -n "$MEDIA_ROOT" ]; then
    media_acc="$(access_of "$MEDIA_ROOT")"
    log "$MEDIA_ROOT -> ${media_acc}"
    if [ "$media_acc" = "NONE" ] || [ "$media_acc" = "unknown" ]; then
        warn "the app user cannot read $MEDIA_ROOT"
        warn "fix: set PUID/PGID in docker-compose.yml to the owner of that mount"
        warn "     (on the host:  stat -c '%u:%g' <media dir>   or   id <your user>)"
    fi
fi

# ---------------------------------------------------------------------------
# 6) exec the CMD as the app user - it stays PID 1 (signals, `docker stop`)
# ---------------------------------------------------------------------------
export HOME="$APP_HOME" USER="$APP_USER" LOGNAME="$APP_USER"
[ "$#" -gt 0 ] || set -- python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${SPM_PORT:-8880}"
log "exec: $*"
if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid "$TARGET_UID" --regid "$TARGET_GID" \
                 --groups "$SUP_GROUPS" "$@"
elif command -v gosu >/dev/null 2>&1; then
    exec gosu "$TARGET_UID:$TARGET_GID" "$@"
else
    exec su -s /bin/sh "$APP_USER" -c 'exec "$@"' sh "$@"
fi
