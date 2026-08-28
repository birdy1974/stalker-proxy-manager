"""
Filesystem permission helpers for bind-mounted directories (PUID / PGID).

A host folder bind-mounted into the container keeps its HOST ownership. When
the app user (image default uid/gid 2000) is not the owner, reading it fails
with a bare

    PermissionError: [Errno 13] Permission denied: '/media'

That is a deployment problem - fixed with `PUID` / `PGID` in
docker-compose.yml (see README) - not an application bug, so the API reports
it as such (clean 403 with a hint) instead of a 500 traceback.
"""

from __future__ import annotations

import os
from pathlib import Path


def current_ids() -> str:
    """'uid=1000 gid=1000' of the running process (best effort)."""
    try:
        return f"uid={os.getuid()} gid={os.getgid()}"
    except (AttributeError, OSError):     # non-POSIX (Windows dev shell)
        return "uid=? gid=?"


def permission_hint(path) -> str:
    """Actionable message for a PermissionError / access problem on `path`."""
    return (f"permission denied: {path} - the app runs as {current_ids()}; "
            f"set PUID/PGID in docker-compose.yml (or .env) to the owner of "
            f"that mount and re-create the container "
            f"(on the host: stat -c '%u:%g' <dir>, or: id <your-user>)")


def describe_access(path) -> str:
    """'read+write' / 'read' / 'none' as seen by this process (for logs)."""
    out = []
    try:
        if os.access(path, os.R_OK):
            out.append("read")
        if os.access(path, os.W_OK):
            out.append("write")
    except OSError:
        return "unknown"
    return "+".join(out) or "none"


def is_readable_dir(path) -> bool:
    """True when `path` is a directory we may actually list."""
    try:
        return Path(path).is_dir() and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False
