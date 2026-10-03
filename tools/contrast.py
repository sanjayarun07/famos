"""WCAG contrast for every text-on-background pair the console actually uses.

A dark theme is easy to write and easy to get wrong in a way that only shows
up on somebody else's screen. This is the part a screenshot would not tell me
anyway: whether the numbers hold.
"""
import pathlib
import re
import sys

css = pathlib.Path("familyos/web/app.css").read_text()


def block(start, end=None):
    """The tokens between two markers, without the leading dashes."""
    a = css.index(start)
    b = css.index(end, a + 1) if end else len(css)
    return {k[2:]: v for k, v in re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", css[a:b])}


dark = block(":root {", ':root[data-theme="light"]')
light = {**dark, **block(':root[data-theme="light"]', "/* The old notice")}
# The old-notice pair sits in its own two rules at the end, dark first.
tail = css.index("/* The old notice")
dark.update(block(":root { --old-tint", ':root[data-theme="light"] { --old-tint'))
light.update(block(':root[data-theme="light"] { --old-tint', None))


def rgb(value, over=None):
    value = value.strip()
    if value.startswith("rgba"):
        r, g, b, a = (float(x) for x in re.findall(r"[\d.]+", value))
        br, bg, bb = over
        return (r * a + br * (1 - a), g * a + bg * (1 - a), b * a + bb * (1 - a))
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(c):
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def ratio(fg, bg, tokens):
    b = rgb(tokens[bg])
    a = rgb(tokens[fg], over=b)
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# (text, background, minimum, what it is)
PAIRS = [
    ("ink", "paper", 4.5, "body text on the ground"),
    ("ink", "card", 4.5, "body text in a card"),
    ("ink", "sunk", 4.5, "body text in a recessed row"),
    ("ink-2", "card", 4.5, "secondary text"),
    ("ink-3", "card", 4.5, "muted text (.muted, .sub)"),
    ("ink-3", "paper", 4.5, "muted text on the ground"),
    ("ink-3", "sunk", 4.5, "muted text in a recessed row"),
    ("faint", "card", 3.0, "faint text"),
    ("primary", "paper", 4.5, "a link"),
    ("primary", "card", 4.5, "a link in a card"),
    ("primary-dark", "card", 4.5, "a hovered link"),
    ("on-solid", "primary", 4.5, "button label"),
    ("on-ink", "ink", 4.5, "a selected pill / solid tag / toast"),
    ("on-amber", "amber-line", 4.5, "the claim number on its marker"),
    ("amber-ink", "amber-tint", 4.5, "amber text on amber"),
    ("green", "green-tint", 4.5, "a green tag"),
    ("rust", "rust-tint", 4.5, "a rust tag"),
    ("red", "red-tint", 4.5, "a red tag"),
    ("red-ink", "red-tint", 4.5, "the consent warning"),
    ("code-ink", "code-bg", 4.5, "a code block"),
    ("ink", "primary-tint", 4.5, "text on the primary tint"),
    ("ink-2", "stage", 4.5, "the note under a rendered page"),
    ("ink", "old-tint", 4.5, "the superseded side of a revision"),
]

bad = 0
for name, tokens in (("dark (default)", dark), ("light", light)):
    print(f"\n{name}")
    print(f"  {'pair':34} {'ratio':>6}  {'min':>4}")
    for fg, bg, floor, what in PAIRS:
        r = ratio(fg, bg, tokens)
        ok = r >= floor
        if not ok:
            bad += 1
        print(f"  {'FAIL ' if not ok else '     '}{fg} on {bg:<18} {r:5.2f}  {floor:>4}  {what}")

# The highlight is not text, but it has to be visible over a white page.
# The highlight is not text. It has to be *seen* over a page, and the page is
# white in both themes, so neither it nor the page may be themed.
page = {"glow": dark["amber-glow"], "line": dark["amber-line"], "page": dark["page"]}
print(f"\na page is {dark['page']} in dark and {light['page']} in light "
      f"(it is paper, and the quote highlighting points at it)")
print(f"the highlight wash over a page: {ratio('glow', 'page', page):.2f}:1 (seen, not read)")
print(f"its border over a page:         {ratio('line', 'page', page):.2f}:1")
print(f"\n{bad} pair(s) below the floor" if bad else "\nevery pair clears its floor")
sys.exit(1 if bad else 0)
