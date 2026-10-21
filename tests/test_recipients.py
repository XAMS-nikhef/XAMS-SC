"""The Alarms page and the recipient list. See DESIGN.md §4.4, §8.1, §11.

The alarm chain is the least visible part of this system and the most
consequential, and this page is where it becomes visible: what has fired, what
*would* fire, and who is told.

Two things here are worth more than the rest:

**Nobody on the alarm list is a WARNING, not a refusal.** Turning everyone off
may be exactly what somebody means during an intervention. It is said loudly
and allowed.

**Alarms and the daily report are two lists.** A tick in one is not a tick in
the other, and a file written before the split - `enabled:` alone - is read as
both, because that is what it meant when it was written.

**A recipient with neither an email nor a phone is WARNED ABOUT, not refused.**
They would sit on the list looking notified and hear nothing, which is the
silent failure this whole subsystem exists to avoid - so it is said, loudly and
by name. It is not a reason to refuse the save: a blank field is somebody
mid-edit far more often than it is a mistake, and one of them must not hold the
rest of the list hostage.
"""

import json

import yaml

from xams_sc import config as config_module
from xams_sc.model import iso, utcnow
from xams_sc.config import (read_recipients, recipient_warnings,
                            validate_recipients, wants_alarms, wants_daily,
                            write_recipients)

REPO_CONFIG = config_module.ROOT / "config"


def rows(people, **extra):
    """The form a browser would post for this list, plus the spare row."""
    data = {}
    for i, p in enumerate(people):
        data[f"name-{i}"] = p.get("name", "")
        data[f"email-{i}"] = p.get("email", "")
        data[f"phone-{i}"] = p.get("phone", "")
        # An unticked checkbox posts NOTHING, which is how both columns say
        # "not on this list" - there is no value to send for "off".
        if p.get("alarms"):
            data[f"alarms-{i}"] = "on"
        if p.get("daily"):
            data[f"daily-{i}"] = "on"
    data[f"name-{len(people)}"] = ""
    data[f"email-{len(people)}"] = ""
    data[f"phone-{len(people)}"] = ""
    data.update(extra)
    return data


# ------------------------------------------------------------- validation

def test_blank_phone_is_allowed():
    """An empty phone MEANS "do not SMS"; it is not a missing number."""
    assert validate_recipients(
        [{"name": "A", "email": "a@nikhef.nl", "phone": "", "alarms": True}]
    ) == []


def test_neither_email_nor_phone_is_allowed_but_warned_about():
    person = [{"name": "A", "email": "", "phone": "", "alarms": True}]
    assert validate_recipients(person) == []
    warnings = recipient_warnings(person)
    assert len(warnings) == 1
    assert "hear nothing" in warnings[0]
    assert "A" in warnings[0]


def test_a_reachable_person_warns_about_nothing():
    """A blank phone alone is silent, not a warning: it MEANS "do not SMS"."""
    assert recipient_warnings(
        [{"name": "A", "email": "a@nikhef.nl", "phone": ""}]) == []
    assert recipient_warnings(
        [{"name": "B", "email": "", "phone": "+31612345678"}]) == []


def test_phone_needs_a_country_code():
    problems = validate_recipients(
        [{"name": "A", "email": "a@x.nl", "phone": "0600000000"}])
    assert any("country code" in p for p in problems)


def test_malformed_email_is_refused():
    problems = validate_recipients([{"name": "A", "email": "a.nikhef.nl"}])
    assert any("not an email" in p for p in problems)


def test_missing_name_is_refused():
    problems = validate_recipients([{"name": "", "email": "a@x.nl"}])
    assert any("no name" in p for p in problems)


def test_duplicate_names_are_refused():
    problems = validate_recipients([
        {"name": "Ada", "email": "a@x.nl"},
        {"name": "ada", "email": "b@x.nl"},
    ])
    assert any("listed twice" in p for p in problems)


def test_the_real_list_is_valid():
    """The list actually in the repository must pass its own checks."""
    assert validate_recipients(read_recipients(REPO_CONFIG)) == []


# ------------------------------------------------------------ the file

def test_round_trip_keeps_the_header(config_dir):
    write_recipients([{"name": "Ada", "email": "a@x.nl", "phone": "",
                       "alarms": True, "daily": False}], "apc", config_dir)
    text = (config_dir / "recipients.yaml").read_text()
    assert text.startswith("# Who receives alarm notifications")
    assert "do not SMS this person" in text
    assert read_recipients(config_dir) == [
        {"name": "Ada", "email": "a@x.nl", "phone": "",
         "alarms": True, "daily": False}]


