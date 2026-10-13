"""Every `onsubmit` confirmation must actually be a confirmation. See §10a.

**Three of the four confirm dialogs on this site did nothing, and one of them
guarded the high voltage.** They looked right in the template and they were
never rendered by a test, so nothing noticed.

Two separate ways of breaking them, both silent:

  * a **raw newline inside a JS string literal**. Written into the template
    as real blank lines to keep the message readable, which is a syntax error
    in JavaScript. The handler throws, no dialog appears, and - because an
    exception in an inline handler does not cancel anything - the form
    submits regardless.

  * **`tojson` inside a double-quoted attribute**. It writes a double-quoted
    JS string, so the first quote it emits ends `onsubmit="` and the rest of
    the message becomes stray markup.

Neither shows up in a page that merely renders, which is why this file asserts
on the rendered HTML rather than on the templates.
"""

import pathlib
import re

from jinja2 import Environment

from xams_sc import config as config_module
from xams_sc.api import app as app_module

REPO_CONFIG = config_module.ROOT / "config"

# Every page that has a form on it. A page missing from here is a page whose
# buttons nothing checks, so it is asserted below that each one was reachable.
PAGES = ("/", "/alarms", "/hv", "/hv/defaults", "/status", "/mimic", "/rga")

HANDLER = re.compile(r"""onsubmit=(?P<q>["'])(?P<body>.*?)(?P=q)""", re.S)


def handlers(html):
    return [m.group("q") + m.group("body") + m.group("q")
            for m in HANDLER.finditer(html)]


def test_the_pages_have_handlers_to_check():
    """A regex that silently matches nothing is a test that always passes."""
    templates = pathlib.Path(app_module.__file__).parent / "templates"
    found = sum("onsubmit=" in p.read_text(encoding="utf-8")
                for p in templates.glob("*.html"))
    assert found >= 3


def test_no_confirmation_contains_a_raw_newline(client):
    seen = 0
    for page in PAGES:
        response = client.get(page)
        assert response.status_code == 200, page
        for handler in handlers(response.text):
            seen += 1
            assert "\n" not in handler, (
                "%s: a real newline in a JS string literal - the dialog will "
                "never open and the form submits unconfirmed: %r"
                % (page, handler[:80]))
    assert seen >= 3


def test_no_confirmation_breaks_out_of_its_attribute(client):
    """The quote that opened the attribute must not appear inside it."""
    for page in PAGES:
        for m in HANDLER.finditer(client.get(page).text):
            assert m.group("q") not in m.group("body"), (
                "%s: the handler ends early at a %s - the confirm text is "
                "stray markup: %r" % (page, m.group("q"), m.group("body")[:80]))


def test_the_hv_confirmation_is_quoted_on_both_branches():
    """Turning OFF and energising, since only one of them renders at a time.

    A filter binds tighter than the inline `if` in Jinja, so `A if c else B |
    tojson` quotes B alone and sends A out bare. Both HV buttons are the same
    expression, and for months only the branch nobody was looking at was
    broken.
    """
    templates = pathlib.Path(app_module.__file__).parent / "templates"
    source = (templates / "hv.html").read_text(encoding="utf-8")
    expression = re.search(r"confirm\((\{\{.*?\}\})\)", source, re.S).group(1)

    env = Environment()
    rendered = {
        energised: env.from_string(expression).render(
            c=type("C", (), {"label": "pmt_top", "energised": energised})())
        for energised in (True, False)
    }
    for energised, text in rendered.items():
        assert text.startswith(('"', "'")), (
            "energised=%s renders an unquoted argument: %r"
            % (energised, text[:60]))
        assert "\n" not in text, energised
    assert "Turn OFF" in rendered[True]
    assert "Energise" in rendered[False]
