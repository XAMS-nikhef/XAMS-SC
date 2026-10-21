"""Which page answers which question. See DESIGN.md §8.1, §8.2.

The site used to open on an Overview that answered two questions at once —
"is everything all right?" and "change something" — because the Lake Shore
and the flow reset had nowhere else to be. They are separate questions, asked
at different moments, and they are now separate pages:

    /         the plant, drawn, live, READ-ONLY
    /hv       Control: everything that writes to an instrument
    /system   System health: is the software all right
    /status   the channel list
    /rga      the residual gas analyser, status only for now
    /alarms   the alarm chain
    /logs     the service logs

What is pinned here is that every one of them answers, that the old
bookmarks still work, and that no page grew a second job.
"""

from __future__ import annotations

import re

import pytest

#: Every form in this system that reaches an instrument or closes a record.
#: `/operator` is deliberately absent: it sets a cookie saying who is at the
#: keyboard, which is how the audit trail gets a name, and it touches nothing.
INSTRUMENT_WRITES = ('action="/hv/apply"', 'action="/hv/output"',
                     'action="/hv/clear"',
                     'action="/lakeshore/setpoint"', 'action="/lakeshore/range"',
                     'action="/flow/reset"')

#: The RGA's writes, which live on its own page by design: take it, scan,
#: look at the spectrum, save, give it back is one workflow around one plot
#: (docs/drivers/rga.md), with emission and the electron multiplier.
RGA_WRITES = ('action="/rga/control"', 'action="/rga/scan"', 'action="/rga/emission"',
              'action="/rga/em"')

PAGES = ("/", "/status", "/hv", "/hv/defaults", "/rga", "/system", "/alarms", "/logs")


class TestEveryPageAnswers:
    @pytest.mark.parametrize("path", PAGES)
    def test_it_renders(self, client, path):
        assert client.get(path).status_code == 200


class TestTheOldAddressesStillWork:
    def test_the_mimic_url_redirects_to_the_landing_page(self, client):
        """It is in the manual, in bookmarks and on a wall-display kiosk."""
        r = client.get("/mimic", follow_redirects=False)

        assert r.status_code == 301
        assert r.headers["location"] == "/"

    def test_following_it_arrives_at_the_drawing(self, client):
        r = client.get("/mimic")

        assert r.status_code == 200
        assert "P&amp;I" in r.text


class TestNoPageHasTwoJobs:
    def test_the_landing_page_writes_to_no_instrument(self, client):
        body = client.get("/").text

        for action in INSTRUMENT_WRITES:
            assert action not in body, f"{action} appeared on the P&ID"

    def test_system_health_writes_nothing(self, client):
        """It answers "is the software all right", and nothing else. The two
        control cards it used to carry are the reason this page felt like two
        pages stapled together."""
        body = client.get("/system").text

        assert 'action="/lakeshore/setpoint"' not in body
        assert 'action="/flow/reset"' not in body

    def test_control_carries_every_write_in_the_system(self, client):
        body = client.get("/hv").text

        for action in INSTRUMENT_WRITES:
            assert action in body, f"{action} is not on the Control page"

    def test_the_rga_writes_are_on_the_rga_page_and_nowhere_else(self, client):
        body = client.get("/rga").text
        for action in RGA_WRITES:
            assert action in body, f"{action} is not on the RGA page"
        for path in ("/", "/system", "/status"):
            other = client.get(path).text
            assert not [a for a in RGA_WRITES if a in other], path

    def test_control_points_at_the_rga_page(self, client):
        assert 'href="/rga"' in client.get("/hv").text

    def test_control_points_at_what_it_will_not_do(self, client):
        """The §10a limits — no enable, no MAXV, no TRIP. The explanation is
        in the manual (21 October 2026: a card of text at the foot of the
        page was never read); the page links to it."""
        body = client.get("/hv").text

        assert "what this page cannot do" in body
        assert "/manual/operating/webui/#control-high-voltage" in body


class TestTheNavBar:
    def test_it_offers_the_pages_it_should(self, client):
        nav = client.get("/").text
        nav = nav[nav.index("<nav>"):nav.index("</nav>")]

        labels = re.findall(r">([^<>]+)</a>", nav)
        assert [l.strip() for l in labels][:4] == [
            "P&amp;I", "Channels", "Control", "System health"]

    def test_every_internal_link_resolves(self, client):
        """A nav that points at a 404 is worse than one that is missing it."""
        html = client.get("/").text
        nav = html[html.index("<nav>"):html.index("</nav>")]

        for href in re.findall(r'href="(/[^"#]*)', nav):
            assert client.get(href).status_code == 200, href


class TestTheControlPageLayout:
    def test_the_two_small_cards_share_a_row(self, client):
        """Stacked, they pushed the high voltage below the fold on a laptop,
        and neither is wide enough to need a full row."""
        body = client.get("/hv").text

        assert "control-pair" in body
        pair = body[body.index('class="control-pair"'):body.index('id="hv"')]
        assert 'id="cryostat"' in pair and 'id="flow"' in pair

    def test_they_fold_to_one_column_on_a_narrow_window(self, client):
        # 1200 px since 73ec4af; the test had kept the old 900.
        assert "@media (max-width:1200px)" in client.get("/hv").text

    def test_the_two_cards_are_the_same_height(self, client):
        """They used to end wherever their own text ran out, which left a
        ragged edge between two boxes sitting side by side.

        Asserted on the rule rather than on a rendered height, which no test
        here can measure: `align-items:start` is the thing that was wrong and
        the thing somebody would put back.

        Matched inside the `.control-pair` declaration alone. Searching the
        whole page for `align-items:start` also finds the comment explaining
        this, and the P&ID's own layout, where starting at the top is right.
        """
        body = client.get("/hv").text
        rule = re.search(r"\.control-pair \{[^}]*\}", body)
        assert rule is not None, "the .control-pair rule is gone"
        assert "align-items:stretch" in rule.group(0)
        assert "start" not in rule.group(0)
        assert ".control-pair > div { display:flex; }" in body

    def test_the_page_does_not_label_the_obvious(self, client):
        """No "On this page" bar, and no "High voltage" heading over two
        cards that already name themselves hv_1 and hv_2.

        Three links to three sections of one short page is a table of
        contents for something you can see all of, and it was the first thing
        under the header on the page people come to in order to act.
        """
        body = client.get("/hv").text
        assert "On this page" not in body
        assert "<h2 id=\"hv\"" not in body

    def test_the_anchors_the_mimic_links_to_still_exist(self, client):
        """A cross-page guard, in the direction nothing else checks.

        test_mimic_page asserts the P&ID LINKS to /hv#cryostat, /hv#flow and
        /hv#hv. Nothing asserted the targets were there, so removing the
        heading that carried `id="hv"` would have left that link landing
        silently at the top of the page - and the two redirects after a Lake
        Shore write or a flow reset do the same thing with #cryostat and
        #flow.
        """
        body = client.get("/hv").text
        for anchor in ("cryostat", "flow", "hv"):
            assert 'id="%s"' % anchor in body, anchor