def test_missing_file_is_not_an_error(tmp_path):
    assert read_recipients(tmp_path) == []


# ------------------------------------------------------------- the page

def test_page_lists_everyone(webui):
    http, _, _ = webui
    text = http.get("/alarms").text
    assert "Alice Example" in text
    assert "alice.example@example.org" in text


def test_nav_carries_alarms(webui):
    http, _, _ = webui
    assert 'href="/alarms"' in http.get("/").text


def test_limits_unknown_when_engine_is_silent(webui):
    """Nothing published means unknown, never "no thresholds" (§12)."""
    http, _, _ = webui
    text = http.get("/alarms").text
    assert "Unknown" in text
    assert "has not published its" in text


def test_limits_shown_when_published(webui):
    http, app, bus = webui
    bus.deliver(
        "xams/status/limits",
        '{"pmain":{"high":2.1,"hihi":2.5,"low":0.9}}')
    text = http.get("/alarms").text
    assert "2.1" in text and "2.5" in text and "0.9" in text
    assert "pmain" in text


def test_adding_somebody(webui, config_dir):
    http, _, _ = webui
    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Ada Lovelace"
    data[f"email-{len(people)}"] = "ada@nikhef.nl"
    data[f"alarms-{len(people)}"] = "on"
    data[f"daily-{len(people)}"] = "on"
    response = http.post("/alarms/recipients", data=dict(data, by="apc"),
                         follow_redirects=False)
    assert response.status_code == 303
    assert "saved" in response.headers["location"]
    saved = read_recipients(config_dir)
    assert any(p["name"] == "Ada Lovelace" and p["alarms"] and p["daily"]
               for p in saved)


def test_disabling_keeps_the_number(webui, config_dir):
    """Off both lists is not removal: the number survives a holiday (§4.4)."""
    http, _, _ = webui
    people = read_recipients(config_dir)
    for p in people:
        if p["name"] == "Bob Example":
            p["alarms"] = p["daily"] = False
    http.post("/alarms/recipients", data=dict(rows(people), by="apc"),
              follow_redirects=False)
    saved = {p["name"]: p for p in read_recipients(config_dir)}
    assert saved["Bob Example"]["alarms"] is False
    assert saved["Bob Example"]["phone"] == "+31600000002"


def test_removing_somebody(webui, config_dir):
    http, _, _ = webui
    people = read_recipients(config_dir)
    index = [i for i, p in enumerate(people)
             if p["name"] == "Carol Example"][0]
    data = rows(people)
    data[f"remove-{index}"] = "on"
    http.post("/alarms/recipients", data=dict(data, by="apc"),
              follow_redirects=False)
    saved = read_recipients(config_dir)
    assert all(p["name"] != "Carol Example" for p in saved)
    assert len(saved) == len(people) - 1


def test_blank_phone_survives_a_save(webui, config_dir):
    """The thing the user asked to keep: `phone: ""` stays an empty string."""
    http, _, _ = webui
    http.post("/alarms/recipients",
              data=dict(rows(read_recipients(config_dir)), by="apc"),
              follow_redirects=False)
    body = yaml.safe_load((config_dir / "recipients.yaml").read_text())
    dan = [p for p in body["recipients"]
                if p["name"] == "Dan Example"][0]
    assert dan["phone"] == ""


def test_a_bad_row_refuses_the_whole_save(webui, config_dir):
    """All or nothing: a half-saved list is a list nobody chose."""
    http, _, _ = webui
    before = (config_dir / "recipients.yaml").read_text()
    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Nobody"
    data[f"email-{len(people)}"] = "not-an-address"
    data[f"alarms-{len(people)}"] = "on"
    response = http.post("/alarms/recipients", data=dict(data, by="apc"),
                         follow_redirects=False)
    assert "error" in response.headers["location"]
    assert (config_dir / "recipients.yaml").read_text() == before


