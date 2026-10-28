"""The P&ID page's column and its liveness. See DESIGN.md §8.2.

Two things are pinned here, and the second is the one that matters.

**Order.** The alarm card grows and shrinks as alarms come and go. Anything
below it therefore moves, so it sits at the BOTTOM and the cards whose height
never changes sit above it. What replaces it at the top is a fixed-height line
that says only whether something is wrong — glance at that, read the card
afterwards.

**Liveness.** Every channel that stops reading already goes to a dash rather
than its last good number. The case that was NOT handled is the page itself
going quiet: `refresh` caught its own fetch failure and returned, keeping what
was drawn, so a dead web UI or a dropped network left a screen full of frozen
plausible values looking live. On the one page that is left open on a display
all day, that is the exact failure principle 4 names.
"""

from __future__ import annotations

import pathlib
import re

import pytest


@pytest.fixture
def page(client):
    response = client.get("/mimic")
    assert response.status_code == 200
    return response.text


def headings(html):
    """The card titles, without the "change ->" links that live inside them."""
    return [re.sub(r"<[^>]+>", "", re.sub(r"<a\b.*?</a>", "", h, flags=re.S)).strip()
            for h in re.findall(r"<h2>(.*?)</h2>", html, re.S)]


class TestTheColumnOrder:
    def test_the_cards_are_in_the_order_somebody_reads_them(self, page):
        sidebar = [h for h in headings(page)
                   if h != "P&amp;I &mdash; live values"]

        assert sidebar == ["Cryostat setpoint", "Xenon moved", "High voltage",
                           "Mains", "Turbo pump", "Alarms"]

    def test_alarms_are_last_because_that_card_changes_height(self, page):
        assert headings(page)[-1] == "Alarms", \
            "a card that grows must not push the stable ones down the page"

    def test_the_status_line_comes_before_every_card(self, page):
        assert page.index('id="s-status"') < page.index("Cryostat setpoint")


class TestTheDrawingIsNotRepeated:
    def test_no_sidebar_card_shows_a_pressure_or_a_temperature(self, page, config):
        """Strictly the complement of the drawing. A reading with two homes on
        one page is fine until the day they disagree."""
        sidebar = page[page.index('<aside class="mimic-side">'):page.index("</aside>")]

        on_drawing = [c.name for c in config.enabled_channels()
                      if c.kind in ("rtd", "temperature") and c.on_pid]
        repeated = [n for n in on_drawing if 's-' + n in sidebar]

        assert repeated == [], f"already on the drawing: {repeated}"


class TestTheStrainGaugesAreOnTheDrawing:
    """SG101 and SG102 are tagged instruments on the P&ID, so they get a value
    slot like any other point in the plant.

    They were briefly given `on_pid: false` on the assumption that a load cell
    is not a piping point. It is one here: the drawing carries both tags, each
    with its own bubble, and the generator found them the moment the channels
    existed. The drift check must therefore keep asking for them — that is the
    half of §8.2 that catches a reading quietly vanishing from the mimic.
    """

    @pytest.fixture
    def svg(self):
        return pathlib.Path(
            "src/xams_sc/api/static/xams_pid.svg").read_text(encoding="utf-8")

    @pytest.mark.parametrize("name", ["sg101", "sg102"])
    def test_the_drawing_has_a_slot_for_it(self, svg, name):
        assert f'id="v-{name}"' in svg

    @pytest.mark.parametrize("name", ["sg101", "sg102"])
    def test_it_is_not_excused_from_the_drawing(self, config, name):
        assert config.channels[name].on_pid is True

    def test_the_drift_check_covers_current_channels(self, config):
        """The check listed rtd, temperature and cdaq VOLTAGE channels. A cdaq
        current channel is as much a point in the plant as the voltage one
        beside it, and without this it could drop off the mimic in silence."""
        from xams_sc.api.mimic import check_mimic_tags

        assert check_mimic_tags(config) == []

        stripped = config.channels["sg101"].__class__(
            **{**config.channels["sg101"].__dict__, "name": "sg999"})
        config.channels["sg999"] = stripped
        try:
            problems = check_mimic_tags(config)
            assert any("sg999" in p for p in problems), (
                "a cdaq current channel with no slot must be reported")
        finally:
            del config.channels["sg999"]


class TestLiveness:
    def test_the_page_can_say_it_has_stopped_updating(self, page):
        assert "markStale" in page
        assert "NOT UPDATING" in page

    def test_one_dropped_request_does_not_blank_the_page(self, page):
        """Making the whole mimic flicker because a packet went missing would
        be worse than the packet."""
        assert "STALE_AFTER_MS" in page
        assert re.search(r"Date\.now\(\)\s*-\s*lastOk\s*<\s*STALE_AFTER_MS", page), \
            "staleness is decided on the first failure rather than on elapsed time"

    def test_a_throttled_tab_is_still_caught(self, page):
        """A tab the browser has stopped scheduling never reaches the catch,
        so elapsed time is checked on its own timer too."""
        assert re.search(r"setInterval\(\(\)\s*=>\s*\{\s*\n?\s*if \(lastOk", page)

    def test_the_drawing_is_withdrawn_and_not_merely_annotated(self, page):
        """A banner over a drawing that still looks live is half a warning."""
        assert ".mimic-body.stale" in page
        assert 'body.classList.add("stale")' in page

    def test_going_stale_is_reversible(self, page):
        assert 'body.classList.remove("stale")' in page


