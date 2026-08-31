"""
Pushing bouquets to a real receiver (E3).

The box is an appliance with 512 MB of flash and no undo: if a push writes half
a `bouquets.tv`, the living room TV boots into an empty channel list. Every
rule below exists for that reason and is pinned here against a fake FTP server
that behaves like the vsftpd an OpenPLi image ships:

  * nothing is ever written in place - upload beside the target, then rename;
  * the box's own `bouquets.tv` becomes a restore point before we touch it;
  * only `userbouquet.<prefix>_*.tv` may be deleted - a satellite bouquet a
    user spent an evening sorting is not ours to remove;
  * a dry run writes nothing at all.
"""

from __future__ import annotations

import ftplib
import io

import pytest

from app.models import Enigma2Profile
from app.services import enigma2_bouquets as e2
from app.services import enigma2_push as push


# --------------------------------------------------------------------------- #
# a fake vsftpd: in-memory files, the same command surface ftplib uses
# --------------------------------------------------------------------------- #
class FakeFTP:
    instances: list["FakeFTP"] = []

    # what the test wants the server to do
    files: dict[str, str] = {}
    fail_rename_onto_existing = False
    no_such_dir = False

    def __init__(self) -> None:
        self.cwd_path = "/"
        self.log: list[str] = []
        self.closed = False
        self.timeout = None
        FakeFTP.instances.append(self)

    # -- session
    def connect(self, host, port=21, timeout=None):
        self.host, self.port = host, port
        self.log.append(f"connect {host}:{port}")

    def getwelcome(self):
        return "220 Welcome to the OpenPLi FTP service"

    def login(self, user="", passwd=""):
        if (user, passwd) != ("root", "boxpw"):
            raise ftplib.error_perm("530 Login incorrect.")
        self.log.append(f"login {user}")

    def set_pasv(self, on):
        pass

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True

    # -- commands
    def cwd(self, path):
        if FakeFTP.no_such_dir:
            raise ftplib.error_perm("550 Failed to change directory.")
        self.cwd_path = path
        self.log.append(f"cwd {path}")

    def nlst(self, *args):
        return sorted(FakeFTP.files)

    def retrbinary(self, cmd, callback, blocksize=8192):
        name = cmd.split(" ", 1)[1]
        if name not in FakeFTP.files:
            raise ftplib.error_perm("550 Failed to open file.")
        callback(FakeFTP.files[name].encode())

    def storbinary(self, cmd, fp, blocksize=8192):
        name = cmd.split(" ", 1)[1]
        FakeFTP.files[name] = fp.read().decode()
        self.log.append(f"stor {name}")

    def rename(self, src, dst):
        if src not in FakeFTP.files:
            raise ftplib.error_perm("550 RNFR: no such file.")
        if dst in FakeFTP.files and FakeFTP.fail_rename_onto_existing:
            raise ftplib.error_perm("550 RNTO: file exists.")
        FakeFTP.files[dst] = FakeFTP.files.pop(src)
        self.log.append(f"rename {src} -> {dst}")

    def delete(self, name):
        if name not in FakeFTP.files:
            raise ftplib.error_perm("550 Delete operation failed.")
        del FakeFTP.files[name]
        self.log.append(f"dele {name}")


FOREIGN_TV = (
    "#NAME Bouquets (TV)\n"
    '#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.favourites.tv" ORDER BY bouquet\n'
    '#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.astra19.tv" ORDER BY bouquet\n'
)


@pytest.fixture(autouse=True)
def fake_box(monkeypatch):
    """A receiver with two bouquets of its own and nothing from SPM yet."""
    FakeFTP.instances = []
    FakeFTP.fail_rename_onto_existing = False
    FakeFTP.no_such_dir = False
    FakeFTP.files = {
        "bouquets.tv": FOREIGN_TV,
        "userbouquet.favourites.tv": "#NAME Favourites\n",
        "userbouquet.astra19.tv": "#NAME Astra\n",
        "lamedb": "eDVB services /5/\n",
    }
    monkeypatch.setattr(push.ftplib, "FTP", FakeFTP)
    # OpenWebif lives in its own test; here it always answers
    async def _ok(profile, path):
        _ok.calls.append(path)
        return True, "OK"
    _ok.calls = []
    monkeypatch.setattr(push, "_owif_get", _ok)
    return _ok


