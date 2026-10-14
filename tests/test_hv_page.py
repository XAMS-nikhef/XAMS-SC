"""How /hv reports a channel's state. See DESIGN.md §7.2, §8.1.

The decoding itself is covered in `test_hv_status.py`; what is pinned here is
what a person actually reads on the page, because that is where two mistakes
in a row were made.

**First:** the page inferred on/off from `VMON > 1`, so a channel energised at
zero volts read as "off".

**Second:** the fix used STAT bit 0 as "enabled" — but bit 0 is the OUTPUT.
The front-panel enable switch is bit 10. A channel whose switch had been
flipped on but which was not energised matched nothing and fell through to
"off" again, telling the operator their switch had not worked.

Four states, and the middle two are the ones worth care:

    disabled   bit 10          the front-panel switch is off
    enabled    neither bit     switch on, output NOT energised
    ON 0 V     bit 0, VMON~0   energised, sitting at zero volts
    ON         bit 0, VMON!=0  energised with volts out
"""

import pytest
from fastapi.testclient import TestClient

from doubles import RecordingBus, StubDrift, ui_client
from xams_sc.api import app as app_module
from xams_sc.api.state import ChannelView


def view(name, value, unit="V"):
    return ChannelView(name=name, value=value, unit=unit, quality="ok",
                       age_s=1.0)


@pytest.fixture
def page(monkeypatch):
    """Render /hv with the status word and voltage of our choosing."""
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app()
    state = app.state.system

    def render(stat_word, vmon):
        def channel(name):
            if name.endswith("_stat"):
                return None if stat_word is None else \
                    view(name, float(stat_word), "bits")
            if name.endswith("_imon"):
                return view(name, 0.0, "uA")
            return view(name, vmon)

        monkeypatch.setattr(state, "channel", channel)
        response = ui_client(app).get("/hv")
        assert response.status_code == 200
        # ONLY the status cells of the tables. The explanatory card at the
        # bottom of the page spells out "ON", "enabled" and "off" in prose,
        # so searching the whole page matches the explanation rather than the
        # reading and every assertion below would pass regardless.
        return status_cells(response.text)

    return render


@pytest.fixture
def page_html(monkeypatch):
    """Like `page`, but returns the whole page and lets VSET be set too."""
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app()
    state = app.state.system

    def render(stat_word, vmon, vset):
        def channel(name):
            if name.endswith("_stat"):
                return None if stat_word is None else                     view(name, float(stat_word), "bits")
            if name.endswith("_vset"):
                return view(name, vset)
            if name.endswith("_imon"):
                return view(name, 0.0, "uA")
            return view(name, vmon)

        monkeypatch.setattr(state, "channel", channel)
        response = ui_client(app).get("/hv")
        assert response.status_code == 200
        return response.text

    return render


