from app.services.titles import best_title, portal_item_title


def test_year_only_loses_to_full_title():
    assert best_title("2026", "**Man of War - 2026") == "**Man of War - 2026"
    assert best_title("2026") == "2026"


def test_portal_item_prefers_o_name():
    assert portal_item_title({"name": "2026", "o_name": "**Man of War - 2026"}) == "**Man of War - 2026"
    assert portal_item_title({"name": "The Matrix"}) == "The Matrix"
