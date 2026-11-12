"""The pictures of the customers' path (shops/customer.py), drawn here as SVG, with no text in them.

They are generic on purpose: a phone, a sheet of paper, a code. No screenshot of a real app, no logo of a wallet, no
name: what a customer sees in their own wallet is never quite like a picture, and a picture that differs from the
screen makes them think they are doing it wrong. The words next to the picture say what to do; the picture only shows
where to look.

The colours come from the stylesheet (static/customer.css), so they follow the community's theme, dark mode included.
Nothing here is built from data that arrives from outside: every number below is fixed.
"""
from django.utils.safestring import mark_safe

# A 9x9 grid of fixed dots between three squares: it looks like a QR code and is not one, so no camera reads it and a
# picture can never send a wallet anywhere
CODE_DOTS = ("000000000", "000000000", "000000000", "010110100", "101001011", "010111010", "000010101", "000101110",
             "000011001")
FINDERS = ((0, 0), (6, 0), (0, 6))


def svg(inner):
    return mark_safe(f'<svg class="art" viewBox="0 0 160 120" aria-hidden="true" focusable="false">{inner}</svg>')


def num(value):
    return f"{value:g}"


def phone(x, y, screen="", width=48, height=92):
    """A phone standing up: body, screen and a small speaker, with `screen` drawn on top."""
    return (f'<rect class="art-body" x="{x}" y="{y}" width="{width}" height="{height}" rx="8"/>'
            f'<rect class="art-screen" x="{x + 5}" y="{y + 10}" width="{width - 10}" height="{height - 20}" rx="3"/>'
            f'<line class="art-line art-thin" x1="{num(x + width / 2 - 6)}" x2="{num(x + width / 2 + 6)}" y1="{y + 5}" y2="{y + 5}"/>'
            f'{screen}')


def code(x, y, cell):
    """The look of a QR code: three squares in the corners and dots in between, `cell` units a dot."""
    parts = []
    for column, row in FINDERS:
        parts.append(f'<rect class="art-ink" x="{num(x + column * cell)}" y="{num(y + row * cell)}" '
                     f'width="{num(3 * cell)}" height="{num(3 * cell)}"/>'
                     f'<rect class="art-paper" x="{num(x + (column + 0.6) * cell)}" y="{num(y + (row + 0.6) * cell)}" '
                     f'width="{num(1.8 * cell)}" height="{num(1.8 * cell)}"/>'
                     f'<rect class="art-ink" x="{num(x + (column + 1) * cell)}" y="{num(y + (row + 1) * cell)}" '
                     f'width="{num(cell)}" height="{num(cell)}"/>')
    for row, dots in enumerate(CODE_DOTS):
        for column, dot in enumerate(dots):
            in_finder = any(0 <= column - fx < 3 and 0 <= row - fy < 3 for fx, fy in FINDERS)
            if dot == "1" and not in_finder:
                parts.append(f'<rect class="art-ink" x="{num(x + column * cell)}" y="{num(y + row * cell)}" '
                             f'width="{num(cell)}" height="{num(cell)}"/>')
    return "".join(parts)


def tick(cx, cy, radius):
    return (f'<circle class="art-good" cx="{cx}" cy="{cy}" r="{radius}"/>'
            f'<path class="art-line art-on-good" d="M{num(cx - radius * 0.45)} {cy} l{num(radius * 0.3)} {num(radius * 0.32)} '
            f'l{num(radius * 0.6)} {num(-radius * 0.68)}"/>')


def corners(x, y, size, arm=7):
    """The four corners of a viewfinder, around what the camera looks at."""
    far = x + size
    low = y + size
    return (f'<path class="art-line art-focus" d="M{x} {y + arm} V{y} H{x + arm} M{far - arm} {y} H{far} V{y + arm} '
            f'M{far} {low - arm} V{low} H{far - arm} M{x + arm} {low} H{x} V{low - arm}"/>')


def arrow(x, y, length=22):
    """An arrow to the right, from (x, y)."""
    return f'<g class="art-line"><path d="M{x} {y} h{length}"/><path d="M{x + length - 7} {y - 7} l7 7 -7 7"/></g>'


