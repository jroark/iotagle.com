"""Classify whether a User-Agent string belongs to a vintage browser/OS.

This is a deliberately conservative heuristic: a UA only counts as "vintage"
if it matches a pattern strongly suggesting an old system that iotagle is
*specifically* useful for. Modern browsers running on top of macOS 14 do not
qualify, even though "macOS" appears in their UA string.

The output drives a visual marker on the /visitors page; misclassification
costs only a missing or extra highlight, never an error, so we err toward
specific patterns.
"""

from __future__ import annotations

import re

# Whole-word/phrase patterns. Compiled once at import time.
# Matching is case-insensitive on the full UA string.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Text browsers and explicitly retro browsers
    re.compile(r"\b(lynx|w3m|links|elinks|netsurf|dillo)\b", re.I),
    re.compile(r"\b(arachne|amaya|mothra|icab|macweb|cyberdog|emacs-w3)\b", re.I),
    # Mosaic — but exclude "ncsa_mosaic_archive" or similar bot strings
    re.compile(r"\bmosaic[/ ]", re.I),
    # Old Netscape (1.x–4.x). Modern stuff is "Mozilla/5.0 (... Firefox/...)";
    # we only want the genuinely-old Netscape series.
    re.compile(r"\bMozilla/[1-4]\.", re.I),
    re.compile(r"\bNetscape[36]?/[1-4]\.", re.I),
    # Old IE: MSIE 1–5
    re.compile(r"\bMSIE [1-5]\.", re.I),
    # Old Opera (presto era, 1–9)
    re.compile(r"\bOpera/[1-9]\.", re.I),
    # Classic Mac OS
    re.compile(r"\bMac OS [789](?:\.|\b)", re.I),
    re.compile(r"\bMac_PowerPC\b", re.I),
    re.compile(r"\bClassic Mac\b", re.I),
    # Pre-XP Windows
    re.compile(r"\bWindows (?:3\.1|95|98|ME|NT 4)\b", re.I),
    re.compile(r"\bWin(?:dows)? ?CE\b", re.I),
    # DOS-era and 16-bit
    re.compile(r"\bDOS\b"),  # case-sensitive: avoid matching "doSomething"
    re.compile(r"\b(win16|wince|wm5|palmos|psion|symbian|symbos|nokiabrowser)\b", re.I),
    # Apple ][, Apple //
    re.compile(r"\bApple ?(?://|II)\b"),
    # PlayStation 1/2, original Xbox, GBA — sometimes get vintage browsers
    re.compile(r"\bPlayStation [123]\b", re.I),
    # iCab on classic Mac shows up as "iCab/2" or "iCab/3" — match generously
    re.compile(r"\biCab\b", re.I),
    # Wii / 3DS web browsers
    re.compile(r"\b(Nintendo(?: Wii| 3DS)?|Wii)\b"),
)

# Negative list: matches first; if any hit, we never call it vintage. Avoids
# false positives from modern UAs that happen to embed an old version token.
_NEGATIVE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bChrome/[6-9]\d\.|\bChrome/\d{3}\.", re.I),  # Chrome >= 60
    re.compile(r"\bFirefox/[6-9]\d\.|\bFirefox/\d{3}\.", re.I),  # FF >= 60
    re.compile(r"\bEdg(?:e|A|iOS)?/", re.I),  # any modern Edge
    re.compile(r"\bSafari/[5-9]\d\d\.|\bSafari/\d{4}\.", re.I),  # modern Safari
)


def is_vintage(ua: str | None) -> bool:
    """Return True if ``ua`` looks like a vintage browser."""
    if not ua:
        return False
    for neg in _NEGATIVE:
        if neg.search(ua):
            return False
    return any(p.search(ua) for p in _PATTERNS)


__all__ = ("is_vintage",)