class TestWhereControlLives:
    """The P&ID does not write to instruments; it says where to (§8.2).

    This is the page left open on a screen all day, and a setpoint box on it
    is the wrong thing to reach for by accident. The cards link to the
    Control page instead, which costs a deliberate navigation — the same
    reasoning that keeps the CAEN front-panel enable a hand operation.
    """

    def test_the_drawing_writes_to_no_instrument(self, page):
        """The `/operator` form in the header is not one: it sets a cookie
        naming who is at the keyboard, which is how the audit trail gets a
        name, and it touches no hardware."""
        for action in ('action="/hv/apply"', 'action="/hv/output"',
                       'action="/hv/clear"', 'action="/lakeshore/setpoint"',
                       'action="/lakeshore/range"', 'action="/flow/reset"'):
            assert action not in page, f"{action} appeared on the P&ID"

    def test_each_controllable_card_says_where_to_change_it(self, page):
        for anchor in ("/hv#cryostat", "/hv#flow", "/hv#hv"):
            assert anchor in page, f"no way through to {anchor}"

    def test_the_read_only_cards_offer_nothing(self, page):
        """Mains and Alarms are not controls and must not pretend to be."""
        mains = page[page.index("<h2>Mains"):page.index("<h2>Alarms")]

        assert "change &rarr;" not in mains


class TestTheCryostatCard:
    """Setpoint and heater effort, read as one thing. See DESIGN.md §8.2.

    The heater was shown as a percentage of full scale, which answers "how
    hard is it working" only once you know what full scale is. Watts is the
    number you compare against the heat load, and it is the derived channel
    `ls_heater_1_w` — the square-law transform in scaling.py, not a second
    copy of the arithmetic here.
    """

    def test_the_heater_is_shown_in_watts(self, page):
        assert 'id="s-ls_heater_1_w"' in page
        assert "ls_heater_1_w" in page.split("function drawSide")[1], \
            "the element is there but nothing fills it"

    def test_the_percentage_survives_as_context(self, page):
        """Not a replacement: full-scale percentage is what the instrument
        actually reports, and it is worth seeing beside the watts."""
        assert 'id="s-ls_heater_1"' in page

    def test_the_card_no_longer_points_at_a_page_that_moved(self, page):
        """It used to say "change it on the overview". The overview is not at
        `/` any more, so that link pointed the P&ID at itself."""
        card = page[page.index("Cryostat setpoint"):page.index("Xenon moved")]

        assert '<a href="/">' not in card
        assert "/hv#cryostat" in card


class TestTheDrawingGetsTheRoom:
    """Every rem of chrome on this page is a rem the drawing does not get.

    The drawing is bounded in both dimensions so it is never scrolled — the
    whole point of a mimic is that it is taken in at a glance. What bounds the
    height is whatever the page furniture leaves, so the furniture is kept to
    what earns its place.
    """

    def test_the_drawing_is_still_bounded_so_it_never_scrolls(self, page):
        """Bounded in BOTH dimensions: width by the column, height by what the
        page furniture leaves. Unbounded height is how a mimic becomes a thing
        you scroll, which is no longer a mimic."""
        assert "max-height:calc(100vh" in page
        assert ".mimic-body { padding:" in page and "overflow:hidden" in page


class TestTheDrawingFillsItsBox:
    """The viewBox is cropped to the ink. See tools/build_mimic.py.

    The PDF is a drawing sheet, so the page was bigger than the drawing: even
    after the 10pt border inset there were 42 units of blank paper above the
    ink and 76 below. In a box whose height is bounded — which is the whole of
    §8.2 — that blank paper comes off the drawing before anything else gets a
    say, so the mimic rendered about 14% smaller than it needed to, for a
    reason nobody could see by looking at it.

    Trimming page furniture did nothing, which was the clue: the drawing was
    already fitting, and what it was fitting was mostly margin.
    """

    #: What the generator leaves, and why. The values are centred on their
    #: tags and grow as they are filled in, so a tag near an edge must still
    #: have room to say its number.
    MARGIN_X, MARGIN_Y = 18.0, 12.0

    @pytest.fixture
    def viewbox(self):
        import pathlib
        svg = pathlib.Path("src/xams_sc/api/static/xams_pid.svg").read_text()
        return [float(n) for n in
                re.search(r'viewBox="([^"]+)"', svg).group(1).split()]

    def test_it_no_longer_starts_at_the_sheet_corner(self, viewbox):
        x, y, _, _ = viewbox

        assert (x, y) != (0.0, 0.0), "the viewBox is still the whole sheet"

    def test_the_drawing_is_wider_than_it_is_tall_by_more_than_the_sheet(
            self, viewbox):
        """The sheet was 1.425:1; the ink is about 1.56:1. A box that matches
        the ink wastes nothing whichever dimension binds."""
        _, _, w, h = viewbox

        assert w / h > 1.5

    def test_the_margins_are_the_ones_the_generator_intends(self):
        """Regenerating must reproduce this, not undo it."""
        build = pathlib.Path("tools/build_mimic.py").read_text()

        assert f"INK_MARGIN_X = {self.MARGIN_X}" in build
        assert f"INK_MARGIN_Y = {self.MARGIN_Y}" in build
        assert "tighten_viewbox" in build

    def test_the_generator_crops_after_writing_not_before(self):
        """It measures by RENDERING the finished file, because the geometry is
        a soup of relative path commands and the one thing certainly right
        about a rendering is where the ink is."""
        build = pathlib.Path("tools/build_mimic.py").read_text()
        write = build.index("OUT.write_bytes")

        assert build.index("box = tighten_viewbox(OUT") > write