def _profile(**kw) -> Enigma2Profile:
    base = dict(name="Duo2", token="tok", host="192.168.1.50", ftp_port=21,
                login="root", password="boxpw", transport="ftp",
                bouquet_prefix="spm", web_port=80, owif_auth="none")
    return Enigma2Profile(**(base | kw))


def _bundle(*names: str) -> e2.Bundle:
    b = e2.Bundle()
    for i, n in enumerate(names or ("live_news", "vod_action"), 1):
        b.files.append(e2.BouquetFile(
            name=f"userbouquet.spm_{n}.tv", title=f"SPM - {n}", services=i,
            lines=[f"#SERVICE 5002:0:1:{i}:0:0:0:0:0:0:http%3a//nas/x:{n}",
                   f"#DESCRIPTION {n}"]))
    return b


# --------------------------------------------------------------------------- #
# the push
# --------------------------------------------------------------------------- #
async def test_push_uploads_the_bouquets_and_merges_the_index():
    rep = await push.push_bundle(_profile(), _bundle())
    assert rep.ok and not rep.error
    assert FakeFTP.files["userbouquet.spm_live_news.tv"].startswith("#NAME SPM - live_news")
    tv = FakeFTP.files["bouquets.tv"]
    # our two bouquets are in, and the user's own two are still there
    assert 'userbouquet.spm_vod_action.tv" ORDER BY bouquet' in tv
    assert "userbouquet.favourites.tv" in tv and "userbouquet.astra19.tv" in tv
    assert rep.reloaded


async def test_the_box_gets_exactly_one_restore_point_before_anything_changes():
    """The backup is the file as it was BEFORE the push - that is the whole
    point of it - and a second push overwrites it instead of piling up."""
    prof = _profile()
    await push.push_bundle(prof, _bundle())
    assert FakeFTP.files["bouquets.tv.spm-backup"] == FOREIGN_TV
    first = FakeFTP.files["bouquets.tv"]
    await push.push_bundle(prof, _bundle())
    assert FakeFTP.files["bouquets.tv.spm-backup"] == first
    assert [n for n in FakeFTP.files if ".spm-backup" in n] == ["bouquets.tv.spm-backup"]


async def test_every_write_lands_through_a_rename_and_leaves_no_temp_file():
    """enigma2 may read /etc/enigma2 at any moment: a partially transferred
    bouquets.tv is a box with no channels. So the bytes go to a temp name in
    the same directory and are renamed over the target, which is atomic."""
    ftp_log: list[str] = []
    rep = await push.push_bundle(_profile(), _bundle())
    ftp_log = FakeFTP.instances[0].log
    stored = [l.split()[1] for l in ftp_log if l.startswith("stor ")]
    assert stored and all(n.startswith(push.TMP_PREFIX) for n in stored)
    assert all(f"rename {n} -> {n[len(push.TMP_PREFIX):]}" in ftp_log for n in stored)
    assert not [n for n in FakeFTP.files if n.startswith(push.TMP_PREFIX)]
    assert rep.ok


async def test_a_server_that_refuses_to_rename_onto_an_existing_file_still_works():
    """Some FTP servers answer 550 to RNTO when the target exists; the fallback
    is delete-then-rename, and it must not leave the temp file behind."""
    FakeFTP.fail_rename_onto_existing = True
    await push.push_bundle(_profile(), _bundle())
    assert FakeFTP.files["bouquets.tv"].count("userbouquet.spm_") == 2
    assert not [n for n in FakeFTP.files if n.startswith(push.TMP_PREFIX)]


async def test_only_our_own_bouquets_are_removed_when_the_playlist_shrinks():
    await push.push_bundle(_profile(), _bundle("live_news", "vod_action"))
    rep = await push.push_bundle(_profile(), _bundle("live_news"))
    assert rep.removed == ["userbouquet.spm_vod_action.tv"]
    assert "userbouquet.spm_vod_action.tv" not in FakeFTP.files
    # the user's own bouquets and lamedb are untouched
    assert "userbouquet.favourites.tv" in FakeFTP.files
    assert "userbouquet.astra19.tv" in FakeFTP.files
    assert "lamedb" in FakeFTP.files


