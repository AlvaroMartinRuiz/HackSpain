"""What the console asks Quiver to draw.

Sixteen icons that have to look like one set rather than sixteen icons, which is
what the shared ``*_STYLE`` blocks are for: the style text is the same on every
request, and only the subject changes. Outcome icons are named after the submit
actions in ``platform_api.client.SUBMIT_ROUTES``, so the dashboard can look one
up as ``outcome-{action}`` without a mapping table.

Changing a prompt changes its hash, and the generator redraws only what changed.
"""

from __future__ import annotations

from dataclasses import dataclass

ICON_STYLE = (
    "Flat monochrome line icon for a dark user interface. One consistent stroke "
    "weight of 1.6 units, round caps and round joins, centred in the viewBox "
    "with roughly 2 units of clear margin on every side. Draw with "
    'stroke="currentColor" and fill="none" on every element — never a literal '
    "colour value, never a gradient, never a filter, never a drop shadow. No "
    "lettering and no numerals. Geometric, uncluttered and still legible at 20 "
    "pixels. Return a single <svg> element."
)

SCENE_STYLE = (
    "Simple monochrome line illustration for a dark user interface, in the same "
    "language as a set of line icons: one consistent stroke weight of 1.4 units, "
    "round caps and joins, generous negative space, no ground shadow. Draw with "
    'stroke="currentColor" and fill="none" only — no literal colour values, no '
    "gradients. No lettering, no numerals and no people. Return a single <svg> "
    "element."
)

BRAND_STYLE = (
    "Monochrome geometric logo mark for a dark user interface. Confident, "
    "balanced and simple enough to read as a favicon. Strokes of 1.8 units with "
    'round joins, drawn with stroke="currentColor"; a solid fill is allowed but '
    'only as fill="currentColor". No literal colour values, no gradients, no '
    "lettering and no numerals. Return a single <svg> element."
)


@dataclass(frozen=True)
class Asset:
    name: str
    group: str
    prompt: str
    style: str = ICON_STYLE
    width: int = 24
    height: int = 24


# What an agent is doing right now. These four carry the live fleet view, so they
# have to read at a glance and differ in silhouette, not just in detail.
STATUS = [
    Asset(
        "status-idle", "status",
        "A telephone handset resting in its cradle, seen from the side: a quiet, "
        "waiting line. Nothing radiating from it.",
    ),
    Asset(
        "status-listening", "status",
        "An incoming sound wave: a small circle on the left with three "
        "concentric arcs opening rightwards from it, each arc larger than the "
        "one before.",
    ),
    Asset(
        "status-thinking", "status",
        "A gear wheel with six square teeth and a hollow round centre, with "
        "three small separate dots arranged in a short arc above its upper "
        "right, suggesting deliberation.",
    ),
    Asset(
        "status-speaking", "status",
        "An audio waveform: five vertical rounded bars of clearly different "
        "heights in a row, all centred on one horizontal midline.",
    ),
]

# Named after the submit actions, so the dashboard resolves outcome-{action}.
OUTCOMES = [
    Asset(
        "outcome-book", "outcome",
        "A calendar page — a rounded square with two short tabs on top and a "
        "horizontal rule below the header — with a large check mark drawn "
        "across its lower half.",
    ),
    Asset(
        "outcome-reschedule", "outcome",
        "A calendar page — a rounded square with two short tabs on top and a "
        "horizontal rule below the header — with a clockwise circular arrow, "
        "an arc ending in an arrowhead, drawn across its lower half.",
    ),
    Asset(
        "outcome-cancel", "outcome",
        "A calendar page — a rounded square with two short tabs on top and a "
        "horizontal rule below the header — with a diagonal cross drawn across "
        "its lower half.",
    ),
    Asset(
        "outcome-register", "outcome",
        "A single person's head and shoulders in outline, with a small plus "
        "sign sitting just off their lower right: a patient being added to the "
        "records for the first time.",
    ),
    Asset(
        "outcome-escalate", "outcome",
        "A stethoscope: a Y-shaped tube with two small earpieces at the top and "
        "a round chest piece at the lower right.",
    ),
    Asset(
        "outcome-no_action", "outcome",
        "A blank document page with a folded top-right corner, and centred on "
        "it a short vertical stroke above a single separate dot: a record "
        "deliberately left empty.",
    ),
]