def test_a_contactless_row_saves_with_a_warning(webui, config_dir):
    """Somebody with no email and no phone is saved, loudly.

    Refusing it meant one half-filled row - a name typed while the number is
    looked up - refused every other change on the page with it.
    """
    http, _, _ = webui
    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Nobody"      # no email, no phone
    data[f"alarms-{len(people)}"] = "on"
    response = http.post("/alarms/recipients", data=dict(data, by="apc"),
                         follow_redirects=False)
    where = response.headers["location"]
    assert "saved" in where
    assert "WARNING" in where and "hear%20nothing" in where
    assert any(p["name"] == "Nobody" for p in read_recipients(config_dir))


def test_the_page_marks_who_hears_nothing(webui, config_dir):
    """Marked where the blank is, not only in a banner that scrolls away."""
    http, _, _ = webui
    people = read_recipients(config_dir)
    people.append({"name": "Nobody", "email": "", "phone": "",
                   "alarms": True, "daily": False})
    write_recipients(people, "apc", config_dir)
    assert "hears-nothing" in http.get("/alarms").text


def test_the_boxes_are_not_browser_default_white(webui):
    """The inputs read as the dark panel they sit in, not as four lamps."""
    http, _, _ = webui
    text = http.get("/alarms").text
    assert 'class="recip-box' in text
    assert 'style="width:95%"' not in text


def test_empty_spare_row_is_not_an_error(webui, config_dir):
    """Saving without using the spare row must simply work."""
    http, _, _ = webui
    before = read_recipients(config_dir)
    response = http.post("/alarms/recipients",
                         data=dict(rows(before), by="apc"),
                         follow_redirects=False)
    assert "saved" in response.headers["location"]
    # Against the list that was there, not a number: this fixture copies the
    # REAL recipients.yaml, and hard-coding its length made adding a colleague
    # break the test suite.
    assert len(read_recipients(config_dir)) == len(before)


def test_disabling_everyone_warns_but_is_allowed(webui, config_dir):
    http, _, _ = webui
    people = read_recipients(config_dir)
    for p in people:
        p["alarms"] = False
    response = http.post("/alarms/recipients",
                         data=dict(rows(people), by="apc"),
                         follow_redirects=False)
    assert "saved" in response.headers["location"]
    assert "WARNING" in response.headers["location"]
    assert all(not p["alarms"] for p in read_recipients(config_dir))


def test_the_page_says_so_when_nobody_is_on_the_alarm_list(webui, config_dir):
    http, _, _ = webui
    people = read_recipients(config_dir)
    for p in people:
        p["alarms"] = False
    write_recipients(people, "apc", config_dir)
    assert "alarms reach nobody" in http.get("/alarms").text


def test_changes_are_audited(webui, config_dir):
    http, _, bus = webui
    people = read_recipients(config_dir)
    for p in people:
        if p["name"] == "Bob Example":
            p["alarms"] = False
    http.post("/alarms/recipients", data=dict(rows(people), by="apc"),
              follow_redirects=False)
    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    # "no alarms", not "disabled": which of the two lists somebody left is
    # what the trail has to say now that there are two of them.
    assert any('"action":"recipients"' in a and "Bob Example" in a
               and "no alarms" in a and '"actor":"apc"' in a for a in audits)


def test_removal_is_audited_with_what_was_lost(webui, config_dir):
    """The file no longer mentions them, so the trail must (§4.4)."""
    http, _, bus = webui
    people = read_recipients(config_dir)
    index = [i for i, p in enumerate(people)
             if p["name"] == "Carol Example"][0]
    data = rows(people)
    data[f"remove-{index}"] = "on"
    http.post("/alarms/recipients", data=dict(data, by="apc"),
              follow_redirects=False)
    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    line = [a for a in audits if "Carol Example" in a]
    assert line, audits
    assert '"new":"removed"' in line[0]
    # Their address as the file actually had it. Hard-coding one meant the
    # test failed when they changed jobs, which is not what it is watching.
    assert people[index]["email"] in line[0]


def test_an_unchanged_save_is_not_audited(webui, config_dir):
    http, _, bus = webui
    http.post("/alarms/recipients",
              data=dict(rows(read_recipients(config_dir)), by="apc"),
              follow_redirects=False)
    assert [p for topic, p in bus.published if topic == "xams/audit"] == []


def test_the_reload_notices_a_ticked_checkbox(webui):
    """The auto-reload must not discard an unsent tick.

    A checkbox's `value` is "on" whether or not it is ticked, so comparing
    values alone cannot see one. This page is the first with checkboxes worth
    losing — **Notify** and **Remove** — and losing a *Remove* tick silently
    is the same class of bug as the HV boxes that reverted after ten seconds.
    """
    http, _, _ = webui
    script = http.get("/alarms").text
    assert "defaultChecked" in script