async def test_a_dry_run_writes_absolutely_nothing():
    before = dict(FakeFTP.files)
    rep = await push.push_bundle(_profile(), _bundle(), dry_run=True)
    assert rep.ok and rep.dry_run
    assert FakeFTP.files == before
    assert not any(l.startswith(("stor", "dele", "rename")) for l in FakeFTP.instances[0].log)
    assert any("would upload" in s.name for s in rep.steps)


async def test_a_box_without_a_bouquets_tv_is_created_not_crashed():
    del FakeFTP.files["bouquets.tv"]
    rep = await push.push_bundle(_profile(), _bundle())
    assert rep.ok
    assert FakeFTP.files["bouquets.tv"].startswith("#NAME Bouquets (TV)")
    assert "bouquets.tv.spm-backup" not in FakeFTP.files


async def test_push_refuses_when_the_transport_is_not_ftp_or_the_host_is_missing():
    rep = await push.push_bundle(_profile(transport="download"), _bundle())
    assert not rep.ok and "transport" in rep.error
    rep = await push.push_bundle(_profile(host=""), _bundle())
    assert not rep.ok and "host" in rep.error
    assert not FakeFTP.instances          # nothing was even dialled


async def test_an_empty_bundle_never_wipes_the_receiver():
    """A profile that renders nothing (all content types off, or a group filter
    that matches none) must not be turned into 'delete all SPM bouquets'."""
    rep = await push.push_bundle(_profile(), e2.Bundle())
    assert not rep.ok and "nothing to push" in rep.error
    assert FakeFTP.files["bouquets.tv"] == FOREIGN_TV


async def test_a_wrong_password_is_reported_as_a_readable_failure():
    rep = await push.push_bundle(_profile(password="nope"), _bundle())
    assert not rep.ok
    assert "530" in rep.error and "192.168.1.50:21" in rep.error
    assert rep.steps[-1].ok is False


async def test_a_box_that_is_not_enigma2_fails_before_writing():
    FakeFTP.no_such_dir = True
    before = dict(FakeFTP.files)
    rep = await push.push_bundle(_profile(), _bundle())
    assert not rep.ok and "/etc/enigma2" in rep.error
    assert FakeFTP.files == before


# --------------------------------------------------------------------------- #
# reload, test-connection, restore
# --------------------------------------------------------------------------- #
async def test_the_reload_is_bouquets_only(fake_box):
    await push.push_bundle(_profile(), _bundle())
    assert fake_box.calls == ["/api/servicelistreload?mode=2"]


async def test_a_failed_reload_is_a_warning_not_a_lost_push(monkeypatch):
    async def _down(profile, path):
        return False, "ConnectError: connection refused"
    monkeypatch.setattr(push, "_owif_get", _down)
    rep = await push.push_bundle(_profile(), _bundle())
    assert rep.ok and not rep.reloaded          # the files ARE on the box
    assert "box menu" in rep.steps[-1].detail   # ...and the user is told how to finish


async def test_test_connection_reports_the_box_without_writing():
    before = dict(FakeFTP.files)
    rep = await push.test_connection(_profile())
    assert rep.ok and FakeFTP.files == before
    names = [s.name for s in rep.steps]
    assert "FTP login" in names and "OpenWebif reachable" in names
    assert any("0 from a previous SPM push" in s.detail for s in rep.steps)


async def test_restore_puts_the_backup_back_and_drops_our_bouquets():
    prof = _profile()
    await push.push_bundle(prof, _bundle())
    rep = await push.restore(prof)
    assert rep.ok
    assert FakeFTP.files["bouquets.tv"] == FOREIGN_TV
    assert not [n for n in FakeFTP.files if n.startswith("userbouquet.spm_")]
    assert "userbouquet.favourites.tv" in FakeFTP.files


