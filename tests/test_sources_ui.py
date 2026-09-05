"""Input Sources GUI wiring that is easy to regress in the template."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (ROOT / "app/templates/sources.html").read_text()


def test_local_file_on_badge_is_clickable():
    assert "toggleLocalFile(" in SOURCES
    assert "/api/sources/local/files/toggle" in SOURCES
    # the green/grey badge itself is the click target
    assert "onclick=\"event.stopPropagation();toggleLocalFile(" in SOURCES


def test_local_files_table_has_bulk_enable_disable():
    # same checkbox + bulk bar pattern as live/vod/series sources
    assert "selectable: true, perPageDefault: 15" in SOURCES
    assert "bulkLocalFiles" in SOURCES
    assert 'mBtn("Enable selected", "btn-outline-success", bulkLocalFiles(true)' in SOURCES
    assert 'mBtn("Disable selected", "btn-outline-secondary", bulkLocalFiles(false)' in SOURCES


def test_live_playlist_first_click_selects_name_and_second_click_places_caret():
    assert "onfocus=\"this.select()\"" in SOURCES
    # The first click must preserve select-all, but subsequent clicks must not
    # have their normal caret placement prevented.
    assert "document.activeElement === this" in SOURCES
    assert "if(this.dataset.wasFocused === 'false') event.preventDefault()" in SOURCES
    assert 'onmouseup="event.preventDefault()"' not in SOURCES
    # disabled channels stay blank, not an input
    assert "enable the channel to give it a custom name" in SOURCES


def test_enabling_one_live_source_reloads_its_custom_name_cell():
    assert ".then(()=>TABS.live&&TABS.live.reload())" in SOURCES


def test_custom_group_has_the_same_inline_edit_interaction():
    assert "playlistGroupCell(r)" in SOURCES
    assert "saveLivePlaylistGroup" in SOURCES
    assert "/playlist-group`" in SOURCES
    assert "enable the channel to give it a custom group" in SOURCES


def test_the_editable_columns_are_named_and_sized_for_typing():
    """The editable custom fields sit between Channel and Portal.

    The name used to be headed "Playlist" - which reads like the Playlist
    *tab* - and was narrower than the read-only portal name beside it. These
    details are pinned because they are one careless edit away from regressing.
    """
    assert 'label: "Custom Channel Name"' in SOURCES
    assert 'label: "Custom Group"' in SOURCES
    assert 'label: "Playlist"' not in SOURCES
    channel = SOURCES.index('label: "Channel"')
    custom = SOURCES.index('label: "Custom Channel Name"')
    group = SOURCES.index('label: "Custom Group"')
    portal = SOURCES.index('label: "Portal"', channel)
    assert channel < custom < group < portal
    # the editable column is the wider of the two
    def _width(at: int) -> int:
        import re
        return int(re.search(r'width: "(\d+)px"', SOURCES[at:at + 400]).group(1))
    assert _width(custom) > _width(channel)
