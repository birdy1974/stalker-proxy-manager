from app.services.titles import best_title, m3u_display_title, portal_item_title


def test_year_only_loses_to_full_title():
    assert best_title("2026", "**Man of War - 2026") == "**Man of War - 2026"
    assert best_title("2026") == "2026"


def test_portal_item_prefers_o_name():
    assert portal_item_title({"name": "2026", "o_name": "**Man of War - 2026"}) == "**Man of War - 2026"
    assert portal_item_title({"name": "The Matrix"}) == "The Matrix"


def _vlc3_extinf_title(extinf: str) -> str:
    """VLC 3.0 parseEXTINF: first comma ends duration, then ' - ' splits artist/title."""
    after = extinf.split(",", 1)[1]
    if " - " in after:
        return after.split(" - ", 1)[1]
    if after.startswith(","):
        return after[1:]
    if "," in after:
        return after.split(",", 1)[1]
    return after


def test_m3u_display_title_keeps_minus_visible_but_vlc_does_not_split():
    """VLC 3.0 would otherwise show only '2026' for 'Man of War - 2026'."""
    raw = "**Man of War - 2026"
    safe = m3u_display_title(raw)
    assert "Man of War" in safe and "2026" in safe
    assert " - " not in safe, "ASCII space-hyphen-space is VLC's artist/title splitter"
    assert "\u2013" in safe
    extinf = f"#EXTINF:-1 tvg-name=\"{safe}\",{safe}"
    shown = _vlc3_extinf_title(extinf)
    assert "Man of War" in shown and "2026" in shown, shown
    assert shown == safe


def test_m3u_display_title_strips_newlines_and_leaves_plain_names():
    assert m3u_display_title("Broken\nChannel") == "Broken Channel"
    assert m3u_display_title("NPO 1") == "NPO 1"
    assert m3u_display_title("Spider-Man") == "Spider-Man"  # no spaces around '-'
