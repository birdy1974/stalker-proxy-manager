"""
S-A + S-B: what a `create_link` ANSWER looks like, and the two cmd FORMS of one
VOD file.

Both are reading problems, not policy problems, which is why they are easy to
miss and expensive once missed:

* A panel that has to choose a storage - or that inserts an advertisement -
  answers `create_link` with a LIST of candidates. A client that only reads the
  dict shape reports "the portal returned no playable URL" for every item that
  panel serves, and the log reads like a dead channel rather than an unparsed
  answer. So the reader knows every shape, it skips what the panel labelled an
  ad, and it can *describe* what it got (`links.read_link_answer`).
* Some panels list a movie as `/media/1234.mpg` and answer `create_link` only
  for `/media/file_<id>.mpg`, where `<id>` is what `get_ordered_list&movie_id=`
  reports. The refusal they send for the catalogue form is `nothing_to_play` -
  byte-for-byte what a dead item looks like. The rewrite is a REPAIR: tried only
  after a refusal that can mean "wrong form", never pre-emptively, and the form
  that worked is stored on the row so the next play does not pay for the refusal
  and the resolution again.

The witnesses matter more than the return values: `media_refusals` proves the
panel was asked with the wrong form once, `media_resolutions` proves we asked it
which file was behind the movie, and both staying at zero on the SECOND play is
the only evidence that learning anything was worth the column.
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import (LiveSource, MacAddress, Portal, VodPlaylist,
                        VodPlaylistSource, VodSource)
from app.portal import client as client_mod
from app.portal.client import PortalError
from app.portal.links import (CmdRepair, generic_media_ref, media_file_form,
                              plan_for, read_link_answer)
from app.portal.mock_portal import _STATE
from app.services.db_logging import flush_logs
from app.services.stream_manager import MANAGER, _store_media_cmd
from mockclient import GOOD, PORTAL, Wired

CLEAN = "http://test/mock/ts/1002.ts"
MEDIA = "/media/2001.mpg"
FILE_ID = "9001"

#: everything a test in here may dirty, saved and restored around each one - the
#: mock's state is module-global, so a knob left on is a failure in somebody
#: else's test file
_KNOBS = ("create_link_list", "media_form", "media_file_id", "create_link_error")
_COUNTERS = ("create_links", "media_refusals", "media_resolutions")


@pytest.fixture(autouse=True)
def _fresh_mock():
    saved = {k: _STATE.get(k) for k in _KNOBS + _COUNTERS + ("create_link_seen",)}
    _STATE.update({"create_link_list": "", "media_form": "", "media_file_id": FILE_ID,
                   "create_link_error": "", "create_links": 0, "media_refusals": 0,
                   "media_resolutions": 0, "create_link_seen": {}})
    yield
    for k, v in saved.items():
        _STATE[k] = v


# =========================================================================== #
# S-A: reading the answer
# =========================================================================== #
@pytest.mark.parametrize("js,want", [
    ({"cmd": "ffmpeg http://cdn/x.ts"}, "ffmpeg http://cdn/x.ts"),
    ({"url": "http://cdn/x.ts"}, "http://cdn/x.ts"),          # the other key names
    ({"link": "http://cdn/x.ts"}, "http://cdn/x.ts"),
    ({"cmd": ""}, ""),                                         # blanked by the panel
    ({"id": 7}, ""),                                           # a payload, but no link
    ("http://cdn/x.ts", "http://cdn/x.ts"),                    # a bare string
    (None, ""), ({"js": None}, ""), ("", ""), ({}, ""),        # nothing at all
])
def test_the_dict_and_string_shapes(js, want):
    payload = js.get("js") if isinstance(js, dict) and "js" in js else js
    assert read_link_answer(payload).raw == want


@pytest.mark.parametrize("js,raw,ads,candidates", [
    # the shape that used to be unread: one candidate in a list
    ([{"cmd": "ffmpeg http://cdn/x.ts", "storage_id": "7"}],
     "ffmpeg http://cdn/x.ts", 0, 1),
    # an advertisement first: taking the first entry would play the ad
    ([{"type": "ad", "cmd": "http://cdn/ad.mp4"},
      {"type": "stream", "cmd": "http://cdn/x.mp4"}], "http://cdn/x.mp4", 1, 1),
    # ads are recognised by label, and labels arrive in whatever case the panel
    # felt like ("AD", "Advert")
    ([{"type": "AD", "cmd": "http://cdn/ad.mp4"},
      {"type": "advert", "cmd": "http://cdn/ad2.mp4"},
      {"cmd": "http://cdn/x.mp4"}], "http://cdn/x.mp4", 2, 1),
    # nothing but ads is NO link - not the ad's URL, and not a crash
    ([{"type": "ad", "cmd": "http://cdn/ad.mp4"}], "", 1, 0),
    # entries that carry no link at all
    ([{"id": 1}, {"id": 2}], "", 0, 0),
    # a list of bare strings is still a list of candidates
    (["http://cdn/x.ts"], "http://cdn/x.ts", 0, 1),
    ([], "", 0, 0),
])
def test_a_list_answer_is_read_entry_by_entry(js, raw, ads, candidates):
    got = read_link_answer(js)
    assert got.shape == "list"
    assert got.raw == raw
    assert got.ads == ads
    assert got.candidates == candidates


def test_the_first_real_candidate_wins_and_its_storage_is_kept():
    """Panels order candidates by preference; the second one is the worse storage."""
    got = read_link_answer([{"cmd": "http://cdn/fast.mp4", "storage_id": "3"},
                            {"cmd": "http://cdn/slow.mp4", "storage_id": "9"}])
    assert got.raw == "http://cdn/fast.mp4"
    assert got.storage_id == "3"
    assert got.candidates == 2 and got.ads == 0


@pytest.mark.parametrize("js,expect", [
    (None, "an empty payload"),
    ([], "an empty list"),
    ([{"type": "ad", "cmd": "http://cdn/ad.mp4"}], "advertisement"),
    ([{"id": 1}, {"id": 2}], "2 entries"),
    ({"id": 7}, "no cmd/url/link"),
    ({"cmd": "/media/file_1.mpg"}, "not a URL"),
    (42, "unrecognised"),
])
def test_an_unusable_answer_names_its_own_shape(js, expect):
    """The point of `describe()`: the failure has to say WHAT arrived.

    "the portal returned no playable URL" is what a dead channel looks like; the
    sentence that follows it is what tells an unparsed answer apart from one.
    """
    assert expect in read_link_answer(js).describe()


# =========================================================================== #
# S-B: the two cmd forms of one file
# =========================================================================== #
@pytest.mark.parametrize("cmd,want", [
    ("/media/1234.mpg", "/media/1234.mpg"),
    ("auto /media/1234.mpg", "/media/1234.mpg"),        # tv_archive's prefix
    ("/media/1234", "/media/1234"),                     # no extension at all
    ("/media/file_1234.mpg", None),                     # already the file form
    ("/MEDIA/1234.MPG", "/MEDIA/1234.MPG"),             # panels and their cases
    ("ffmpeg http://cdn/x.mp4", None),                  # a link, not a reference
    ("http://cdn/media/1234.mpg", None),                # ...and not a path in one
    ("", None), (None, None),
])
def test_only_a_relative_storage_reference_counts(cmd, want):
    assert generic_media_ref(cmd) == want


@pytest.mark.parametrize("cmd,file_id,want", [
    ("/media/1234.mpg", "5678", "/media/file_5678.mpg"),
    ("/media/1234.mpg", None, "/media/file_1234.mpg"),   # the cheap guess
    ("/media/1234.mpg", "", "/media/file_1234.mpg"),
    ("auto /media/1234.mpg", "9", "auto /media/file_9.mpg"),   # prefix survives
    ("/media/1234", "9", "/media/file_9"),                     # no ext to keep
    ("ffmpeg http://cdn/x.mp4", "9", "ffmpeg http://cdn/x.mp4"),  # never a URL
    ("", "9", ""),
])
def test_the_file_form_rewrites_the_token_and_nothing_else(cmd, file_id, want):
    assert media_file_form(cmd, file_id) == want


# =========================================================================== #
# what the plan hands the stream paths
# =========================================================================== #
class _Src:
    def __init__(self, cmd, *, media_cmd=None, item_id="2001", link_flags=None):
        self.cmd = cmd
        self.media_cmd = media_cmd
        self.portal_item_id = item_id
        self.link_flags = link_flags


class _Mac:
    mac = GOOD
    force_ch_link_check = False


def test_a_learned_form_is_asked_first_and_the_catalogue_form_is_the_fallback():
    plan = plan_for(_Src(MEDIA, media_cmd="/media/file_9001.mpg"), _Mac())
    assert plan.cmd == "/media/file_9001.mpg", "asking with the catalogue form again " \
                                               "would pay for a refusal every play"
    assert plan.alt_cmd == MEDIA, "the learned form can go stale; the truth is the fallback"
    kwargs = plan.request_kwargs()
    assert kwargs["item_id"] == "2001" and kwargs["alt_cmd"] == MEDIA


def test_a_row_with_nothing_learned_offers_no_fallback_form():
    """The second form exists only when there are two forms to choose between."""
    kwargs = plan_for(_Src(f"ffmpeg {CLEAN}"), _Mac()).request_kwargs()
    assert "alt_cmd" not in kwargs and kwargs["item_id"] == "2001"
    bare = plan_for(_Src(MEDIA, item_id=""), _Mac()).request_kwargs()
    assert "item_id" not in bare and "alt_cmd" not in bare, \
        "a row with neither an id nor a learned form sends no new keyword at all"


# =========================================================================== #
# against a panel that answers like this
# =========================================================================== #
async def test_a_list_answer_is_played_and_the_advertisement_is_not(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(create_link_list="ad")
    client = w.client(GOOD)
    url = await client.create_link(f"ffmpeg {CLEAN}", "live")
    assert url == CLEAN
    assert client.last_cmd_repair is None, "the shape was not a form problem"
    assert (await w.state())["counters"]["create_links"] == 1


async def test_a_list_of_only_advertisements_is_no_link_and_says_so(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(create_link_list="ads_only")
    client = w.client(GOOD)
    with pytest.raises(PortalError) as err:
        await client.create_link(f"ffmpeg {CLEAN}", "vod")
    assert err.value.code == "no_url"
    assert "advertisement" in str(err.value), "the log must say the panel offered only ads"


async def test_the_generic_media_form_is_repaired_with_the_panels_file_id(monkeypatch):
    """The whole of S-B in one play: refused, resolved, retried, remembered."""
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    client = w.client(GOOD)
    url = await client.create_link(MEDIA, "vod", item_id="2001")
    assert url.endswith(f"/mock/vod/{FILE_ID}.mp4")
    repair = client.last_cmd_repair
    assert repair is not None and repair.how == "media_file"
    assert repair.asked == MEDIA and repair.worked == f"/media/file_{FILE_ID}.mpg"
    counters = (await w.state())["counters"]
    assert counters["media_refusals"] == 1, "the catalogue form was refused once"
    assert counters["media_resolutions"] == 1, "and we asked which file was behind it"


async def test_without_an_item_id_the_cheap_form_is_tried_and_nothing_is_resolved(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    client = w.client(GOOD)
    url = await client.create_link(MEDIA, "vod")
    assert url.endswith("/mock/vod/2001.mp4")
    assert client.last_cmd_repair.worked == "/media/file_2001.mpg"
    assert (await w.state())["counters"]["media_resolutions"] == 0


async def test_the_other_stored_form_is_tried_before_the_ladder(monkeypatch):
    """A row that knows both forms costs one refusal, not a refusal plus a resolution."""
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    client = w.client(GOOD)
    url = await client.create_link(MEDIA, "vod", item_id="2001",
                                   alt_cmd=f"/media/file_{FILE_ID}.mpg")
    assert url.endswith(f"/mock/vod/{FILE_ID}.mp4")
    assert client.last_cmd_repair == CmdRepair(
        asked=MEDIA, worked=f"/media/file_{FILE_ID}.mpg", how="stored")
    counters = (await w.state())["counters"]
    assert counters["media_refusals"] == 1, "the first form was refused once"
    assert counters["media_resolutions"] == 0, "and the ladder was never needed"


async def test_a_form_the_panel_is_happy_with_is_not_second_guessed(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    client = w.client(GOOD)
    url = await client.create_link(f"/media/file_{FILE_ID}.mpg", "vod", item_id="2001")
    assert url.endswith(f"/mock/vod/{FILE_ID}.mp4")
    assert client.last_cmd_repair is None
    counters = (await w.state())["counters"]
    assert counters["media_refusals"] == 0 and counters["media_resolutions"] == 0


async def test_the_ladder_has_a_kill_switch(monkeypatch):
    """A panel that rate-limits aggressively can be exempted without a code change."""
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    monkeypatch.setattr(client_mod, "MEDIA_REPAIR_ENABLED", False)
    client = w.client(GOOD)
    with pytest.raises(PortalError) as err:
        await client.create_link(MEDIA, "vod", item_id="2001")
    assert err.value.code == "nothing_to_play", "the panel's OWN refusal must surface"
    assert (await w.state())["counters"]["media_resolutions"] == 0


async def test_a_cmd_that_is_not_a_storage_reference_is_left_alone(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    client = w.client(GOOD)
    assert await client.create_link(f"ffmpeg {CLEAN}", "live") == CLEAN
    assert client.last_cmd_repair is None
    assert (await w.state())["counters"]["media_refusals"] == 0


async def test_a_mac_refusal_is_not_treated_as_a_form_problem(monkeypatch):
    """`limit` is about the MAC: asking again in another shape burns a slot."""
    w = Wired(monkeypatch)
    await w.control(create_link_error="limit")
    client = w.client(GOOD)
    with pytest.raises(PortalError) as err:
        await client.create_link(MEDIA, "vod", item_id="2001")
    assert err.value.code == "limit"
    counters = (await w.state())["counters"]
    assert counters["create_links"] == 1, "exactly one attempt, no ladder"
    assert counters["media_resolutions"] == 0


# =========================================================================== #
# the stream path learns it, and then stops paying for it
# =========================================================================== #
async def _vod_route(cmd=MEDIA, *, media_cmd=None, item_id="2001"):
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://test/mock/c/", enabled=True,
                   resolved_url=PORTAL, direct_links=True)
        s.add(p)
        await s.flush()
        mac = MacAddress(portal_id=p.id, mac=GOOD, order=0)
        s.add(mac)
        src = VodSource(portal_id=p.id, portal_item_id=item_id, original_name="Movie",
                        cmd=cmd, enabled=True, media_cmd=media_cmd)
        s.add(src)
        await s.flush()
        pl = VodPlaylist(vod_source_id=src.id, custom_name="Movie", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(VodPlaylistSource(vod_playlist_id=pl.id, vod_source_id=src.id, priority=1))
        await s.commit()
        return pl.id, src.id, mac.id


async def _row(src_id: int) -> VodSource:
    async with SessionLocal() as s:
        return await s.get(VodSource, src_id)


async def test_a_play_stores_the_form_it_learned_and_never_rewrites_the_catalogue(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    pl, src_id, _mac_id = await _vod_route()
    url, name = await MANAGER.resolve("vod", pl)
    assert name == "Movie" and url.endswith(f"/mock/vod/{FILE_ID}.mp4")
    row = await _row(src_id)
    assert row.media_cmd == f"/media/file_{FILE_ID}.mpg", "learned for the next play"
    assert row.cmd == MEDIA, "the catalogue stays the panel's truth"
    await flush_logs()


async def test_the_next_play_does_not_pay_for_the_refusal_again(monkeypatch):
    """The only reason the column exists, asserted with the mock's counters."""
    w = Wired(monkeypatch)
    await w.control(media_form="file_only")
    pl, _src_id, mac_id = await _vod_route()
    await MANAGER.resolve("vod", pl)
    first = (await w.state())["counters"]
    assert (first["media_refusals"], first["media_resolutions"]) == (1, 1)

    # a redirect leases its MAC for REDIRECT_LEASE_S, which is exactly what makes
    # two back-to-back plays of one item honest about concurrency - and what would
    # make this test measure a skipped MAC instead of a learned cmd form
    MANAGER.release_macs([mac_id])
    _STATE.update({"create_links": 0, "media_refusals": 0, "media_resolutions": 0})
    url, _name = await MANAGER.resolve("vod", pl)
    assert url.endswith(f"/mock/vod/{FILE_ID}.mp4")
    second = (await w.state())["counters"]
    assert second["create_links"] == 1, "one create_link, with the form that works"
    assert second["media_refusals"] == 0, "no refusal to recover from"
    assert second["media_resolutions"] == 0, "and no movie resolution to pay for"
    await flush_logs()