# ------------------------------------------------ the two lists (§4.4)
#
# `alarms` is an SMS at 3am, `daily` is an email over breakfast, and somebody
# may reasonably want either without the other. What is tested here is that
# each column reaches its own list and nothing else - the failure worth
# catching is a tick in the quiet column putting somebody back on the loud
# one.

def test_the_two_ticks_are_independent(webui, config_dir):
    http, _, _ = webui
    people = read_recipients(config_dir)
    for p in people:
        p["alarms"], p["daily"] = (p["name"] == "Alice Example",
                                   p["name"] == "Bob Example")
    http.post("/alarms/recipients", data=dict(rows(people), by="apc"),
              follow_redirects=False)

    saved = {p["name"]: p for p in read_recipients(config_dir)}
    alice, bob = saved["Alice Example"], saved["Bob Example"]
    assert alice["alarms"] and not alice["daily"]
    assert bob["daily"] and not bob["alarms"]


def test_only_the_alarm_list_is_notified_of_an_alarm():
    """The whole point: the morning-report list is not woken up (§4.4)."""
    from xams_sc.alarms.notify import Notifier
    people = [{"name": "Loud", "email": "loud@x.nl", "alarms": True,
               "daily": False},
              {"name": "Quiet", "email": "quiet@x.nl", "alarms": False,
               "daily": True}]
    sent = []
    notifier = Notifier({}, lambda: people)
    notifier.send_email = (
        lambda address, *a, **kw: sent.append(address) or True)

    notifier.send("test", ["email"])
    assert sent == ["loud@x.nl"]


def test_a_file_from_before_the_split_is_read_as_both(config_dir):
    """`enabled: true` meant both, and must not silently become neither."""
    (config_dir / "recipients.yaml").write_text(
        "recipients:\n"
        "  - name: Ada\n"
        "    email: a@x.nl\n"
        "    enabled: true\n",
        encoding="utf-8")
    person = read_recipients(config_dir)[0]
    assert person["alarms"] and person["daily"]
    assert wants_alarms({"enabled": True}) and wants_daily({"enabled": True})
    # And the new keys win where a row carries both.
    assert not wants_daily({"enabled": True, "daily": False})


def test_the_legacy_key_is_written_for_an_engine_not_yet_restarted(config_dir):
    """A save must not silence an alarm service still running the old code."""
    write_recipients([{"name": "Ada", "email": "a@x.nl", "alarms": True,
                       "daily": False}], "apc", config_dir)
    row = yaml.safe_load(
        (config_dir / "recipients.yaml").read_text())["recipients"][0]
    assert row["enabled"] is True


def test_the_page_offers_both_columns(webui):
    http, _, _ = webui
    text = http.get("/alarms").text
    assert "alarms-0" in text and "daily-0" in text
    assert "daily report" in text


def test_the_daily_list_needs_an_email_and_says_so():
    """The report is email only: a phone alone is another way to hear
    nothing, and it is marked where the blank is."""
    notes = recipient_warnings(
        [{"name": "A", "email": "", "phone": "+31612345678", "daily": True}])
    assert len(notes) == 1 and "daily report" in notes[0]


# ------------------------------------------------- the master switch (§4.4a)
#
# One button that stops every alarm reaching anybody. It exists because the
# slow control runs when the plant does not, and the ways of getting that
# quiet WITHOUT a switch - stop the service, empty the recipient list, widen
# a threshold - all leave a system that looks armed and is not.
#
# So what is tested here is mostly the saying-so.

def notify_status(bus, enabled, by="apc", at="2026-09-20T10:00:00Z"):
    """What the alarm engine publishes, retained, about the switch."""
    bus.deliver(
        "xams/status/notify",
        json.dumps({"enabled": enabled, "by": by, "at": at}))


def test_unknown_until_the_engine_says(webui):
    """Nothing published is unknown, never "on" (§12)."""
    http, _, _ = webui
    text = http.get("/alarms").text
    assert "has not said whether it would notify" in text
    assert "disable all alarms" not in text


def test_the_button_is_offered_when_alarms_are_on(webui):
    http, _, bus = webui
    notify_status(bus, True)
    text = http.get("/alarms").text
    assert "disable all alarms" in text
    assert "Alarms are notifying" in text


