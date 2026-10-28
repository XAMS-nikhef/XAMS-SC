"""Editing the HV default setpoints from the web UI. See DESIGN.md §4.6.

This is the one place the web UI writes a file that git tracks, and §8.1 draws
a hard line around it: **defaults are editable, limits are not.** A default is
a number offered in a box; a limit is the range every write to an electrode is
checked against. A page that could widen its own limit and then write to it
would not be a limit at all, so the tests that matter most here are the ones
that say no.

Nothing in this file touches an instrument, and nothing in the feature does
either — which is the reason it is allowed to exist before the rest of §10a.
"""


import pytest
import yaml

from xams_sc import config as config_module
from xams_sc.config import ConfigError, load, read_hv_defaults, write_hv_defaults

REPO_CONFIG = config_module.ROOT / "config"


# ------------------------------------------------------------ the config layer

def test_absent_file_is_fine(config_dir):
    """A fresh clone has no hv_defaults.yaml and runs on channels.yaml."""
    assert not (config_dir / "hv_defaults.yaml").exists()
    cfg = load(config_dir)
    assert cfg.channels["hv_cathode_vset"].default_setpoint == -2250.0


def test_override_wins_over_channels_yaml(config_dir):
    write_hv_defaults({"hv_cathode_vset": -1500.0}, "apc", config_dir)
    cfg = load(config_dir)
    assert cfg.channels["hv_cathode_vset"].default_setpoint == -1500.0
    # A channel the file does not mention keeps the reviewed value.
    assert cfg.channels["hv_anode_vset"].default_setpoint == 4200.0


def test_default_outside_limits_is_refused(config_dir):
    """The limit is in channels.yaml and the override cannot escape it."""
    write_hv_defaults({"hv_cathode_vset": -3600.0}, "apc", config_dir)
    with pytest.raises(ConfigError) as exc:
        load(config_dir)
    assert "outside its limits" in str(exc.value)
    # And it says where to go instead of leaving the reader to guess.
    assert "channels.yaml" in str(exc.value)


def test_unknown_channel_is_refused(config_dir):
    write_hv_defaults({"hv_nonesuch_vset": -10.0}, "apc", config_dir)
    with pytest.raises(ConfigError) as exc:
        load(config_dir)
    assert "unknown channel" in str(exc.value)


def test_non_setpoint_channel_is_refused(config_dir):
    """A monitor is not a setpoint, however much its name looks like one."""
    write_hv_defaults({"hv_cathode_vmon": -10.0}, "apc", config_dir)
    with pytest.raises(ConfigError) as exc:
        load(config_dir)
    assert "not a setpoint" in str(exc.value)


def test_defaults_do_not_change_the_config_hash(config_dir):
    """Excluded from the hash, like recipients.yaml: it does not affect the
    data, and every JSONL file carries that hash (§4.5)."""
    before = load(config_dir).config_hash
    write_hv_defaults({"hv_cathode_vset": -1500.0}, "apc", config_dir)
    assert load(config_dir).config_hash == before


def test_written_file_records_who_and_when(config_dir):
    write_hv_defaults({"hv_gate_vset": -1000.0}, "apc", config_dir)
    body = yaml.safe_load((config_dir / "hv_defaults.yaml").read_text())
    assert body["by"] == "apc"
    assert body["updated"].endswith("+00:00")
    assert body["defaults"] == {"hv_gate_vset": -1000.0}


def test_written_file_is_readable_prose(config_dir):
    """It is hand-editable, so it must explain itself at the top."""
    write_hv_defaults({"hv_gate_vset": -1000.0}, "apc", config_dir)
    text = (config_dir / "hv_defaults.yaml").read_text()
    assert text.startswith("# HV default setpoints")
    assert "SIGNED" in text


def test_read_hv_defaults_on_missing_file(config_dir):
    assert read_hv_defaults(config_dir) == {}


# ------------------------------------------------------------------- the page

def test_page_lists_every_setpoint_channel(webui):
    http, app, _ = webui
    response = http.get("/hv/defaults")
    assert response.status_code == 200
    for label in ("cathode", "gate", "anode", "nai", "pmt_bot"):
        assert label in response.text


def test_page_shows_the_range_but_offers_no_box_for_it(webui):
    """The limits are displayed, because the operator needs them to choose a
    default. They are NOT an input, because that is the whole line (§4.6)."""
    http, _, _ = webui
    text = http.get("/hv/defaults").text
    assert "-3500" in text or "−3500" in text
    assert 'name="limits"' not in text
    assert 'name="hv_cathode_vset_min"' not in text