# The counters along the top of the dashboard.
METRICS = [
    Asset(
        "metric-live", "metric",
        "A telephone handset tilted as though in use, with two short curved "
        "lines radiating away from its earpiece.",
    ),
    Asset(
        "metric-peak", "metric",
        "Four vertical bars of increasing height standing on one baseline, with "
        "a horizontal line running across the top of the tallest bar and a "
        "little beyond it, marking a high-water mark.",
    ),
    Asset(
        "metric-latency", "metric",
        "A stopwatch: a circle with a small stem and button on top, two short "
        "hands inside pointing to the upper right, and a small tab either side "
        "of the stem.",
    ),
    Asset(
        "metric-silent", "metric",
        "A rounded speech bubble in outline with a short tail at its lower "
        "left, crossed out by a single diagonal slash running corner to corner.",
    ),
    Asset(
        "metric-interruption", "metric",
        "Two sound waves meeting head on: two arcs on the left opening "
        "rightwards and two arcs on the right opening leftwards, with a short "
        "vertical stroke in the gap between them.",
    ),
]

# Panel headings and the one control on the page.
UI = [
    Asset(
        "icon-ehr", "ui",
        "A database drawn as three horizontal cylinders stacked one above "
        "another, the topmost showing its full ellipse.",
    ),
    Asset(
        "icon-tool", "ui",
        "A single wrench lying at a forty-five degree angle, its open jaws at "
        "the upper left and a plain handle running to the lower right.",
    ),
    Asset(
        "icon-transcript", "ui",
        "A rounded speech bubble with a short tail at its lower left, "
        "containing three stacked horizontal lines of clearly different "
        "lengths, as lines of dialogue.",
    ),
    Asset(
        "icon-rehearse", "ui",
        "A play triangle pointing right, centred inside a circle, with a second "
        "broken circular arc outside it ending in a small arrowhead: run it "
        "again.",
    ),
]

BRAND = [
    Asset(
        "empty-quiet", "brand",
        "A quiet clinic reception seen from the front: a wide counter, a "
        "telephone with its handset resting in the cradle standing on the "
        "counter top, and a small potted plant beside it. Leave the upper half "
        "of the frame almost empty.",
        style=SCENE_STYLE, width=160, height=120,
    ),
    Asset(
        "mark-elturno", "brand",
        "A logo mark in a rounded square badge: a telephone handset seen from "
        "the side, centred, with one short clock hand sweeping from the middle "
        "of the badge up to its upper right — a telephone and a turn in a "
        "queue, in one mark.",
        style=BRAND_STYLE, width=32, height=32,
    ),
    Asset(
        "mark-socket-wizard", "brand",
        "A logo mark in a rounded square badge: a stylised electrical wall "
        "socket (two vertical slots side by side, with a small round earth pin "
        "centred below them) and a short magic wand crossing diagonally in "
        "front of the socket from lower left to upper right, tipped with a "
        "simple four-pointed spark — a network socket and a wizard, in one mark.",
        style=BRAND_STYLE, width=32, height=32,
    ),
]

CATALOGUE: list[Asset] = [*STATUS, *OUTCOMES, *METRICS, *UI, *BRAND]
GROUPS = ["status", "outcome", "metric", "ui", "brand"]

BY_NAME = {asset.name: asset for asset in CATALOGUE}


def select(names: list[str] | None = None, groups: list[str] | None = None) -> list[Asset]:
    """The assets a run should cover, or everything when nothing is asked for."""
    if not names and not groups:
        return list(CATALOGUE)
    chosen: list[Asset] = []
    for asset in CATALOGUE:
        if names and asset.name in names:
            chosen.append(asset)
        elif groups and asset.group in groups:
            chosen.append(asset)
    return chosen