async def test_a_stale_learned_form_is_cleared_not_kept():
    """NULL is an action: the catalogue form worked, so the memory is wrong."""
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://test/mock/c/", enabled=True)
        s.add(p)
        await s.flush()
        src = VodSource(portal_id=p.id, portal_item_id="2001", original_name="Movie",
                        cmd=MEDIA, media_cmd="/media/file_9001.mpg")
        s.add(src)
        await s.commit()
        src_id = src.id

    await _store_media_cmd(await _row(src_id),
                           CmdRepair(asked="/media/file_9001.mpg", worked=MEDIA))
    assert (await _row(src_id)).media_cmd is None

    # and writing what the row already says is not a write at all
    row = await _row(src_id)
    await _store_media_cmd(row, CmdRepair(asked=MEDIA, worked=MEDIA))
    assert (await _row(src_id)).media_cmd is None


async def test_a_row_that_has_no_such_form_is_ignored():
    """LiveSource has no storage reference to learn; that is not an error."""
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://test/mock/c/", enabled=True)
        s.add(p)
        await s.flush()
        src = LiveSource(portal_id=p.id, portal_channel_id="1", original_name="Ch",
                         cmd=f"ffmpeg {CLEAN}")
        s.add(src)
        await s.commit()
        live_id = src.id
    async with SessionLocal() as s:
        live = await s.get(LiveSource, live_id)
    await _store_media_cmd(live, CmdRepair(asked="a", worked="b", how="media_file"))
    assert not hasattr(type(live), "media_cmd")