def test_the_alarms_page_shouts_when_they_are_off(webui):
    http, _, bus = webui
    notify_status(bus, False)
    text = http.get("/alarms").text
    assert "ALARMS ARE DISABLED" in text
    assert "enable all alarms" in text
    assert "apc" in text


def test_the_overview_says_alarms_disabled_not_running(webui):
    """The services row must say what the engine is DOING (§4.4a)."""
    http, _, bus = webui
    bus.deliver("xams/status/alarms/state", "running")
    notify_status(bus, False)
    # /system, not /: the services table moved there when the P&ID became the
    # landing page. The badge still says it on every page; this is about the
    # row that says what the engine is DOING.
    text = http.get("/system").text
    assert "alarms disabled" in text
    assert "ALARMS ARE DISABLED" in text


def test_the_overview_says_running_when_they_are_on(webui):
    http, _, bus = webui
    bus.deliver("xams/status/alarms/state", "running")
    notify_status(bus, True)
    text = http.get("/system").text
    assert "alarms disabled" not in text


def all_well(app, bus):
    """Every service beating and every channel fresh.

    The badge reports the worst thing it can find, so a fixture where nothing
    has reported yet says "N CHANNEL(S) NOT OK" and never gets as far as the
    switch. To test what the badge says about the switch, everything else has
    to be genuinely well first.
    """
    now = iso(utcnow())
    # All seven, including `sinks` and `alarms`. They were absent here because
    # they used to publish no heartbeat, which is exactly what made the page
    # unable to show the archive or the notifier as broken.
    for service in ("cdaq", "caen", "lakeshore", "ups", "turbo", "rga", "scale",
                    "derived", "sinks", "alarms"):
        bus.deliver(
            f"xams/status/{service}/heartbeat", now)
    for ch in app.state.system.config.enabled_channels():
        bus.deliver(
            f"xams/meas/{ch.name}",
            json.dumps({"t": now, "ch": ch.name, "v": 1.0,
                        "u": ch.unit, "q": "ok"}))


def test_the_badge_never_says_all_ok_with_alarms_off(webui):
    """"ALL OK" on a system nobody would be told about is the sentence this
    whole subsystem exists to prevent."""
    http, app, bus = webui
    all_well(app, bus)
    assert http.get("/healthz").text == "ALL OK"      # the fixture is sound

    notify_status(bus, False)
    assert http.get("/healthz").text == "OK — ALARMS DISABLED"
    # The health page says it in its own words, above everything else on it.
    assert "ALARMS ARE DISABLED" in http.get("/system").text
    # And the header badge carries it on EVERY page, including the P&ID that
    # the site now opens on — which is the one somebody is actually looking
    # at when the alarms are off.
    assert "OK &mdash; ALARMS DISABLED" in http.get("/").text \
        or "OK — ALARMS DISABLED" in http.get("/").text


def test_a_live_alarm_still_outranks_the_switch(webui):
    http, _, bus = webui
    notify_status(bus, False)
    bus.deliver(
        "xams/alarm/pmain",
        '{"state":"major","threshold":"hihi","value":2.5}')
    assert "MAJOR ALARM" in http.get("/healthz").text


def test_api_state_carries_the_switch(webui):
    http, _, bus = webui
    notify_status(bus, False)
    body = http.get("/api/state").json()
    assert body["notifications"] == {"known": True, "enabled": False,
                                     "by": "apc", "at": "2026-09-20T10:00:00Z"}


def test_api_state_survives_a_channel_that_has_never_reported(webui):
    """One silent channel must not take the whole endpoint down.

    A channel with no reading carries an age of infinity, which strict JSON
    cannot write - so `/api/state` raised and returned 500 for EVERY caller
    the moment any one channel went quiet. The machine-readable view of the
    system failed exactly when part of the system had stopped talking, which
    is the same failure the pages are built to avoid. `null` is how a service
    with no heartbeat already reports the same thing.
    """
    http, app, bus = webui
    silent = list(app.state.system.config.enabled_channels())[0].name

    body = http.get("/api/state").json()          # nothing has reported yet
    ages = {c["name"]: c["age_s"] for c in body["channels"]}
    assert ages[silent] is None
    assert all(age is None for age in ages.values())

    all_well(app, bus)
    body = http.get("/api/state").json()
    assert all(isinstance(c["age_s"], float) for c in body["channels"])