def test_saving_writes_the_file_and_reloads(webui, config_dir):
    http, app, _ = webui
    response = http.post("/hv/defaults",
                         data={"hv_cathode_vset": "-1500", "by": "apc"},
                         follow_redirects=False)
    assert response.status_code == 303
    assert "saved" in response.headers["location"]
    body = yaml.safe_load((config_dir / "hv_defaults.yaml").read_text())
    assert body["defaults"]["hv_cathode_vset"] == -1500.0
    # Reloaded in place: /hv must offer the new value at once, not after a
    # restart. Reporting "saved" for something not yet in force is the
    # failure §7.5 names.
    assert app.state.system.config.channels[
        "hv_cathode_vset"].default_setpoint == -1500.0


def test_saving_out_of_range_writes_nothing(webui, config_dir):
    http, app, _ = webui
    response = http.post("/hv/defaults",
                         data={"hv_cathode_vset": "-3600", "by": "apc"},
                         follow_redirects=False)
    assert response.status_code == 303
    assert "error" in response.headers["location"]
    assert "outside" in response.headers["location"]
    assert not (config_dir / "hv_defaults.yaml").exists()


def test_one_bad_value_refuses_the_whole_save(webui, config_dir):
    """All or nothing. A partial save leaves a set of defaults nobody chose."""
    http, _, _ = webui
    response = http.post("/hv/defaults",
                         data={"hv_cathode_vset": "-1500",   # fine
                               "hv_anode_vset": "9999",      # outside 0..4500
                               "by": "apc"},
                         follow_redirects=False)
    assert "error" in response.headers["location"]
    assert not (config_dir / "hv_defaults.yaml").exists()


def test_non_numeric_is_refused_by_name(webui, config_dir):
    http, _, _ = webui
    response = http.post("/hv/defaults",
                         data={"hv_gate_vset": "minus a lot", "by": "apc"},
                         follow_redirects=False)
    assert "error" in response.headers["location"]
    assert "gate" in response.headers["location"]
    assert not (config_dir / "hv_defaults.yaml").exists()


def test_every_change_is_audited(webui):
    """§10 rule 5: who, what, when, from, to — for this as for every write."""
    http, _, bus = webui
    http.post("/hv/defaults",
              data={"hv_cathode_vset": "-1500", "by": "apc"},
              follow_redirects=False)
    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert any('"action":"hv_default"' in a
               and '"target":"hv_cathode_vset"' in a
               and '"old":"-2250.0"' in a
               and '"new":"-1500.0"' in a
               and '"actor":"apc"' in a
               for a in audits), audits


ALL_DEFAULTS = {
    "hv_pmt_bot_vset": "-700", "hv_pmt_top_vset": "0",
    "hv_ts_vset": "-500", "hv_bs_vset": "-600",
    "hv_cathode_vset": "-2250", "hv_gate_vset": "-1750",
    "hv_anode_vset": "4200", "hv_nai_vset": "600",
}


def test_unchanged_values_are_not_audited(webui):
    """Saving the page untouched must not fill the audit trail with noise."""
    http, _, bus = webui
    http.post("/hv/defaults", data=dict(ALL_DEFAULTS, by="apc"),
              follow_redirects=False)
    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert audits == []


def test_a_field_left_out_of_the_form_is_not_cleared(webui, config_dir):
    """**ABSENT is not EMPTY.**

    A box left blank on this page posts an empty string and means "no
    default". A field missing from the POST altogether did not come from this
    page, and clearing the other seven channels because one was sent would
    lose seven reviewed numbers to a malformed request.

    Found by a test that posted one field and watched all eight get audited.
    """
    http, app, bus = webui
    http.post("/hv/defaults",
              data={"hv_cathode_vset": "-1500", "by": "apc"},
              follow_redirects=False)
    body = yaml.safe_load((config_dir / "hv_defaults.yaml").read_text())
    assert body["defaults"]["hv_cathode_vset"] == -1500.0
    assert body["defaults"]["hv_anode_vset"] == 4200.0
    assert body["defaults"]["hv_gate_vset"] == -1750.0  # negative again since 28 Oct 2026 (ch1)
    # And only the one that really changed is audited.
    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert len(audits) == 1
    assert "hv_cathode_vset" in audits[0]


def test_an_empty_box_is_still_a_clear(webui, config_dir):
    """The other half of the rule above: present-but-blank does clear."""
    http, _, _ = webui
    http.post("/hv/defaults",
              data=dict(ALL_DEFAULTS, hv_cathode_vset="", by="apc"),
              follow_redirects=False)
    body = yaml.safe_load((config_dir / "hv_defaults.yaml").read_text())
    assert "hv_cathode_vset" not in body["defaults"]
    assert body["defaults"]["hv_anode_vset"] == 4200.0


def test_saving_asks_the_others_to_reload(webui):
    http, _, bus = webui
    http.post("/hv/defaults", data={"hv_cathode_vset": "-1500", "by": "apc"},
              follow_redirects=False)
    assert any(topic == "xams/cmd/all/reload" for topic, _ in bus.published)


def test_hv_page_links_to_the_editor(webui):
    http, _, _ = webui
    assert '/hv/defaults' in http.get("/hv").text
