"""The alarm and digest emails. See DESIGN.md §11.

These are not tests of how the mail looks. They pin down the handful of things
that would make it actively misleading, because an email is read hours after
it was written, by somebody who cannot check it against the instrument.

The rules that matter:

  * a stale reading shows a dash, never its last number
  * colour is never the only signal
  * an alarm mail carries the plant around it, not just the channel
  * nothing renders as a broken template when data is missing
"""

from datetime import datetime, timezone

import pytest

from zoneinfo import ZoneInfo

from xams_sc.alarms import mail
from xams_sc.alarms.engine import _ViewLike

WHEN = datetime(2026, 9, 18, 3, 14, 0, tzinfo=timezone.utc)


def view(name, value, unit="C", healthy=True, quality="ok"):
    return _ViewLike(name, value, unit, healthy, quality)


def context(**kw):
    base = {"alarms": [], "readings": [], "services": [], "config_hash": "abc1234"}
    base.update(kw)
    return base


class TestTheAlarmMailCarriesItsContext:
    """An alarm that says only "tt302 is high" sends the reader to the web UI
    to find out whether anything else is wrong - at three in the morning, from
    a phone, which is exactly when they will not."""

    def test_the_channel_and_its_reading_are_in_the_subject_or_body(self):
        subject, html, text = mail.alarm(
            "pmain", "major", "hihi", 2.62, "main system pressure",
            context(), WHEN)

        assert "pmain" in subject
        assert "2.62" in html and "2.62" in text

    def test_other_active_alarms_are_listed(self):
        ctx = context(alarms=[{"channel": "tt302", "state": "minor",
                               "threshold": "high", "value": 31.4,
                               "description": "gas system"}])
        _, html, text = mail.alarm("pmain", "major", "hihi", 2.6, "", ctx, WHEN)

        assert "tt302" in html, "a second alarm was not mentioned"
        assert "tt302" in text

    def test_the_alarming_channel_is_not_repeated_in_also_in_alarm(self):
        ctx = context(alarms=[{"channel": "pmain", "state": "major",
                               "threshold": "hihi", "value": 2.6}])
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", ctx, WHEN)

        assert "Also in alarm" not in html

    def test_a_down_service_is_reported(self):
        ctx = context(services=[{"name": "cdaq", "expects_heartbeat": True,
                                 "healthy": False}])
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", ctx, WHEN)

        assert "cdaq" in html and "DOWN" in html

    def test_all_services_up_says_so_rather_than_saying_nothing(self):
        ctx = context(services=[{"name": "cdaq", "expects_heartbeat": True,
                                 "healthy": True}])
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", ctx, WHEN)

        assert "ALL RUNNING" in html


class TestAStaleReadingNeverShowsANumber:
    """The same rule as the web UI and the mimic (§8.2), and it matters more
    here: an email is read long after it was sent."""

    def test_a_stale_reading_shows_a_dash(self):
        ctx = context(readings=[("tt104", view("tt104", 49.2, healthy=False,
                                               quality="stale"))])
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", ctx, WHEN)

        assert "49.2" not in html, "a stale value was printed as though live"
        assert "mdash" in html or "stale" in html

    def test_a_missing_reading_does_not_render_as_none(self):
        ctx = context(readings=[("tt104", None)])
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", ctx, WHEN)

        assert "None" not in html

    def test_a_healthy_reading_shows_its_value_and_unit(self):
        ctx = context(readings=[("pmain", view("pmain", 1.485, "bar"))])
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", ctx, WHEN)

        assert "1.485" in html and "bar" in html


class TestColourIsNeverTheOnlySignal:
    """About one reader in twelve cannot reliably tell the two accent colours
    apart, and many clients strip styling entirely."""

    @pytest.mark.parametrize("state", ["minor", "major", "critical"])
    def test_the_severity_is_written_as_a_word(self, state):
        _, html, text = mail.alarm("pmain", state, "hihi", 2.6, "", context(), WHEN)

        assert state.upper() in html
        assert state.upper() in text

    def test_the_plain_text_part_stands_on_its_own(self):
        """Some people read mail as text, and a message with no usable text
        part also scores worse with spam filters - a poor way to lose an
        alarm."""
        ctx = context(readings=[("pmain", view("pmain", 2.62, "bar"))])
        _, _, text = mail.alarm("pmain", "major", "hihi", 2.62, "pressure",
                                ctx, WHEN)

        assert "<" not in text, "HTML leaked into the text part"
        for expected in ("MAJOR", "pmain", "2.62", "hihi"):
            assert expected in text


