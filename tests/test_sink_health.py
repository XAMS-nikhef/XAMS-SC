"""Saying so when data is thrown away. See DESIGN.md §2.1, principle 4.

"Fail loudly, never silently" is the principle that shows up most often in
this code, and this is the one place it is about the system's OWN data rather
than an instrument's reading. It was also the place that obeyed it least:
`PgWriter` counted every row it discarded when the backlog hit `max_pending`,
and the only thing that read the counter logged it at DEBUG. The default
level is INFO, so the system's stated worst case — measurements thrown away —
was invisible in normal running.

`Bus.dropped` was worse than invisible: `_flush` zeroed it, so by the time
anything asked, the answer was almost always 0.

What is pinned here is the difference between loss and delay. A full backlog
is a delay — the rows are in memory and will be written when the database
returns — and rates a warning. A discarded row is loss, is unrecoverable, and
must be an ERROR and a `degraded` service state, so it is visible on /status
to somebody who was not reading the log at the time.

Reported on change, not on every cycle: a service that logs the same error
every five seconds for a week teaches people to filter it out, which costs
the next one.
"""

from __future__ import annotations

import logging
import threading

import pytest

from xams_sc.api.state import SystemState
from xams_sc.model import ServiceState
from xams_sc.sinks.__main__ import SinkHealth


class StubPg:
    """Only what SinkHealth reads."""

    def __init__(self, dropped=0, pending=0, max_pending=100_000):
        self.dropped = dropped
        self.pending = pending
        self.max_pending = max_pending

    @property
    def stats(self):
        return {"written": 0, "pending": self.pending, "dropped": self.dropped}


@pytest.fixture
def pg():
    return StubPg()


@pytest.fixture
def health(bus, pg):
    return SinkHealth(bus, pg, remind_after_s=300.0, backlog_warn=10_000)


def states(bus):
    return [state for service, state in bus.states if service == "sinks"]


class TestNothingWrong:
    def test_a_quiet_cycle_says_nothing(self, health, bus, caplog):
        with caplog.at_level(logging.INFO):
            health.check(now=0.0)

        assert caplog.records == []
        assert bus.states == []
        assert health.degraded is False

    def test_a_backlog_that_is_merely_large_is_a_warning_not_a_loss(
            self, health, bus, pg, caplog):
        """The rows are in memory and will be written when the database
        returns. Nothing is lost yet."""
        pg.pending = 50_000

        with caplog.at_level(logging.WARNING):
            health.check(now=0.0)

        assert "nothing is lost yet" in caplog.text
        assert states(bus) == [], "a delay is not a degraded service"
        assert health.degraded is False

    def test_the_backlog_warning_is_not_repeated_every_cycle(
            self, health, pg, caplog):
        pg.pending = 50_000

        with caplog.at_level(logging.WARNING):
            for tick in range(10):
                health.check(now=tick * 5.0)

        assert len([r for r in caplog.records if "nothing is lost" in r.message]) == 1

    def test_a_drained_backlog_is_noted_and_rearms_the_warning(
            self, health, pg, caplog):
        pg.pending = 50_000
        health.check(now=0.0)
        caplog.clear()

        with caplog.at_level(logging.INFO):
            pg.pending = 0
            health.check(now=5.0)
            pg.pending = 50_000
            health.check(now=10.0)

        assert "backlog has drained" in caplog.text
        # Warned again, because this is a new episode rather than the same
        # one continuing. A warning that fires once per process would go
        # unsaid the second time it mattered.
        assert len([r for r in caplog.records if "nothing is lost" in r.message]) == 1


class TestRowsDiscarded:
    def test_a_discarded_row_is_an_error_and_degrades_the_service(
            self, health, bus, pg, caplog):
        pg.dropped = 17

        with caplog.at_level(logging.ERROR):
            health.check(now=0.0)

        assert "DATA LOST" in caplog.text
        assert "17 measurement row(s) discarded" in caplog.text
        assert states(bus) == [ServiceState.DEGRADED]
        assert health.degraded is True

    def test_the_same_count_again_is_not_reported_again(
            self, health, bus, pg, caplog):
        """A service that logs the same error every five seconds for a week
        teaches people to filter it out.

        The backlog is held full so the watcher stays degraded; a drained
        backlog would legitimately end the episode, which is the next test.
        """
        pg.dropped, pg.pending = 17, 100_000
        health.check(now=0.0)
        caplog.clear()

        with caplog.at_level(logging.ERROR):
            for tick in range(1, 10):           # well inside the reminder window
                health.check(now=tick * 5.0)

        assert caplog.records == []
        assert states(bus) == [ServiceState.DEGRADED], "state republished"

    def test_further_loss_is_reported_with_both_the_delta_and_the_total(
            self, health, pg, caplog):
        pg.dropped = 17
        health.check(now=0.0)

        with caplog.at_level(logging.ERROR):
            pg.dropped = 20
            health.check(now=5.0)

        assert "3 measurement row(s)" in caplog.text
        assert "20 since this service started" in caplog.text

    def test_broker_buffer_overflow_counts_as_loss_too(self, bus, pg, caplog):
        health = SinkHealth(bus, pg)
        bus.dropped = 42

        with caplog.at_level(logging.ERROR):
            health.check(now=0.0)

        assert "42 message(s) lost to the broker outage buffer" in caplog.text
        assert states(bus) == [ServiceState.DEGRADED]

    def test_both_kinds_of_loss_are_reported_separately(self, bus, pg, caplog):
        health = SinkHealth(bus, pg)
        pg.dropped, bus.dropped = 5, 7

        with caplog.at_level(logging.ERROR):
            health.check(now=0.0)

        assert len([r for r in caplog.records if "DATA LOST" in r.message]) == 2