def test_switching_it_off_is_a_command_and_is_audited(webui):
    """The UI asks over the bus and reports what came back - it does not
    decide for itself that the alarms are off."""
    http, _, bus = webui

    def answer(topic, payload):
        # Only the command. The handler audits on the same bus, and a fake
        # service that tried to acknowledge the audit line died on it.
        if topic != "xams/cmd/alarms/notify":
            return
        request = json.loads(payload)
        bus.deliver(
            "xams/ack/alarms/notify",
            json.dumps({"ok": True, "enabled": request["enabled"],
                        "was": True, "active": ["pmain"],
                        "by": request["by"]}))

    bus.on_publish = answer
    response = http.post("/alarms/notify", data={"enabled": "0", "by": "apc"},
                         follow_redirects=False)
    assert response.status_code == 303
    assert "nobody%20is%20told" in response.headers["location"]

    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert any('"action":"alarm_notifications"' in a and '"new":"off"' in a
               and '"actor":"apc"' in a for a in audits)


def test_a_silent_engine_is_reported_as_a_refusal(webui):
    """No ack means it did not happen, and the page must not claim it did."""
    http, app, _ = webui
    app.state.system.command = lambda *a, **kw: {
        "ok": False, "reason": "the alarms service did not answer"}
    response = http.post("/alarms/notify", data={"enabled": "0", "by": "apc"},
                         follow_redirects=False)
    assert "error" in response.headers["location"]


# ------------------------------------------- the "acting as" suggestions

def test_the_operator_box_offers_the_recipients(webui):
    """The names are typed a few times a day and end up in the audit trail,
    where `alice`, `A Example` and `Alice Example` are three people."""
    http, _, _ = webui
    text = http.get("/").text
    assert 'list="operators"' in text
    assert '<option value="Bob Example">' in text


def test_the_suggestions_are_on_every_page(webui):
    http, _, _ = webui
    for path in ("/", "/status", "/hv", "/alarms", "/logs"):
        assert '<option value="Bob Example">' in http.get(path).text, (
            "%s offers no names under 'acting as'" % path)


def test_a_disabled_recipient_is_still_offered(webui, config_dir):
    """Disabled means "do not notify me", which is usually somebody away -
    exactly when a colleague is the one at the keyboard."""
    http, _, _ = webui
    people = read_recipients(config_dir)
    for p in people:
        if p["name"] == "Bob Example":
            p["alarms"] = p["daily"] = False
    http.post("/alarms/recipients", data=dict(rows(people), by="apc"),
              follow_redirects=False)

    assert '<option value="Bob Example">' in http.get("/").text


def test_a_name_that_is_not_offered_is_still_accepted(webui, config_dir):
    """The heart of it being a datalist and not a select. A student on shift
    is not an alarm recipient, and the alternative to typing their own name
    is picking a colleague's - which puts the WRONG name in the audit trail,
    worse than no name at all."""
    http, _, bus = webui
    http.post("/operator", data={"operator": "Visiting Student"})

    people = read_recipients(config_dir)
    people[0]["phone"] = "+31000000000"
    http.post("/alarms/recipients", data=rows(people),   # no `by` on the form
              follow_redirects=False)

    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert any('"actor":"Visiting Student"' in a for a in audits)


def test_the_names_follow_an_edit_without_a_restart(webui, config_dir):
    """The list is cached on the file's timestamp, because it renders in the
    header of pages that reload every ten seconds. A cache that outlived an
    edit made on /alarms would be a stale list nobody could explain."""
    http, _, _ = webui
    assert "Ada Lovelace" not in http.get("/").text

    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Ada Lovelace"
    data[f"email-{len(people)}"] = "ada@nikhef.nl"
    data[f"alarms-{len(people)}"] = "on"
    http.post("/alarms/recipients", data=dict(data, by="apc"),
              follow_redirects=False)

    assert '<option value="Ada Lovelace">' in http.get("/").text


def test_no_recipient_file_leaves_the_box_working(webui, config_dir):
    """Nothing to suggest is not a broken header: it is a plain text field,
    which is what it was before any of this."""
    http, _, _ = webui
    (config_dir / "recipients.yaml").unlink()

    text = http.get("/").text
    # Scoped to the datalist: the page has other <select>s of its own.
    offered = text.split('<datalist id="operators">')[1].split("</datalist>")[0]

    assert "<option" not in offered
    assert 'list="operators"' in text and 'name="operator"' in text
