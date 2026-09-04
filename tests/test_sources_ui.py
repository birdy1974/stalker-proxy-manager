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
    assert "enable the channel to assign a playlist name" in SOURCES
