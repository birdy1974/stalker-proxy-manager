"""
E3: put the generated bouquets ON the receiver, over FTP, and reload it.

Why FTP and not something nicer: OpenWebif has no file-upload API (it can list,
zap and reload, but never write `/etc/enigma2`), and an OpenPLi image's SSH is
dropbear *without* an sftp-server, so `scp`/`sftp` are a coin flip. What every
stock image does have is vsftpd with the root account enabled - which is also
why the login defaults to `root`: the bouquet directory is root-owned and a
normal user cannot write it.

Safety rules encoded here, because a half-written `bouquets.tv` is a receiver
that boots into an empty channel list:

* **never write a file in place.** Every upload goes to a temporary name *in
  the same directory* and is then RNFR/RNTO'd over the target. Rename inside
  one directory is atomic; the box either sees the old file or the new one.
  (Uploading to `/tmp` first and renaming across would NOT work: `/tmp` is a
  tmpfs and `/etc/enigma2` is flash, and `rename(2)` cannot cross filesystems -
  the server answers 550 and you are left with the file in the wrong place.)
* **always keep one restore point.** The box's own `bouquets.tv` is copied to
  `bouquets.tv.spm-backup` before we touch it, every push, overwriting the
  previous one. One is deliberate: it is the last known-good state, and a pile
  of dated backups on a 512 MB flash is its own kind of failure. `restore()`
  puts it back and reloads.
* **only our own files are ever deleted.** `userbouquet.<prefix>_*.tv` and
  nothing else; foreign bouquets (satellite, favourites, other tools) are
  preserved by `merge_bouquets_tv`.
* **dry run first.** Every push can be planned without a single write, and the
  GUI shows what would happen.

The reload is OpenWebif's `GET /api/servicelistreload?mode=2` - mode 2 is
"bouquets only", which is all we change; mode 0/1 would also re-read `lamedb`
and drop the tuner's cached services for no reason.
"""

from __future__ import annotations

import asyncio
import ftplib
import io
from dataclasses import dataclass, field

from .enigma2_bouquets import (BOUQUETS_TV, Bundle, E2_DIR, bouquets_add_file,
                               merge_bouquets_tv, slugify)

# how the OpenWebif call authenticates - selectable per profile, because an
# out-of-the-box OpenPLi answers without any auth while a box whose web
# interface was locked down needs the same user/password the browser uses
OWIF_AUTH = ("none", "basic")
RELOAD_MODE = 2                      # bouquets only (see module docstring)
BACKUP_SUFFIX = ".spm-backup"
TMP_PREFIX = ".spm-upload-"
FTP_TIMEOUT = 20                     # a receiver on the LAN answers in ms
HTTP_TIMEOUT = 20


@dataclass
class Step:
    """One line of the push log, as shown in the GUI."""
    name: str
    ok: bool = True
    detail: str = ""


@dataclass
class Report:
    ok: bool = False
    dry_run: bool = False
    steps: list[Step] = field(default_factory=list)
    error: str = ""
    uploaded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reloaded: bool = False

    def step(self, name: str, ok: bool = True, detail: str = "") -> None:
        self.steps.append(Step(name, ok, detail))

    def as_dict(self) -> dict:
        return {"ok": self.ok, "dry_run": self.dry_run, "error": self.error,
                "uploaded": self.uploaded, "removed": self.removed,
                "reloaded": self.reloaded,
                "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail}
                          for s in self.steps]}

    def text(self) -> str:
        """Compact one-per-line log, stored on the profile as last_push_result."""
        out = [f"{'ok  ' if s.ok else 'FAIL'} {s.name}"
               + (f" - {s.detail}" if s.detail else "") for s in self.steps]
        if self.error:
            out.append(f"FAIL {self.error}")
        return "\n".join(out)


class PushError(RuntimeError):
    """A push step failed; the message is meant to be read by a human."""