class TestRecovery:
    def test_it_recovers_only_once_the_backlog_has_drained(
            self, health, bus, pg, caplog):
        """Otherwise the next full buffer reads as a fresh problem rather than
        the same one continuing."""
        pg.dropped, pg.pending = 17, 100_000
        health.check(now=0.0)

        pg.pending = 100_000
        health.check(now=5.0)
        assert health.degraded is True, "recovered while still backed up"

        with caplog.at_level(logging.WARNING):
            pg.pending = 0
            health.check(now=10.0)

        assert health.degraded is False
        assert states(bus) == [ServiceState.DEGRADED, ServiceState.RUNNING]
        assert "17 row(s) and 0 message(s) were lost in total" in caplog.text

    def test_a_long_outage_is_reminded_about_but_not_every_cycle(
            self, health, pg, caplog):
        pg.dropped, pg.pending = 17, 100_000
        health.check(now=0.0)

        caplog.clear()
        with caplog.at_level(logging.ERROR):
            for tick in range(1, 61):        # five minutes at 5 s
                health.check(now=tick * 5.0)

        reminders = [r for r in caplog.records if "STILL DEGRADED" in r.message]
        assert len(reminders) == 1, "a reminder per cycle is noise, not signal"
        assert "17 row(s) lost so far" in reminders[0].message


class TestWithoutADatabase:
    def test_it_works_when_only_the_archive_is_running(self, bus, caplog):
        """No DSN is a normal state: the archive runs without a database."""
        health = SinkHealth(bus, pg=None)

        with caplog.at_level(logging.INFO):
            health.check(now=0.0)

        assert caplog.records == []
        assert health.degraded is False

    def test_broker_loss_is_still_reported_without_a_database(self, bus, caplog):
        health = SinkHealth(bus, pg=None)
        bus.dropped = 3

        with caplog.at_level(logging.ERROR):
            health.check(now=0.0)

        assert "3 message(s) lost" in caplog.text
        assert states(bus) == [ServiceState.DEGRADED]


class TestTheRunSummary:
    """The last line of a run should name a loss, even one from hours ago.

    An ERROR at 03:00 has scrolled off by the time anybody looks, and "how
    was last night" is the question actually asked.
    """

    def test_it_counts_both_kinds(self, bus, pg):
        health = SinkHealth(bus, pg)
        pg.dropped, bus.dropped = 17, 3
        health.check(now=0.0)

        assert health.lost_rows == 17
        assert health.lost_messages == 3

    def test_a_clean_run_has_nothing_to_report(self, health):
        health.check(now=0.0)

        assert health.lost_rows == 0 and health.lost_messages == 0
        assert health.degraded is False


def service_rows(heartbeats, states):
    """`SystemState.services()` with only the two dicts it reads.

    Built with __new__ rather than a constructed SystemState: the real one
    connects a bus, starts threads and loads the configuration, and none of
    that is what these are about.
    """
    s = SystemState.__new__(SystemState)
    s._lock = threading.Lock()
    s._heartbeats = heartbeats
    s._states = states
    return {row["name"]: row for row in s.services()}


class TestTheHeartbeat:
    """`sinks` publishes one now, like every other service.

    Without it the only evidence the process was alive was a RETAINED
    `running` that outlived it: a sinks that hangs rather than exits publishes
    no will, so the page went on saying `running` indefinitely. The archive is
    the last thing that should be able to die quietly.
    """

    def test_a_beating_sinks_is_healthy(self):
        rows = service_rows({"sinks": 3.0}, {"sinks": "running"})

        assert rows["sinks"]["healthy"] is True

    def test_a_silent_sinks_is_not_healthy_whatever_it_last_said(self):
        rows = service_rows({}, {"sinks": "running"})

        assert rows["sinks"]["healthy"] is False, \
            "a retained `running` outlived the process and nothing noticed"

    def test_a_stale_heartbeat_is_not_healthy(self):
        """Sixty seconds, the same window every other service is held to."""
        rows = service_rows({"sinks": 3600.0}, {"sinks": "running"})

        assert rows["sinks"]["healthy"] is False

    def test_no_service_is_exempt_from_reporting(self):
        rows = service_rows({}, {})

        assert set(rows) == {"cdaq", "caen", "lakeshore", "ups", "turbo", "rga", "derived",
                             "alarms", "sinks"}
        assert all(r["healthy"] is False for r in rows.values())
        assert not any("expects_heartbeat" in r for r in rows.values()), \
            "the exemption is gone; nothing should still be asking for it"