def store():
    """Choosing and installing: a phone with a search bar and a few apps, one marked, and the app that arrives."""
    tiles = "".join(f'<rect class="art-soft" x="{30 + column * 14}" y="{42 + row * 14}" width="10" height="10" rx="3"/>'
                    for row in range(3) for column in range(2) if (row, column) != (1, 1))
    return svg(phone(24, 14, '<rect class="art-paper" x="29" y="28" width="30" height="8" rx="4"/>' + tiles +
                     '<rect class="art-hi art-outline" x="44" y="56" width="10" height="10" rx="3"/>') +
               arrow(80, 60) +
               '<rect class="art-hi art-outline" x="110" y="45" width="30" height="30" rx="8"/>'
               '<path class="art-line" d="M125 52 v14 m-6 -6 l6 6 6 -6"/>')


def paper():
    """The words on paper: a sheet with twelve numbered lines, and a pencil."""
    lines = "".join(f'<rect class="art-ink" x="{24 + column * 30}" y="{26 + row * 12}" width="5" height="5" rx="1"/>'
                    f'<line class="art-line art-thin" x1="{33 + column * 30}" x2="{33 + column * 30 + 17}" '
                    f'y1="{29 + row * 12}" y2="{29 + row * 12}"/>' for column in range(2) for row in range(6))
    return svg('<rect class="art-paper art-outline" x="16" y="12" width="76" height="92" rx="4"/>' + lines +
               '<g transform="rotate(35 120 50)"><rect class="art-hi art-outline" x="114" y="18" width="12" height="52" rx="2"/>'
               '<path class="art-ink" d="M114 70 h12 l-6 12 z"/></g>')


def receive():
    """Receiving: a phone showing a code, and a coin that falls into it."""
    return svg(phone(28, 14, '<rect class="art-paper" x="35" y="34" width="34" height="34" rx="2"/>' + code(38, 37, 3)) +
               '<circle class="art-hi art-outline" cx="112" cy="30" r="12"/><path class="art-line" d="M112 25 v10 M107 30 h10"/>'
               '<g class="art-line"><path d="M112 48 V70"/><path d="M104 62 l8 8 8 -8"/></g>')


def scan():
    """Paying: the shop's phone with the code, and the customer's phone looking at it."""
    return svg('<rect class="art-counter" x="76" y="102" width="80" height="6" rx="2"/>'
               '<rect class="art-body" x="86" y="16" width="52" height="86" rx="7"/>'
               '<rect class="art-paper" x="92" y="24" width="40" height="40" rx="2"/>' + code(96, 28, 4) +
               '<rect class="art-hi" x="94" y="72" width="36" height="10" rx="3"/>' +
               phone(14, 26, '<rect class="art-soft" x="26" y="52" width="24" height="24" rx="2"/>' + corners(24, 50, 28), height=84) +
               '<g class="art-line art-thin"><path d="M66 50 H80" stroke-dasharray="3 4"/></g>')


def confirm():
    """Confirming: the amount on the phone and the button to press."""
    return svg(phone(46, 12, '<rect class="art-soft" x="56" y="32" width="28" height="9" rx="3"/>'
                             '<rect class="art-soft" x="56" y="46" width="18" height="5" rx="2"/>'
                             '<rect class="art-hi art-outline" x="55" y="68" width="30" height="12" rx="6"/>') +
               '<g class="art-line"><path d="M108 74 q10 -2 14 -16"/><path d="M124 66 l-2 -9 -9 3"/></g>')


def done():
    """Paid: the phone with a tick."""
    return svg(phone(56, 12, tick(80, 56, 16) + '<rect class="art-soft" x="68" y="82" width="24" height="5" rx="2"/>') +
               '<g class="art-line"><path d="M34 40 l-7 -8"/><path d="M126 40 l7 -8"/><path d="M28 66 h-10"/><path d="M132 66 h10"/></g>')


def problem():
    """When something does not work: a phone with a quiet warning mark."""
    return svg(phone(56, 12, '<circle class="art-hi art-outline" cx="80" cy="56" r="16"/>'
                             '<path class="art-line" d="M80 47 v11"/><circle class="art-ink" cx="80" cy="65" r="1.8"/>'))


PICTURES = {"store": store, "paper": paper, "receive": receive, "scan": scan, "confirm": confirm, "done": done,
            "problem": problem}


def picture(name):
    return PICTURES[name]()