def status_cells(html):
    """The rendered Status column, one entry per channel row.

    The column is located by its HEADER, not by index. An earlier version
    hardcoded `tds[4]`, and adding a VSET column silently moved Status to
    position 5 - so eight tests failed on a page that was perfectly correct.
    A test coupled to column order breaks every time the table grows, and
    teaches people that a red suite is normal.
    """
    import html as htmlmod
    import re

    cells = []
    for table in re.findall(r'<table class="channels">(.*?)</table>', html,
                            re.S):
        headers = [" ".join(re.sub(r"<[^>]+>", " ", h).split())
                   for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        assert "Status" in headers, "the table has no Status column"
        index = headers.index("Status")

        for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(tds) <= index:
                continue                      # the header row
            text = re.sub(r"<[^>]+>", " ", tds[index])
            cells.append(" ".join(htmlmod.unescape(text).split()))
    assert cells, "no channel rows rendered"
    return cells


class TestTheFourStates:
    """The vocabulary changed on 18 September 2026, and the reason matters.

    This used to test three states, built on the belief that STAT bit 0 meant
    "enabled". **It does not.** Bit 0 is the OUTPUT being energised; the
    front-panel enable switch is bit 10. The two are different things, and a
    channel can be permitted without being energised.

    That gap had no label, so it fell through to "off" - and a channel whose
    switch had just been flipped on displayed as though it were still switched
    off. Which is exactly what was seen in the lab.

        disabled   bit 10          the front-panel switch is off
        enabled    neither bit     switch on, output NOT energised
        ON 0 V     bit 0, VMON~0   energised, sitting at zero volts
        ON         bit 0, VMON!=0  energised with volts out
    """

    def test_energised_with_volts_out_is_on(self, page):
        assert page(stat_word=1, vmon=-99.8) == ["ON"] * 8

    def test_energised_at_zero_says_so(self, page):
        """Live, and nothing on it yet. Not "off": it is one setpoint away
        from volts on an electrode."""
        assert page(stat_word=1, vmon=0.0) == ["ON 0 V"] * 8

    def test_the_switch_on_but_not_energised_reads_enabled(self, page):
        """THE CASE THAT WAS WRONG. Neither bit set: the operator has flipped
        the enable, and the channel is doing nothing until it is energised.

        Showing "off" here told them their switch had not worked.
        """
        assert page(stat_word=0, vmon=0.0) == ["enabled"] * 8

    def test_the_switch_off_reads_disabled(self, page):
        """Bit 10. Named for what it is - the switch - rather than "off",
        which is what the output does."""
        assert page(stat_word=1024, vmon=0.0) == ["disabled"] * 8

    def test_disabled_wins_over_a_decaying_voltage(self, page):
        """Bit 10 is the board's own word about the switch. The page does not
        second-guess it from a monitor still reading volts on the way down."""
        assert page(stat_word=1024, vmon=-500.0) == ["disabled"] * 8

    def test_enabled_is_not_reported_as_off(self, page):
        """The regression, stated as such: these are different states and
        must not share a word."""
        enabled = page(stat_word=0, vmon=0.0)
        disabled = page(stat_word=1024, vmon=0.0)

        assert enabled != disabled
        assert "off" not in enabled


class TestFaultsAndGaps:
    def test_a_trip_is_shown(self, page):
        assert page(stat_word=(1 << 0) | (1 << 7), vmon=0.0) == ["TRIP"] * 8

    def test_a_fault_replaces_the_state_rather_than_hiding_behind_it(self, page):
        cells = page(stat_word=(1 << 0) | (1 << 12), vmon=-99.8)
        assert cells == ["INTERLOCK"] * 8
        assert "ON" not in cells

    def test_several_faults_are_all_shown(self, page):
        cells = page(stat_word=(1 << 7) | (1 << 3), vmon=0.0)
        assert cells == ["OVER_CURRENT, TRIP"] * 8

    def test_no_status_word_says_so_instead_of_guessing(self, page):
        """With no STAT the page must NOT fall back to inferring from the
        voltage. That inference is the bug this whole change removes."""
        assert page(stat_word=None, vmon=-99.8) == ["no reading"] * 8


def armed_warnings(html):
    """The red banners only — NOT the whole page.

    The explanatory text under the controls also contains the phrase
    "setpoint above zero", so searching the page matched the help rather than
    the warning and three of these tests failed on a page that was correct.
    The same mistake as searching an email for "ON" and matching its own
    glossary.
    """
    import re
    return re.findall(r'<div class="flash bad".*?</div>', html, re.S)


class TestTheArmedWarning:
    """"Flipping the enable would ramp straight to it" must only appear for a
    channel whose switch is actually OFF.

    It asked `is_energised` (bit 0) instead of `is_disabled` (bit 10), so a
    channel that was switched on but not energised — the ordinary state while
    loading a setpoint — was reported as dangerous to enable. It was already
    enabled. The warning told the operator to undo the thing they had just
    correctly done, about a hazard that did not exist.
    """

    def test_a_disabled_channel_with_a_setpoint_is_flagged(self, page_html):
        banners = armed_warnings(page_html(stat_word=1024, vmon=0.0,
                                           vset=-2250.0))

        assert any("setpoint above zero" in b for b in banners)

    def test_a_switched_on_channel_with_a_setpoint_is_NOT_flagged(self, page_html):
        """The bug. Switch on, not energised, setpoint loaded and waiting —
        which is exactly what §10a's procedure asks the operator to do."""
        assert armed_warnings(page_html(stat_word=0, vmon=0.0,
                                        vset=100.0)) == []

    def test_an_energised_channel_with_a_setpoint_is_NOT_flagged(self, page_html):
        assert armed_warnings(page_html(stat_word=1, vmon=100.0,
                                        vset=100.0)) == []

    def test_a_disabled_channel_at_zero_is_NOT_flagged(self, page_html):
        """Zero is the resting state the invariant guarantees; there is
        nothing to warn about."""
        assert armed_warnings(page_html(stat_word=1024, vmon=0.0,
                                        vset=0.0)) == []


class TestOnlyUsableBoxesAreOffered:
    """§10a: a channel whose switch is off cannot hold a setpoint above zero.

    Offering a box for one, letting it be filled, and then refusing it is how
    a single click on "load defaults" turned into a column of error messages.
    The box is disabled instead — and a disabled input is not submitted at
    all, so "apply" cannot carry a value that was always going to fail.
    """

    def test_a_switched_off_channel_gets_no_usable_box(self, page_html):
        html = page_html(stat_word=1024, vmon=0.0, vset=0.0)

        assert 'placeholder="disabled"' in html
        assert 'name="hv_cathode_vset"' not in html, (
            "a disabled channel's box would still be submitted")

    def test_a_switched_on_channel_gets_a_box(self, page_html):
        html = page_html(stat_word=0, vmon=0.0, vset=0.0)

        assert 'name="hv_cathode_vset"' in html

    def test_an_energised_channel_gets_a_box(self, page_html):
        """Changing a voltage while it is on is allowed — the same
        plan-and-apply flow, with the board ramping at its own rate."""
        html = page_html(stat_word=1, vmon=-2250.0, vset=-2250.0)

        assert 'name="hv_cathode_vset"' in html

    def test_a_default_is_only_offered_where_it_could_be_applied(self, page_html):
        """`load defaults` reads data-default, so a switched-off channel is
        skipped by construction rather than by a rule in the script."""
        off = page_html(stat_word=1024, vmon=0.0, vset=0.0)
        on = page_html(stat_word=0, vmon=0.0, vset=0.0)

        assert "data-default" not in off
        assert "data-default" in on

    def test_an_unreadable_status_offers_no_box_either(self, page_html):
        """Not knowing whether a channel can take a setpoint is not a reason
        to offer one."""
        html = page_html(stat_word=None, vmon=0.0, vset=0.0)

        assert 'placeholder="no status"' in html
        assert 'name="hv_cathode_vset"' not in html


class TestATrippedChannel:
    """A trip latches, and before 23 September 2026 the only way back was a
    power cycle. The page offers the clear where turning on would be."""

    TRIPPED = (1 << 7) | (1 << 5)        # TRIP and UNDER_VOLTAGE, output off

    def test_it_offers_clear_trip(self, page_html):
        html = page_html(stat_word=self.TRIPPED, vmon=0.0, vset=-2250.0)

        assert 'form="clr-hv_cathode_vset"' in html

    def test_it_does_not_offer_turn_on_instead(self, page_html):
        """ON is what does not work on a latched channel, and with the old
        setpoint still loaded it is the wrong thing to try."""
        html = page_html(stat_word=self.TRIPPED, vmon=0.0, vset=-2250.0)

        assert 'form="out-hv_cathode_vset"' not in html

    def test_a_healthy_channel_is_not_offered_it(self, page_html):
        html = page_html(stat_word=0, vmon=0.0, vset=0.0)

        assert 'form="clr-' not in html

    def test_the_confirmation_says_the_setpoint_goes_to_zero(self, page_html):
        html = page_html(stat_word=self.TRIPPED, vmon=0.0, vset=-2250.0)

        assert "Its setpoint is set to 0 V first" in html


@pytest.fixture
def tripped_page(monkeypatch):
    """/hv with the CAEN service's latched trip record on the anode.

    The status word is what the board really showed after the anode's trips
    on 23 September 2026 once the service had switched it off: nothing at
    all. No TRIP bit, no fault. The record is the only evidence.
    """
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app()
    state = app.state.system

    def render(record):
        def channel(name):
            if name.endswith("_stat"):
                return view(name, 0.0, "bits")
            if name.endswith("_imon"):
                return view(name, 0.0, "uA")
            return view(name, 0.0)

        monkeypatch.setattr(state, "channel", channel)
        monkeypatch.setattr(state, "hv_trip",
                            lambda n: record if n == "hv_anode_vset" else None)
        response = ui_client(app).get("/hv")
        assert response.status_code == 200
        return response.text

    return render


TRIP_RECORD = {
    "channel": "hv_anode_vset", "t": "2026-09-23T13:14:25.000Z",
    "cause": "the output collapsed: VMON +3.5 V against a setpoint of "
             "+2400.0 V", "imon_peak": 5.04, "vmon_at_imon_peak": 2044.7,
    "made_safe": True, "safe_detail": "setpoint zeroed and output switched off",
    "needs_power_cycle": False,
}


class TestATripTheBoardNeverFlagged:
    def test_the_row_says_TRIPPED(self, tripped_page):
        html = tripped_page(TRIP_RECORD)

        assert "TRIPPED" in html

    def test_clear_is_offered_and_turn_on_is_not(self, tripped_page):
        html = tripped_page(TRIP_RECORD)

        assert 'form="clr-hv_anode_vset"' in html
        assert 'form="out-hv_anode_vset"' not in html

    def test_the_banner_names_the_current_spike(self, tripped_page):
        html = tripped_page(TRIP_RECORD)

        assert "anode tripped at 13:14:25" in html
        assert "5.04 µA" in html

    def test_it_says_power_cycle_when_a_clear_did_not_help(self, tripped_page):
        html = tripped_page(dict(TRIP_RECORD, needs_power_cycle=True))

        assert "power-cycle hv_2" in html

    def test_no_trip_no_banner(self, tripped_page):
        html = tripped_page(None)

        assert "TRIPPED" not in html
        assert 'form="clr-' not in html


class TestMovingChannels:
    """14 October 2026: a channel on its way off showed "ON" with "turn off"
    offered again for its whole ramp down; on its way up it showed "ON"."""

    def test_ramping_down_is_switching_off_and_offers_nothing(self, page, page_html):
        word = (1 << 0) | (1 << 2)
        assert page(stat_word=word, vmon=-400.0) == ["switching off…"] * 8
        html = page_html(stat_word=word, vmon=-400.0, vset=-800.0)
        assert ">turn off<" not in html and "switching off…</button>" in html
        assert 'data-moving="yes"' in html

    def test_ramping_up_says_so_and_offers_turn_off_as_an_abort(self, page, page_html):
        word = (1 << 0) | (1 << 1)
        assert page(stat_word=word, vmon=-400.0) == ["ramping up…"] * 8
        html = page_html(stat_word=word, vmon=-400.0, vset=-800.0)
        assert ">turn off<" in html and "Stop the ramp and turn OFF" in html

    def test_a_turn_off_still_waiting_for_bit_0_is_switching_off(self, monkeypatch, page):
        """Bit 0 stays set some seconds after the ramp (29 September 2026):
        the CAEN service's record covers that."""
        from xams_sc.api import state as state_module
        monkeypatch.setattr(state_module.SystemState, "hv_switching",
                            lambda self, ch: {"wanted": "off", "since": "t"})
        assert page(stat_word=1, vmon=-0.5) == ["switching off…"] * 8

    def test_a_still_page_refreshes_slowly(self, page_html):
        assert 'data-moving=""' in page_html(stat_word=1, vmon=-800.0, vset=-800.0)

    def test_the_page_no_longer_reloads_itself_whole(self, page_html):
        html = page_html(stat_word=1, vmon=-800.0, vset=-800.0)
        assert "location.reload" not in html and "DOMParser" in html
