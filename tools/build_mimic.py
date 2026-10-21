"""Build the P&ID mimic SVG from the drawing. See DESIGN.md §8.2.

    python tools/build_mimic.py

Converts `notes/xams_piping_and_instrumentation.pdf` into
`src/xams_sc/api/static/xams_pid.svg`, and adds an empty `<text>` node beside
every instrument tag that corresponds to a channel. The web page fills those
in; the SVG itself is a static file.

**Re-run this when the P&ID is revised.** The drawing will eventually change,
and a mimic quietly out of date with the plant is a liability (§8.2). The tag
check below is what makes that visible rather than silent.

TAG NAMES. The drawing is the authoritative list (§3), and the tags are our
channel names in upper case. That includes the pressures: the P&ID once said
PT101-PT104 and PT201 where `channels.yaml` says p101-p104 and pmain, and the
PDF itself was relabelled (23 September 2026) so the printed sheet in the lab,
the mimic and the software all use one name per instrument.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xams_sc.config import load  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "notes" / "xams_piping_and_instrumentation.pdf"
OUT = ROOT / "src" / "xams_sc" / "api" / "static" / "xams_pid.svg"

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# PMAIN is the one channel whose name is not letters-then-digits.
TAG_PATTERN = re.compile(r"[A-Z]{1,3}\d{2,3}|PMAIN")

# Channels that have no instrument tag of their own, anchored to a label that
# IS on the drawing.
#
# The Lake Shore heater is drawn as a plain "Heater" box beside TT401/TT402
# rather than as a tagged instrument, so nothing in the tag sweep picks it up.
# Its power is worth seeing next to the two sensors that control it: output 1
# regulates on input A (setpoint -90 C), which is what tt401 reads.
# channel -> (anchor label, dx, dy), offset from the CENTRE of that label in
# final rotated coordinates, centre-anchored.
#
# The offsets exist because a plain label has no bubble to sit in, and the
# surrounding drawing decides what room there is. The heater reading goes
# BELOW its label: to the right it runs into the EVN116 valve symbol and then
# reads as though it belonged to that valve, which is worse than being hard to
# find.
EXTRA_PLACEMENTS = {
    "ls_heater_1_w": ("Heater", 0.0, 12.0),
    # The turbo speed, centred UNDER the pump symbol. The label sits above the
    # symbol in the final orientation, and the pipe runs through the symbol
    # sideways, so below it is clear and reads as belonging to the pump.
    "turbo_speed": ("Turbopump", 0.0, 42.0),
}

# Decimals for a value on the drawing where the general rule (two below 10,
# one below 1000) would be wrong. The turbo speed is a whole number of Hz, and
# "0.00 Hz" for a stopped pump reads as a measurement with three digits of
# precision.
DIGITS = {"turbo_speed": 0, "ln2_mass": 0}

# Drawn equipment whose OUTLINE is coloured by its state (section 8.2).
#
# symbol id -> (label beside it on the drawing, channel whose history a click
# opens). The symbol is found next to its label, and its own line work is
# copied into an overlay path with that id; the page colours the overlay and
# leaves the drawing underneath untouched. A copy rather than an edit of the
# drawing's paths, because the generated SVG gives those no stable names and a
# revised drawing would silently lose the colouring.
SYMBOLS = {
    "turbo": ("Turbopump", "turbo_speed"),
}



# Labels on the drawing replaced when the mimic is built: printed word -> tag.
#
# The PKR full-range gauge beside the turbo is drawn as "P" with a subscript
# "pirani" (one word to the text extractor, "Ppirani"). Its channel is p105
# (13 October 2026), so the label becomes P105 and the gauge then gets its
# value in its bubble exactly like P101-P104.
#
# Done HERE, in memory, rather than by editing the PDF: the PDF is saved from
# Illustrator with its editing data embedded, and Illustrator reopens that
# embedded copy - an edit made to the PDF from outside would silently vanish
# at the next save in Illustrator. When the drawing itself says P105, the
# entry below finds nothing and can be removed.
RELABEL = {"Ppirani": "P105"}


# Instruments the drawing does not have yet, ADDED when the mimic is built -
# in memory, like RELABEL, so the PDF from Illustrator stays untouched.
#
# SG401, the LN2 dewar scale (21 October 2026): a bubble with the strain-gauge
# stroke, exactly like SG101 / SG102 on the gas bottles, above the "N2" vessel
# (the LN2 dewar, with V_dewar). SG because a platform scale is load cells, as
# SG101/102 are; 4xx because the LN2 belongs to the cryostat's cooling, with
# TT401/TT402. It shows the NET LN2 mass, channel ln2_mass (TAG_ALIASES).
#
# Placed relative to a label on the drawing, in ORIGINAL page coordinates:
#   tag -> (anchor label, circle centre dx, dy, leader x offset, vessel top dy)
# The leader runs from the circle's left side across to `leader dx` and down
# to the vessel top, the shape SG101's leader has. Measured on the 17/05/2024
# drawing: the N2 label centre is (614.6, 555.9), the vessel top y = 535.3.
# When the drawing itself carries SG401, the tag sweep finds it there and the
# entry below must be removed (it would draw a second one).
ADD_INSTRUMENTS = {"SG401": ("N2", 17.05, -54.85, -14.6, -20.55)}

# A tag on the drawing whose channel is not simply its name in lower case.
# ln2_mass was named, and archived, before it had a tag.
TAG_ALIASES = {"SG401": "ln2_mass"}

BUBBLE_R = 11.65          # SG101's circle is 23.3 pt across
LINE_W = 0.375            # the drawing's instrument line weight


def add_instruments(page) -> None:
    """Draw the ADD_INSTRUMENTS bubbles, with their stroke, leader and tag."""
    words = {w[4]: w for w in page.get_text("words")}
    for tag, (anchor, dx, dy, lx, top) in ADD_INSTRUMENTS.items():
        if tag in words:
            print(f"  WARNING: the drawing already has {tag}; not adding it")
            continue
        spot = words.get(anchor)
        if spot is None:
            print(f"  WARNING: no label {anchor!r} on the drawing; {tag} not added")
            continue
        ax, ay = (spot[0] + spot[2]) / 2, (spot[1] + spot[3]) / 2
        cx, cy = ax + dx, ay + dy
        black = (0, 0, 0)
        page.draw_circle(pymupdf.Point(cx, cy), BUBBLE_R, color=black, width=LINE_W)
        # The strain-gauge stroke: a diameter, as on SG101 and SG102.
        page.draw_line(pymupdf.Point(cx, cy - BUBBLE_R), pymupdf.Point(cx, cy + BUBBLE_R),
                       color=black, width=LINE_W)
        page.draw_polyline([pymupdf.Point(cx - BUBBLE_R, cy), pymupdf.Point(ax + lx, cy),
                            pymupdf.Point(ax + lx, ay + top)], color=black, width=LINE_W)
        # The tag beside the circle, set like SG101's: 8.04 pt, running top to
        # bottom on the portrait page (rotate=270, as in relabel()).
        page.insert_text(pymupdf.Point(cx + BUBBLE_R + 2.4, cy - BUBBLE_R),
                         tag, fontsize=8.04, fontname="helv", rotate=270)
        print(f"  added {tag} beside {anchor!r}")


def relabel(page) -> None:
    """Replace the labels in RELABEL, keeping position, size and direction.

    Only the text is redacted; the line art under and around it (the bubble,
    the leader) is left alone.
    """
    words = [w for w in page.get_text("words") if w[4] in RELABEL]
    if not words:
        return
    for x0, y0, x1, y1, word, *_ in words:
        page.add_redact_annot(pymupdf.Rect(x0, y0, x1, y1))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    for x0, y0, x1, y1, word, *_ in words:
        # The drawing's labels run top to bottom on the (portrait) page, the
        # direction rotate=270 produces; 8.04 pt is the size of P101-P104.
        page.insert_text(pymupdf.Point(x0 + 2, y0), RELABEL[word], fontsize=8.04,
                         fontname="helv", rotate=270)
        print(f"  relabelled {word} -> {RELABEL[word]}")


# The sheet furniture, in the ORIGINAL (unrotated) page coordinates.
# Measured from the PDF. The title block sits in the corner; the frame is the
# sheet border, removed by cropping just inside it.
TITLE_BLOCK = (25.0, 820.0, 160.0, 1180.0)      # x0, y0, x1, y1
BORDER_INSET = 10.0                              # points to crop off each edge

# Black ink on white paper becomes light ink on a dark page. The two source
# colours are the drawing's own: solid black for the pipework and instruments,
# and a mid grey for secondary detail such as hatching and the vessel internals.
DARK_COLOURS = {
    "#000000": "#c4ccd8",      # main linework and text
    "#939598": "#5d6673",      # secondary detail, kept clearly subordinate
}


def strip_sheet_furniture(page) -> None:
    """Remove the title block and the sheet frame, at the PDF level.

    Doing this on the PDF rather than on the generated SVG is deliberate.
    An earlier version filtered SVG <path> elements by parsing the numbers out
    of their `d` attribute — which silently produced nonsense, because SVG path
    data uses `H` and `V` commands that take a SINGLE coordinate and the naive
    pairing of numbers into (x, y) then has everything after the first such
    command offset by one. pymupdf already knows the real geometry, so let it
    do the work.

    Nothing is lost: the drawing, its authors and its date live in
    notes/xams_piping_and_instrumentation.pdf, which is the source this file is
    generated from and where anyone asking "whose drawing is this?" should look.
    """
    page.add_redact_annot(pymupdf.Rect(*TITLE_BLOCK))
    page.apply_redactions()

    r = page.rect
    page.set_cropbox(pymupdf.Rect(r.x0 + BORDER_INSET, r.y0 + BORDER_INSET,
                                  r.x1 - BORDER_INSET, r.y1 - BORDER_INSET))


# The drawing's own labels. Brighter than the linework: on a dark background
# thin glyphs need more contrast than strokes do to read at the same weight.
DARK_TEXT = "#dfe4ec"

# How a unit is written on the drawing. The stored unit strings are chosen for
# machines; these are for people reading a diagram at a glance.
UNIT_LABEL = {
    "C": "°C", "K": "K", "bar": "bar", "g/min": "g/min", "g": "g",
    "V": "V", "uA": "µA", "W": "W", "percent": "%", "min": "min", "mbar": "mbar",
    "bool": "",            # "on battery" is not a quantity; no unit to show
    "TBD": "?",            # a unit nobody has established yet, said out loud
}


def recolour_for_dark(root) -> None:
    """Light ink on a dark page, so the mimic belongs to the interface."""
    changed = 0
    for el in root.iter():
        for attr in ("stroke", "fill"):
            value = el.get(attr)
            if value in DARK_COLOURS:
                el.set(attr, DARK_COLOURS[value])
                changed += 1

    # TEXT WITH NO fill AT ALL DEFAULTS TO BLACK.
    #
    # Recolouring only the elements that name a colour leaves every label in
    # the drawing invisible on a dark page — 82 of them here, because the
    # generated SVG relies on the SVG default rather than stating it. An
    # attribute that is absent still has a value, and this is where that bites.
    labels = 0
    for el in root.iter():
        if el.tag.split("}")[-1] != "text":
            continue
        if el.get("fill") is None:
            el.set("fill", DARK_TEXT)
            labels += 1
    print(f"  recoloured {changed} attribute(s) and {labels} unstyled label(s)")


def channel_for(tag: str, channels: set[str]) -> str | None:
    name = TAG_ALIASES.get(tag, tag.lower())
    return name if name in channels else None


# How much blank paper to leave around the drawing once it is cropped to its
# own ink. Generous on the sides because the live values are centred on their
# tags and GROW as they are filled in — a placeholder em dash is a few units
# wide and "-4200.0 V" is thirty — so a tag near the edge must still have room
# to say its number. Vertical growth is only the line height, so less is kept.
INK_MARGIN_X = 18.0
INK_MARGIN_Y = 12.0


def tighten_viewbox(path: Path, labels=None) -> tuple[float, float, float, float] | None:
    """Crop the viewBox to what is actually drawn, plus a margin.

    The PDF is a drawing sheet, so the page is bigger than the drawing: after
    the border inset there was still 42 units of blank paper above the ink and
    76 below it. In a box whose height is bounded — which is the whole of
    §8.2 — that blank paper is subtracted from the drawing before anything
    else gets a say, so the mimic rendered about 14% smaller than it needed
    to for no reason a reader could see.

    Measured by rendering rather than by parsing: the geometry is a soup of
    relative path commands, and the one thing that is certainly right about a
    rendering is where the ink is.
    """
    doc = pymupdf.open(str(path))
    page = doc[0]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    # Taken while the document is still open. Reading page.rect after
    # doc.close() worked on older pymupdf and asserts on newer ones, which is
    # a crash AFTER the SVG has been written — so the file is left with an
    # untightened viewBox and the build looks like it only half failed.
    page_w, page_h = page.rect.width, page.rect.height
    w, h, n, stride = pix.width, pix.height, pix.n, pix.stride
    data, bg = pix.samples, pix.samples[0:pix.n]

    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = data[y * stride: y * stride + w * n]
        for x in range(w):
            if row[x * n:(x + 1) * n] != bg:
                minx = min(minx, x); maxx = max(maxx, x)
                miny = min(miny, y); maxy = max(maxy, y)
    doc.close()
    if maxx < 0:
        return None                      # nothing drawn; leave it alone

    sx, sy = page_w / w, page_h / h
    ix0, iy0, ix1, iy1 = minx * sx, miny * sy, maxx * sx, maxy * sy
    # The drawing's own labels, measured from the PDF. pymupdf's SVG renderer
    # does not draw every label an Illustrator-saved PDF produces, so the
    # rendering alone cropped GT101, the leftmost label, in half.
    if labels is not None:
        ix0, iy0 = min(ix0, labels[0]), min(iy0, labels[1])
        ix1, iy1 = max(ix1, labels[2]), max(iy1, labels[3])
    x0 = max(0.0, ix0 - INK_MARGIN_X)
    y0 = max(0.0, iy0 - INK_MARGIN_Y)
    x1 = min(page_w, ix1 + INK_MARGIN_X)
    y1 = min(page_h, iy1 + INK_MARGIN_Y)
    return (x0, y0, x1 - x0, y1 - y0)


def symbol_outline(page, anchor: str, rotated) -> str | None:
    """SVG path data for the equipment symbol beside a label, in final coordinates.

    The symbol is the roughly square drawing (a pump circle is about 23 pt
    across) nearest the label, plus whatever is drawn inside it - the chevron
    of a pump. Pipes run past the same label and are excluded by size: a
    drawing longer than the symbol in either direction is not part of it.

    Lines and Bezier curves are copied; anything else in the symbol is reported
    rather than guessed at.
    """
    spot = next((w for w in page.get_text("words") if w[4] == anchor), None)
    if spot is None:
        return None
    lx, ly = (spot[0] + spot[2]) / 2, (spot[1] + spot[3]) / 2

    drawings = page.get_drawings()
    best, best_d2 = None, 45.0 ** 2
    for d in drawings:
        r = d["rect"]
        if not (15 <= r.width <= 40 and 15 <= r.height <= 40):
            continue
        if abs(r.width - r.height) > 0.25 * max(r.width, r.height):
            continue
        d2 = ((r.x0 + r.x1) / 2 - lx) ** 2 + ((r.y0 + r.y1) / 2 - ly) ** 2
        if d2 < best_d2:
            best, best_d2 = d, d2
    if best is None:
        return None

    box = best["rect"]
    parts = [d for d in drawings
             if d is best or (d["rect"].x0 >= box.x0 - 1 and d["rect"].x1 <= box.x1 + 1
                              and d["rect"].y0 >= box.y0 - 1 and d["rect"].y1 <= box.y1 + 1)]

    def pt(p):
        x, y = rotated(p.x, p.y)
        return f"{x:.2f},{y:.2f}"

    out, last = [], None
    for d in parts:
        for item in d["items"]:
            kind = item[0]
            if kind == "l":
                a, b = item[1], item[2]
                if last is None or abs(last.x - a.x) > 0.01 or abs(last.y - a.y) > 0.01:
                    out.append("M" + pt(a))
                out.append("L" + pt(b))
                last = b
            elif kind == "c":
                a, c1, c2, b = item[1:5]
                if last is None or abs(last.x - a.x) > 0.01 or abs(last.y - a.y) > 0.01:
                    out.append("M" + pt(a))
                out.append("C" + " ".join(pt(q) for q in (c1, c2, b)))
                last = b
            else:
                print(f"  WARNING: {anchor} symbol has a {kind!r} element; not copied")
        last = None
    return " ".join(out) if out else None


def main() -> int:
    config = load()
    # Every channel in the file, not just the enabled ones: a disabled channel
    # is still a real instrument on the drawing and should get a place to show
    # that it is out of service.
    known = set(config.channels)

    doc = pymupdf.open(PDF)
    page = doc[0]

    # Before anything reads coordinates: cropping moves the origin, and every
    # tag position below must be measured in the cropped space.
    strip_sheet_furniture(page)
    relabel(page)
    add_instruments(page)
    width, height = page.rect.width, page.rect.height

    svg_text = page.get_svg_image(text_as_path=False)
    root = ET.fromstring(svg_text)

    # Labels set in Helvetica (P101-P104 and PMAIN since the 23 September
    # relabel, and P105 from RELABEL) come out as "Nimbus Sans" with no
    # generic fallback, which a browser without that font draws in a SERIF
    # face. Dropping the attribute lets them inherit the page font, which is
    # what the drawing's other labels already show in.
    for el in root.iter():
        if "Nimbus" in (el.get("font-family") or ""):
            del el.attrib["font-family"]

    # STRIP THE DRAWING FURNITURE, AND RECOLOUR FOR A DARK PAGE.
    #
    # The PDF is a printable A3 sheet: an outer frame, a title block with the
    # authors and the date, black ink on white. All of that is right for paper
    # and wrong for a live mimic embedded in a dark interface, where it reads
    # as a screenshot of some other application pasted into the page.
    #
    # The information is not lost — the drawing, its authors and its date are
    # in notes/xams_piping_and_instrumentation.pdf, which is what the SVG is
    # generated from and where anyone asking "whose drawing is this?" should
    # look (§8.2 keeps the PDF as the source).
    recolour_for_dark(root)

    # ROTATE THE DRAWING 90 DEGREES COUNTER-CLOCKWISE.
    #
    # The PDF is stored portrait with the drawing on its side: 88 of its 90
    # text lines run vertically (direction 0,1). Rotating the page object does
    # not help — pymupdf still emits the original orientation — so the
    # rotation is applied here, in the SVG.
    #
    # rotate(-90) sends (x, y) to (y, -x); translate(0, width) brings it back
    # on screen, so the content ends up spanning (0..height, 0..width) and the
    # viewBox swaps its dimensions.
    body = ET.Element(f"{{{SVG_NS}}}g")
    body.set("transform", f"translate(0,{width:.1f}) rotate(-90)")
    for child in list(root):
        root.remove(child)
        body.append(child)
    root.append(body)

    root.set("viewBox", f"0 0 {height:.0f} {width:.0f}")
    root.set("width", f"{height:.0f}")
    root.set("height", f"{width:.0f}")

    def rotated(x: float, y: float) -> tuple[float, float]:
        """Where a point in the original drawing ends up after the rotation."""
        return (y, width - x)

    # Instrument bubbles: the empty circles the drawing puts beside each tag.
    # A value belongs INSIDE its bubble — that is what makes a mimic read as a
    # mimic rather than as a drawing with numbers scattered over it.
    bubbles = []
    for item in page.get_drawings():
        r = item["rect"]
        if not (18 <= r.width <= 40 and 18 <= r.height <= 40):
            continue
        if abs(r.width - r.height) > 0.25 * max(r.width, r.height):
            continue
        bubbles.append(r)

    def bubble_near(cx: float, cy: float):
        """The instrument bubble belonging to a tag, if it has one.

        Not every tag does. TT201-TT207 are labels with leader lines pointing
        into the detector vessel, and their nearest circle is over 100 pt away
        — a value dropped there would land on an unrelated instrument. Those
        get their value beside the label instead.
        """
        best, best_d2 = None, 30.0 ** 2
        for r in bubbles:
            bx, by = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            d2 = (bx - cx) ** 2 + (by - cy) ** 2
            if d2 < best_d2:
                best, best_d2 = (bx, by), d2
        return best

    placed, unmatched = {}, []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        if not TAG_PATTERN.fullmatch(word):
            continue
        channel = channel_for(word, known)
        if channel is None:
            unmatched.append(word)
            continue
        if channel in placed:
            continue

        # The value goes OUTSIDE the rotated group, in final coordinates.
        # Inside it, it would be rotated with everything else and end up
        # sideways — readable numbers are the whole point of this page.
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        bubble = bubble_near(cx, cy)
        if bubble is not None:
            fx, fy = rotated(*bubble)
            fy += 3.2                       # optical centre of the circle
        else:
            # Just past the END of the label, in the FINAL orientation.
            #
            # The text runs vertically in the original page (direction 0,1),
            # so its last character is at y1 — and after the rotation that
            # becomes the right-hand end. Offsetting the original x instead
            # moves the value along the wrong axis and drops it on top of the
            # label, which is exactly what the first attempt did.
            fx, fy = rotated(cx, y1)
            fx += 6
            fy += 3.2
        # TWO elements: a white outline underneath, then the number on top.
        #
        # The obvious way to get a halo is paint-order="stroke" on one element,
        # but that attribute is not universally supported — where it is
        # ignored the stroke paints OVER the glyph and a 2.5 px outline
        # swallows a 9 px character entirely. Drawing the halo as a separate
        # element underneath works everywhere, which matters for a page that
        # has to keep working for years without anyone maintaining it.
        # Centred inside a bubble; left-aligned when it sits beside a label,
        # so a negative sign grows away from the text rather than into it.
        # A pressure in mbar is written as "1.5e-2": six characters, wider
        # than the four a 23 pt bubble takes at 9 px, so it is set smaller.
        exp = config.channels[channel].unit == "mbar"
        common = {
            "x": f"{fx:.1f}", "y": f"{fy:.1f}",
            "text-anchor": "middle" if bubble is not None else "start",
            "font-family": "Consolas, monospace", "font-size": "6.5" if exp else "9",
            "font-weight": "bold",
        }
        halo = ET.SubElement(root, f"{{{SVG_NS}}}text")
        # id first: ElementTree writes attributes in the order they are set,
        # and a file where every element starts with its id is far easier to
        # grep and to read three years from now.
        halo.set("id", f"h-{channel}")
        for k, v in common.items():
            halo.set(k, v)
        halo.set("fill", "none")
        halo.set("stroke", "#1b1f27")
        halo.set("stroke-width", "3")
        halo.set("stroke-linejoin", "round")
        halo.text = "—"

        value = ET.SubElement(root, f"{{{SVG_NS}}}text")
        value.set("id", f"v-{channel}")
        for k, v in common.items():
            value.set(k, v)
        value.set("fill", "#6e7681")
        value.text = "—"

        # THE UNIT, placed differently depending on the room available.
        #
        # Inside a bubble there is space for a second, smaller line under the
        # number — about four characters at 9 px is all a 23 pt circle takes,
        # so the unit cannot share the line. Beside a label there is plenty of
        # horizontal room, so the unit rides along in the same text node and
        # the page writes "-60.7 degC" as one string. Marking which is which
        # here keeps that decision out of the JavaScript.
        unit_text = UNIT_LABEL.get(config.channels[channel].unit,
                                   config.channels[channel].unit)
        value.set("data-unit", unit_text)
        # A vacuum pressure spans twelve decades: written as 1.5e-2, never
        # as "0.02" or "0.00".
        if config.channels[channel].unit == "mbar":
            value.set("data-format", "exp")
        if channel in DIGITS:
            value.set("data-digits", str(DIGITS[channel]))
        if bubble is not None and unit_text:
            unit = ET.SubElement(root, f"{{{SVG_NS}}}text")
            unit.set("id", f"u-{channel}")
            unit.set("x", f"{fx:.1f}")
            unit.set("y", f"{fy + 7.0:.1f}")
            unit.set("text-anchor", "middle")
            unit.set("font-family", "Consolas, monospace")
            unit.set("font-size", "6.5")
            unit.set("fill", "#6e7681")   # replaced by the page as soon as a value arrives
            unit.text = unit_text
        else:
            value.set("data-unit-inline", "1")

        placed[channel] = (word, "bubble" if bubble is not None else "beside")

    # Equipment outlines coloured by state. See SYMBOLS.
    for sym, (anchor, history) in SYMBOLS.items():
        path = symbol_outline(page, anchor, rotated)
        if path is None:
            print(f"  WARNING: no symbol found beside {anchor!r}; {sym} is not coloured")
            continue
        el = ET.SubElement(root, f"{{{SVG_NS}}}path")
        el.set("id", f"sym-{sym}")
        el.set("d", path)
        el.set("fill", "none")
        # Invisible until the page knows the state: an equipment symbol that
        # is green before anything has been read is a claim nobody made.
        el.set("stroke", "none")
        el.set("stroke-width", "2.2")
        el.set("stroke-linejoin", "round")
        el.set("stroke-linecap", "round")
        el.set("data-history", history)
        placed[f"sym-{sym}"] = (anchor, "outline")

    # Channels anchored to a plain label rather than to an instrument tag.
    for channel, (anchor, dx, dy) in EXTRA_PLACEMENTS.items():
        if channel not in known or channel in placed:
            continue
        spot = next((w for w in page.get_text("words") if w[4] == anchor), None)
        if spot is None:
            print(f"  WARNING: no label {anchor!r} on the drawing for {channel}")
            continue
        x0, y0, x1, y1 = spot[:4]
        fx, fy = rotated((x0 + x1) / 2, (y0 + y1) / 2)
        fx += dx
        fy += dy
        unit_text = UNIT_LABEL.get(config.channels[channel].unit,
                                   config.channels[channel].unit)
        common = {"x": f"{fx:.1f}", "y": f"{fy:.1f}", "text-anchor": "middle",
                  "font-family": "Consolas, monospace", "font-size": "9",
                  "font-weight": "bold"}
        halo = ET.SubElement(root, f"{{{SVG_NS}}}text")
        halo.set("id", f"h-{channel}")
        for k, v in common.items():
            halo.set(k, v)
        halo.set("fill", "none"); halo.set("stroke", "#1b1f27")
        halo.set("stroke-width", "3"); halo.set("stroke-linejoin", "round")
        halo.text = "—"

        value = ET.SubElement(root, f"{{{SVG_NS}}}text")
        value.set("id", f"v-{channel}")
        for k, v in common.items():
            value.set(k, v)
        value.set("fill", "#6e7681")
        value.set("data-unit", unit_text)
        value.set("data-unit-inline", "1")
        if channel in DIGITS:
            value.set("data-digits", str(DIGITS[channel]))
        value.text = "—"
        placed[channel] = (anchor, "anchored")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))

    # Crop to the ink. Done after writing because it is measured by rendering
    # the finished file, values and all.
    corners = [rotated(x, y) for x0, y0, x1, y1, *_ in page.get_text("words")
               for x, y in ((x0, y0), (x1, y1))]
    labels = (min(c[0] for c in corners), min(c[1] for c in corners),
              max(c[0] for c in corners), max(c[1] for c in corners))
    box = tighten_viewbox(OUT, labels)
    if box is not None:
        x, y, w, h = box
        root.set("viewBox", f"{x:.1f} {y:.1f} {w:.1f} {h:.1f}")
        root.set("width", f"{w:.0f}")
        root.set("height", f"{h:.0f}")
        OUT.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
        print(f"  cropped to the ink: viewBox {x:.0f} {y:.0f} {w:.0f} {h:.0f}")

    print(f"  wrote {OUT.relative_to(ROOT)}  ({OUT.stat().st_size/1024:.0f} kB)")
    print(f"  rotated 90 CCW: {width:.0f}x{height:.0f} -> {height:.0f}x{width:.0f}")
    print(f"  {len(placed)} value placeholders added")
    for channel, (tag, how) in sorted(placed.items()):
        note = f"  (drawing calls it {tag})" if tag.lower() != channel else ""
        print(f"    v-{channel:10s} {how:7s}{note}")
    print(f"\n  {len(set(unmatched))} tag(s) on the drawing are not instrumented:")
    print("   ", ", ".join(sorted(set(unmatched))))
    print("\n  That is informative in itself: it shows at a glance how much of")
    print("  the plant the slow control actually sees (§8.2).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