class TestTheTurbo:
    """The pump's state, drawn as the colour of its outline and in a card.

    The outline is a COPY of the drawing's own line work, so the drawing
    underneath is untouched and "off" is simply the copy left invisible.
    """

    @pytest.fixture
    def svg(self):
        return pathlib.Path(
            "src/xams_sc/api/static/xams_pid.svg").read_text(encoding="utf-8")

    def test_the_drawing_carries_the_outline_and_the_speed(self, svg):
        assert 'id="sym-turbo"' in svg
        assert 'id="v-turbo_speed"' in svg

    def test_the_outline_is_invisible_until_a_state_is_known(self, svg):
        """Green before anything has been read is a claim nobody made."""
        tag = re.search(r'<path id="sym-turbo"[^>]*>', svg).group(0)
        assert 'stroke="none"' in tag
        assert 'fill="none"' in tag

    def test_clicking_the_pump_opens_the_speed_history(self, svg, page):
        tag = re.search(r'<path id="sym-turbo"[^>]*>', svg).group(0)
        assert 'data-history="turbo_speed"' in tag
        assert "dataset.history" in page

    def test_the_speed_is_a_whole_number(self, svg):
        tag = re.search(r'<text id="v-turbo_speed"[^>]*>', svg).group(0)
        assert 'data-digits="0"' in tag

    def test_the_card_says_why_the_speed_is_missing(self, page):
        assert "service line (795) to 309" in page

    @pytest.mark.parametrize("state", ["at speed", "accelerating", "running down",
                                       "over temperature", "no data", "Err "])
    def test_every_state_has_words_as_well_as_a_colour(self, page, state):
        assert state in page


class TestThePkrGauge:
    """P105: the drawing's "P_pirani" is relabelled when the mimic is built."""

    @pytest.fixture
    def svg(self):
        return pathlib.Path(
            "src/xams_sc/api/static/xams_pid.svg").read_text(encoding="utf-8")

    def test_the_old_label_is_gone_and_p105_has_a_slot(self, svg):
        assert "pirani" not in svg
        assert ">P105<" in svg
        assert 'id="v-p105"' in svg

    def test_the_pressure_is_written_in_scientific_notation(self, svg, page):
        tag = re.search(r'<text id="v-p105"[^>]*>', svg).group(0)
        assert 'data-format="exp"' in tag
        assert "toExponential" in page

    def test_no_label_falls_back_to_a_serif_font(self, svg):
        """Helvetica-set labels came out as "Nimbus Sans" with no fallback."""
        assert "Nimbus" not in svg

    def test_p105_is_on_the_drawing_and_its_raw_volts_are_not(self, config):
        assert config.channels["p105"].on_pid is True
        assert config.channels["p105_v"].on_pid is False


def test_the_ln2_dewar_has_its_bubble():
    """SG401, added by build_mimic.py above the N2 vessel (21 October 2026):
    the net LN2 mass, in a bubble like SG101 and SG102, in whole kg."""
    svg = (pathlib.Path(__file__).resolve().parents[1] / "src" / "xams_sc"
           / "api" / "static" / "xams_pid.svg").read_text(encoding="utf-8")
    assert "SG401" in svg
    value = re.search(r'<text id="v-ln2_mass"[^>]*>', svg).group(0)
    assert 'data-unit="kg"' in value and 'data-digits="0"' in value
    assert 'id="u-ln2_mass"' in svg          # in a bubble: unit on its own line


def test_the_hv_column_leaves_out_disabled_channels(page):
    """28 October 2026: the old spare (hv_2 ch 1, 14-28 Oct) and the NaI are
    disabled and share their index with the gate and the SiPM equalizer; they
    are not read, so they have no row beside the drawing."""
    assert "s-hv_spare_vmon" not in page and "s-hv_nai_vmon" not in page
    assert "s-hv_sipm_eq_vmon" in page and "s-hv_gate_vmon" in page