async def test_restore_after_a_second_push_leaves_no_dangling_entries():
    """The restore point is the state before the LAST push, so after two
    pushes it already lists our bouquets - and restore deletes those files.
    Writing it back verbatim would leave enigma2 showing bouquets whose files
    are gone."""
    prof = _profile()
    await push.push_bundle(prof, _bundle())
    await push.push_bundle(prof, _bundle())
    assert "userbouquet.spm_" in FakeFTP.files["bouquets.tv.spm-backup"]   # precondition
    await push.restore(prof)
    tv = FakeFTP.files["bouquets.tv"]
    assert "userbouquet.spm_" not in tv
    assert "userbouquet.favourites.tv" in tv and "userbouquet.astra19.tv" in tv


async def test_restore_without_a_restore_point_says_so():
    rep = await push.restore(_profile())
    assert not rep.ok and "no restore point" in rep.error


# --------------------------------------------------------------------------- #
# OpenWebif addressing and authentication (user selectable)
# --------------------------------------------------------------------------- #
def test_the_openwebif_url_follows_port_and_scheme():
    assert push._owif_url(_profile(), "/api/about") == "http://192.168.1.50/api/about"
    assert push._owif_url(_profile(web_port=8080), "/x") == "http://192.168.1.50:8080/x"
    assert push._owif_url(_profile(use_https=True, web_port=443), "/x") \
        == "https://192.168.1.50/x"


def test_basic_auth_is_only_sent_when_the_profile_asks_for_it():
    """A stock OpenPLi answers without credentials; sending them anyway is
    harmless but hides the real problem when a box IS locked down, so the
    choice is explicit."""
    assert push._owif_auth(_profile()) is None
    assert push._owif_auth(_profile(owif_auth="basic", owif_user="admin",
                                    owif_pass="s3cret")) == ("admin", "s3cret")
    # basic with an empty user falls back to root, the OpenPLi default account
    assert push._owif_auth(_profile(owif_auth="basic"))[0] == "root"


# --------------------------------------------------------------------------- #
# the API around it
# --------------------------------------------------------------------------- #
async def _client():
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _stored_profile(**kw) -> int:
    """A profile plus two live channels, so a push has something to render."""
    from app.database import SessionLocal
    from app.models import LivePlaylist

    async with SessionLocal() as s:
        s.add_all([
            LivePlaylist(custom_name="News One", group_name="News", enabled=True, order=1),
            LivePlaylist(custom_name="Sport One", group_name="Sport", enabled=True, order=2),
        ])
        p = _profile(**kw)
        s.add(p)
        await s.commit()
        return p.id


async def test_the_test_endpoint_returns_the_step_list():
    pid = await _stored_profile()
    async with await _client() as c:
        r = await c.post(f"/api/enigma2/profiles/{pid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["dry_run"]
    assert [s["name"] for s in body["steps"]][0] == "FTP login"


async def test_a_dry_run_over_the_api_does_not_stamp_the_profile():
    """last_push_at is 'when did this box last receive files' - a dry run did
    not send any, so claiming it did would be a lie in the profile list."""
    pid = await _stored_profile()
    async with await _client() as c:
        dry = (await c.post(f"/api/enigma2/profiles/{pid}/push?dry_run=1")).json()
        row = (await c.get("/api/enigma2/profiles")).json()["items"][0]
    assert dry["dry_run"] and dry["ok"] and row["last_push_at"] is None
    assert FakeFTP.files["bouquets.tv"] == FOREIGN_TV


async def test_a_failed_push_is_recorded_on_the_profile():
    pid = await _stored_profile(password="nope")
    async with await _client() as c:
        body = (await c.post(f"/api/enigma2/profiles/{pid}/push")).json()
        row = (await c.get("/api/enigma2/profiles")).json()["items"][0]
    assert not body["ok"]
    assert row["last_push_at"] and "530" in row["last_push_result"]


async def test_openwebif_auth_is_validated_and_its_password_is_never_returned():
    pid = await _stored_profile()
    async with await _client() as c:
        bad = await c.put(f"/api/enigma2/profiles/{pid}", json={"owif_auth": "digest"})
        ok = await c.put(f"/api/enigma2/profiles/{pid}",
                         json={"owif_auth": "basic", "owif_user": "admin",
                               "owif_pass": "s3cret"})
    assert bad.status_code == 400
    item = ok.json()["item"]
    assert item["owif_auth"] == "basic" and item["owif_user"] == "admin"
    assert item["owif_pass"] == "" and item["has_owif_pass"] is True
