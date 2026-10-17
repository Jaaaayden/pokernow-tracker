"""The chart page's Group by panel: present, fed by /groupby, and hidden where it
does not belong."""

import re
from pathlib import Path

CHART = Path(__file__).resolve().parents[1] / "pnt" / "server" / "static" / "chart.html"


def test_the_chart_has_a_group_by_panel_that_bluff_value_hides():
    """The panel sits beside the chart card, asks /groupby, and is on value:bluff's
    hide list, so switching there from the grid leaves it behind."""
    html = CHART.read_text("utf-8")
    assert '<section class="card gb hidden" id="groupby"' in html
    assert '"/groupby?"' in html
    body = html[html.index("function renderBluffs("):]
    hidden = set(re.findall(r'"(\w+)"', body[body.index("for (const id of [") : body.index("]) {")]))
    assert "groupby" in hidden
    # Its dimensions are the server's catalog, not a list of the page's own.
    assert "body.dims" in html