class TestEscaping:
    def test_a_description_with_markup_cannot_break_out(self):
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6,
                                "<script>alert(1)</script>", context(), WHEN)

        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestTheMailIsWellFormedEnoughForEmailClients:
    def test_no_external_stylesheet_or_script(self):
        """Gmail discards <style> blocks and every client blocks script."""
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", context(), WHEN)

        assert "<link" not in html
        assert "<script" not in html.replace("&lt;script&gt;", "")

    def test_layout_uses_tables_not_flex_or_grid(self):
        """Outlook renders with Word's engine, which supports neither."""
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.6, "", context(), WHEN)

        assert "<table" in html
        assert "display:flex" not in html and "display:grid" not in html

    def test_there_is_a_preheader(self):
        """Without one, clients scrape the first text they find for the inbox
        preview, which repeats the subject and tells the reader nothing."""
        _, html, _ = mail.alarm("pmain", "major", "hihi", 2.62, "", context(), WHEN)

        head = html[:html.index("<table")]
        assert "2.62" in head or "hihi" in head


class TestTheClockTheEmailIsReadIn:
    """Emails render local time; storage stays UTC. See DESIGN.md §9.3, §11.

    The logs and the HV page already show local time, so a UTC email was the
    one place in the system that answered "when" in a different clock — and
    it is the place read at three in the morning by somebody deciding whether
    to drive in. An hour out, in the dark, on a phone, is a real way to make
    the wrong call.

    An explicit zone is pinned here rather than the machine's, so the test
    means the same thing on the lab PC and on a laptop.
    """

    #: 12:30 UTC on a summer day is 14:30 in Amsterdam (CEST, UTC+2).
    SUMMER = datetime(2026, 7, 15, 12, 30, 45, tzinfo=timezone.utc)
    #: 12:30 UTC in January is 13:30 (CET, UTC+1) — the changeover must follow.
    WINTER = datetime(2026, 1, 15, 12, 30, 45, tzinfo=timezone.utc)

    @pytest.fixture(autouse=True)
    def amsterdam(self, monkeypatch):
        monkeypatch.setattr(mail, "DISPLAY_TZ", ZoneInfo("Europe/Amsterdam"))

    def test_an_alarm_is_stamped_in_local_time(self):
        _, html, text = mail.alarm("tt302", "major", "high", 92.4, "",
                                   context(), self.SUMMER)

        assert "14:30:45" in html and "12:30:45" not in html
        assert "14:30:45" in text

    def test_the_zone_is_named_so_the_time_is_not_ambiguous(self):
        _, html, text = mail.alarm("tt302", "major", "high", 92.4, "",
                                   context(), self.SUMMER)

        assert "CEST" in html
        assert "CEST" in text

    def test_winter_is_cet_not_a_fixed_two_hour_offset(self):
        """A hard-coded +2 would be an hour out for five months of the year."""
        _, html, _ = mail.alarm("tt302", "major", "high", 92.4, "",
                                context(), self.WINTER)

        assert "13:30:45" in html
        assert "CET" in html and "CEST" not in html

    def test_the_daily_report_is_local_too(self, webui):
        _, app, _ = webui
        _, html, text = mail.digest(app.state.system, self.SUMMER)

        assert "14:30" in html and "CEST" in html
        assert "14:30" in text

    def test_the_daily_report_carries_the_ln2_mass(self, webui):
        """24 October 2026: the LN2 left in the dewar is in the report."""
        _, app, _ = webui
        _, html, text = mail.digest(app.state.system, self.SUMMER)

        assert "LN2 in the dewar" in html
        assert "LN2 in the dewar" in text

    def test_the_daily_report_carries_the_vacuum(self, webui):
        """24 October 2026: P105, the insulation vacuum, is in the report."""
        _, app, _ = webui
        _, html, text = mail.digest(app.state.system, self.SUMMER)

        assert "Vacuum (P105)" in html
        assert "vacuum (P105)" in text

    def test_a_naive_timestamp_is_taken_as_utc_rather_than_refused(self):
        """`model.iso` raises on a naive datetime, which is right for storage.
        Refusing to render an ALARM email over a missing tzinfo is not."""
        naive = datetime(2026, 7, 15, 12, 30, 45)

        _, html, _ = mail.alarm("tt302", "major", "high", 92.4, "",
                                context(), naive)

        assert "14:30:45" in html
