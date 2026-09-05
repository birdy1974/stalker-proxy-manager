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


def test_live_playlist_click_selects_the_whole_name():
    assert "onfocus=\"this.select()\"" in SOURCES
    assert "onmouseup=\"event.preventDefault()\"" in SOURCES
    # disabled channels stay blank, not an input
    assert "enable the channel to give it a custom name" in SOURCES


def test_the_editable_column_is_named_and_sized_for_typing():
    """The column between Channel and Portal is the one people type in.

    It used to be headed "Playlist" - which reads like the Playlist *tab* - and
    it was narrower than the read-only portal name beside it. Both are pinned
    here because both are one careless edit away from coming back.
    """
    assert 'label: "Custom Channel Name"' in SOURCES
    assert 'label: "Playlist"' not in SOURCES
    channel = SOURCES.index('label: "Channel"')
    custom = SOURCES.index('label: "Custom Channel Name"')
    portal = SOURCES.index('label: "Portal"', channel)
    assert channel < custom < portal
    # the editable column is the wider of the two
    def _width(at: int) -> int:
        import re
        return int(re.search(r'width: "(\d+)px"', SOURCES[at:at + 400]).group(1))
    assert _width(custom) > _width(channel)
