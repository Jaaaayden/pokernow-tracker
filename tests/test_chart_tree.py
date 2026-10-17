"""The chart page's Tree view and the made view's Streets tab: present, fed by
/tree and /streets, known to the URL, and hidden where they do not belong."""

import re
from pathlib import Path

CHART = Path(__file__).resolve().parents[1] / "pnt" / "server" / "static" / "chart.html"


def _bluff_hide_list(html: str) -> set[str]:
    body = html[html.index("function renderBluffs("):]
    return set(re.findall(r'"(\w+)"', body[body.index("for (const id of [") : body.index("]) {")]))


def test_the_chart_has_a_tree_view_that_bluff_value_hides():
    """`by=tree` is a view the URL knows, with a button after Lines, a box of its
    own and its controls, all on value:bluff's hide list."""
    html = CHART.read_text("utf-8")
    assert '<div id="tree" class="hidden"></div>' in html
    assert '<span class="ctl hidden" id="treectl">' in html
    assert 'const TREE_VIEW = "tree";' in html and "VIEWS.push(TREE_VIEW);" in html
    views = html.index('<span class="seg" role="group" aria-label="Analysis views">')
    buttons = re.findall(r'data-view="(\w+)"', html[views:])
    assert buttons.index("tree") == buttons.index("lines") + 1
    # One request for the whole tree, small by default, and the URL keeps its controls.
    assert "/tree?" in html and "min_hands: TREE_MIN" in html
    for key in ('q.set("depth"', 'q.set("tx"', 'q.get("depth")', 'q.get("tx")'):
        assert key in html, key
    assert {"tree", "treectl"} <= _bluff_hide_list(html)


def test_a_tree_click_goes_to_the_grid_with_the_node_filter():
    """A node's filter joins the spot the tree was built on, and the view becomes
    the preflop grid, whose group-by panel follows the spot."""
    html = CHART.read_text("utf-8")
    body = html[html.index("function drillTree("):]
    body = body[: body.index("\n  }\n")]
    assert 'state.by = "preflop"' in body and "apply()" in body
    assert '"preflop"' in html[html.index("const GB_VIEWS"):][:60]


def test_the_made_view_has_a_streets_tab():
    """Mix and Streets tabs on the made view; Streets reads /streets, keeps
    `made=streets` in the URL, and its box is on value:bluff's hide list."""
    html = CHART.read_text("utf-8")
    assert '<div id="streets" class="hidden"></div>' in html
    assert 'data-made="mix"' in html and 'data-made="streets"' in html
    assert "/streets?" in html
    assert 'q.set("made", "streets")' in html and 'q.get("made")' in html
    assert {"madeseg", "streets"} <= _bluff_hide_list(html)