# --------------------------------------------------------------------------- #
# the FTP side (blocking; every public function runs it in a worker thread)
# --------------------------------------------------------------------------- #
class _Ftp:
    """Thin, deliberately boring wrapper over `ftplib.FTP`.

    Only the handful of commands a stock vsftpd on a set-top box is guaranteed
    to support: LIST/NLST, RETR, STOR, RNFR/RNTO, DELE. No MLSD, no FEAT
    negotiation, no TLS - the box sits on the LAN and its own web interface is
    plain HTTP anyway.
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 directory: str = E2_DIR, timeout: int = FTP_TIMEOUT) -> None:
        self.host, self.port = host, int(port or 21)
        self.user, self.password = user or "root", password or ""
        self.directory = directory or E2_DIR
        self.timeout = timeout
        self.ftp: ftplib.FTP | None = None
        self.welcome = ""

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "_Ftp":
        ftp = ftplib.FTP()
        ftp.timeout = self.timeout
        try:
            ftp.connect(self.host, self.port, timeout=self.timeout)
            self.welcome = (ftp.getwelcome() or "").strip()
            ftp.login(self.user, self.password)
            ftp.set_pasv(True)          # NAT/container friendly, and the default
        except (*ftplib.all_errors, OSError) as exc:   # all_errors is a tuple
            raise PushError(f"FTP {self.host}:{self.port} - {exc}") from exc
        self.ftp = ftp
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            if self.ftp is not None:
                self.ftp.quit()
        except Exception:               # a box that hangs up rudely is fine
            try:
                self.ftp.close()        # type: ignore[union-attr]
            except Exception:
                pass

    # -- commands ----------------------------------------------------------
    def cwd(self) -> None:
        try:
            self.ftp.cwd(self.directory)            # type: ignore[union-attr]
        except ftplib.all_errors as exc:
            raise PushError(f"cannot enter {self.directory} on the receiver "
                            f"({exc}) - is this really an Enigma2 box?") from exc

    def listdir(self) -> list[str]:
        try:
            names = self.ftp.nlst()                 # type: ignore[union-attr]
        except ftplib.error_perm as exc:            # empty dir answers 550 on some servers
            if str(exc).startswith("550"):
                return []
            raise PushError(f"cannot list {self.directory} ({exc})") from exc
        except ftplib.all_errors as exc:
            raise PushError(f"cannot list {self.directory} ({exc})") from exc
        # some servers answer with full paths
        return [n.rsplit("/", 1)[-1] for n in names]

    def read(self, name: str) -> str | None:
        """File contents, or None when it does not exist."""
        buf = io.BytesIO()
        try:
            self.ftp.retrbinary(f"RETR {name}", buf.write)   # type: ignore[union-attr]
        except ftplib.error_perm:
            return None
        except ftplib.all_errors as exc:
            raise PushError(f"cannot read {name} ({exc})") from exc
        return buf.getvalue().decode("utf-8", "replace")

    def _stor(self, name: str, data: bytes) -> None:
        try:
            self.ftp.storbinary(f"STOR {name}", io.BytesIO(data))  # type: ignore[union-attr]
        except ftplib.all_errors as exc:
            raise PushError(f"cannot upload {name} ({exc})") from exc

    def delete(self, name: str) -> None:
        try:
            self.ftp.delete(name)                    # type: ignore[union-attr]
        except ftplib.all_errors as exc:
            raise PushError(f"cannot delete {name} ({exc})") from exc

    def write_atomic(self, name: str, text: str) -> None:
        """Upload beside the target, then rename over it.

        The rename is what makes this safe: enigma2 may read the directory at
        any moment (a zap, a plugin, the web interface), and a partially
        transferred `bouquets.tv` is a box with no channels.
        """
        tmp = f"{TMP_PREFIX}{name}"
        self._stor(tmp, text.encode("utf-8"))
        try:
            self.ftp.rename(tmp, name)               # type: ignore[union-attr]
        except ftplib.all_errors:
            # servers that refuse RNTO onto an existing name: remove, retry,
            # and if even that fails leave no temp file behind
            try:
                self.ftp.delete(name)                # type: ignore[union-attr]
                self.ftp.rename(tmp, name)           # type: ignore[union-attr]
            except ftplib.all_errors as exc:
                try:
                    self.ftp.delete(tmp)             # type: ignore[union-attr]
                except Exception:
                    pass
                raise PushError(f"cannot move {tmp} onto {name} ({exc})") from exc

    def copy(self, src: str, dst: str) -> bool:
        """Server-side copy is not an FTP command - read it, write it back."""
        text = self.read(src)
        if text is None:
            return False
        self._stor(dst, text.encode("utf-8"))
        return True


# --------------------------------------------------------------------------- #
# OpenWebif
# --------------------------------------------------------------------------- #
def _owif_url(profile, path: str) -> str:
    scheme = "https" if getattr(profile, "use_https", False) else "http"
    port = int(getattr(profile, "web_port", 80) or 80)
    hostpart = profile.host if port in (80, 443) else f"{profile.host}:{port}"
    return f"{scheme}://{hostpart}{path}"


def _owif_auth(profile):
    if (getattr(profile, "owif_auth", "none") or "none") != "basic":
        return None
    return (profile.owif_user or "root", profile.owif_pass or "")


async def _owif_get(profile, path: str) -> tuple[bool, str]:
    """(ok, detail) - never raises: a failed reload is a warning, not a loss."""
    from .http_client import outbound_client
    import httpx

    url = _owif_url(profile, path)
    try:
        async with outbound_client(timeout=httpx.Timeout(HTTP_TIMEOUT, connect=8)) as c:
            r = await c.get(url, auth=_owif_auth(profile))
        if r.status_code == 401:
            return False, ("OpenWebif answered 401 - set the web interface "
                           "authentication to 'basic' and fill user/password")
        if r.status_code >= 400:
            return False, f"OpenWebif answered HTTP {r.status_code}"
        return True, (r.text or "").strip()[:200]
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        return False, f"{type(exc).__name__}: {exc}"


async def reload_services(profile, mode: int = RELOAD_MODE) -> tuple[bool, str]:
    """Ask the box to re-read its bouquets (mode 2 = bouquets only)."""
    return await _owif_get(profile, f"/api/servicelistreload?mode={mode}")


# --------------------------------------------------------------------------- #
# the operations the API exposes
# --------------------------------------------------------------------------- #
def _require_ftp(profile) -> None:
    if (getattr(profile, "transport", "download") or "download") != "ftp":
        raise PushError("this profile's transport is not FTP - switch it to "
                        "'ftp' (or use the installer one-liner on the box)")
    if not (profile.host or "").strip():
        raise PushError("no host/IP set for this receiver")


def _connect(profile) -> _Ftp:
    return _Ftp(profile.host.strip(), profile.ftp_port, profile.login,
                profile.password or "")


async def test_connection(profile) -> Report:
    """Log in, look at `/etc/enigma2`, and ping OpenWebif - writing nothing."""
    rep = Report(dry_run=True)
    try:
        _require_ftp(profile)
    except PushError as exc:
        rep.error = str(exc)
        return rep

    def _probe() -> tuple[str, list[str], bool]:
        with _connect(profile) as ftp:
            ftp.cwd()
            names = ftp.listdir()
            return ftp.welcome, names, BOUQUETS_TV in names

    try:
        welcome, names, has_tv = await asyncio.to_thread(_probe)
    except PushError as exc:
        rep.step("FTP login", False, str(exc))
        rep.error = str(exc)
        return rep

    rep.step("FTP login", True, welcome or f"{profile.host}:{profile.ftp_port}")
    ours = [n for n in names if n.startswith(f"userbouquet.{slugify(profile.bouquet_prefix)}_")]
    rep.step(f"{E2_DIR} readable", True,
             f"{len(names)} files, {len(ours)} from a previous SPM push")
    rep.step(f"{BOUQUETS_TV} present", has_tv,
             "" if has_tv else "not found - the push will create it")
    ok, detail = await _owif_get(profile, "/api/about")
    rep.step("OpenWebif reachable", ok, detail)
    rep.ok = True                      # FTP works; OpenWebif is only the reload
    return rep


async def push_bundle(profile, bundle: Bundle, *, dry_run: bool = False) -> Report:
    """Upload the rendered bouquets and reload the box.

    Order matters: back up, upload the new bouquets, drop the stale ones, and
    only then rewrite `bouquets.tv`. At every intermediate moment the index
    still points at files that exist.
    """
    rep = Report(dry_run=dry_run)
    try:
        _require_ftp(profile)
    except PushError as exc:
        rep.error = str(exc)
        return rep
    if not bundle.files:
        rep.error = ("nothing to push: this profile renders no services "
                     "(check its content types and group filters)")
        return rep

    prefix = slugify(profile.bouquet_prefix)
    wanted = {f.name: f.text for f in bundle.files}
    index = bouquets_add_file(list(wanted))

    def _run() -> Report:
        with _connect(profile) as ftp:
            ftp.cwd()
            names = ftp.listdir()
            existing_tv = ftp.read(BOUQUETS_TV)
            stale = sorted(n for n in names
                           if n.startswith(f"userbouquet.{prefix}_")
                           and n.endswith(".tv") and n not in wanted)
            merged = merge_bouquets_tv(existing_tv or "", list(wanted), prefix)

            if dry_run:
                rep.step("connect", True, f"{profile.host}:{profile.ftp_port} as {profile.login}")
                rep.step("would back up", True,
                         f"{BOUQUETS_TV} -> {BOUQUETS_TV}{BACKUP_SUFFIX}"
                         if existing_tv is not None else f"no {BOUQUETS_TV} yet")
                rep.step("would upload", True, f"{len(wanted)} bouquet file(s)")
                rep.step("would remove", True,
                         ", ".join(stale) if stale else "nothing (no stale SPM bouquets)")
                rep.step("would rewrite", True,
                         f"{BOUQUETS_TV} ({'changed' if merged != (existing_tv or '') else 'unchanged'})")
                rep.step("would reload", True, f"/api/servicelistreload?mode={RELOAD_MODE}")
                rep.uploaded, rep.removed, rep.ok = sorted(wanted), stale, True
                return rep

            if existing_tv is not None:
                ftp.write_atomic(f"{BOUQUETS_TV}{BACKUP_SUFFIX}", existing_tv)
                rep.step("backup", True, f"{BOUQUETS_TV}{BACKUP_SUFFIX} (restore point)")
            else:
                rep.step("backup", True, f"no {BOUQUETS_TV} on the box yet - nothing to save")

            for name in sorted(wanted):
                ftp.write_atomic(name, wanted[name])
                rep.uploaded.append(name)
            rep.step("upload", True, f"{len(rep.uploaded)} bouquet file(s)")

            for name in stale:
                ftp.delete(name)
                rep.removed.append(name)
            if stale:
                rep.step("remove stale", True, ", ".join(stale))

            ftp.write_atomic(BOUQUETS_TV, merged)
            rep.step(BOUQUETS_TV, True, "merged (foreign bouquets kept)")
            # a copy of the index, so the box can be repaired by hand
            ftp.write_atomic("bouquets.spm.add", index)
            rep.ok = True
            return rep

    try:
        rep = await asyncio.to_thread(_run)
    except PushError as exc:
        rep.step("push", False, str(exc))
        rep.error = str(exc)
        return rep

    if dry_run:
        return rep
    ok, detail = await reload_services(profile)
    rep.reloaded = ok
    rep.step("reload service list", ok,
             detail if ok else f"{detail} - reload the bouquets from the box menu")
    return rep


async def restore(profile) -> Report:
    """Put the backed-up `bouquets.tv` back and remove our bouquets.

    The escape hatch for "the box now shows a channel list I did not want":
    the restore point is the file as it was before the last push.
    """
    rep = Report()
    try:
        _require_ftp(profile)
    except PushError as exc:
        rep.error = str(exc)
        return rep
    prefix = slugify(profile.bouquet_prefix)

    def _run() -> Report:
        with _connect(profile) as ftp:
            ftp.cwd()
            backup = ftp.read(f"{BOUQUETS_TV}{BACKUP_SUFFIX}")
            if backup is None:
                raise PushError(f"no restore point on the box "
                                f"({BOUQUETS_TV}{BACKUP_SUFFIX} not found)")
            # The restore point is the file as it was before the LAST push - so
            # after a second push it already lists the bouquets of the first
            # one, which we are about to delete. Writing it back verbatim would
            # leave enigma2 with entries pointing at files that no longer
            # exist (empty ghost bouquets in the list), so our own lines are
            # stripped here; everything foreign is kept exactly as it was.
            ftp.write_atomic(BOUQUETS_TV, merge_bouquets_tv(backup, [], prefix))
            rep.step("restore", True, f"{BOUQUETS_TV} from {BACKUP_SUFFIX[1:]}")
            for name in sorted(n for n in ftp.listdir()
                               if n.startswith(f"userbouquet.{prefix}_") and n.endswith(".tv")):
                ftp.delete(name)
                rep.removed.append(name)
            rep.step("remove SPM bouquets", True,
                     ", ".join(rep.removed) if rep.removed else "none were present")
            rep.ok = True
            return rep

    try:
        rep = await asyncio.to_thread(_run)
    except PushError as exc:
        rep.step("restore", False, str(exc))
        rep.error = str(exc)
        return rep
    ok, detail = await reload_services(profile)
    rep.reloaded = ok
    rep.step("reload service list", ok, detail)
    return rep
