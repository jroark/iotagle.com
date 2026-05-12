"""User-Agent classification.

Three predicates drive iotagle's recent-visitors page:

- :func:`is_vintage` — UA matches a *known* old browser/OS pattern. Drives
  the red ``[v]`` highlight.
- :func:`is_modern_browser` — UA is an obviously current desktop or mobile
  browser (Chrome/Firefox/Safari/Edge with recent version numbers, plus
  Trident/MSIE 6-11 which are enterprise-legacy but not vintage-interesting,
  plus social in-app browsers).
- :func:`is_bot` — UA is automation: search-engine crawlers, AI training
  bots, HTTP libraries (curl, wget, Go-http-client, python-requests, …),
  uptime monitors.

The composite :func:`is_interesting` is what the ``before_request`` hook
gates on. The rule is intentionally lenient: a UA is interesting if it's
vintage, OR if it's neither modern nor a bot (unknown ≈ obscure ≈ worth
showing). That keeps weird embedded clients we haven't predicted on the
list rather than silently filtered.
"""

from __future__ import annotations

import re

# Whole-word/phrase patterns. Compiled once at import time.
# Matching is case-insensitive on the full UA string.
_VINTAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
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
    # Obscure embedded / handheld / niche browsers worth flagging
    # PicoGUI was an embedded UI framework; AtomicNavigator was a tiny browser
    # built on it. nxweb-be300 is the Casio Cassiopeia BE-300 PDA browser.
    re.compile(r"\b(AtomicNavigator|PicoGUI|nxweb|nx-web)\b", re.I),
    # Amiga browsers
    re.compile(r"\b(IBrowse|AWeb|Voyager|MUI(?:OWB)?|OWB|Origyn|MorphOS|AROS)\b", re.I),
    # BeOS / Haiku / NeXT / OS/2
    re.compile(r"\b(NetPositive|BeOS|Haiku|WebPositive|OS/2|Warp)\b", re.I),
    # Old desktop Unix browsers
    re.compile(r"\b(Galeon|Epiphany|HotJava|OmniWeb|Camino|Mothra|Espial)\b", re.I),
    # Early mobile / PDA browsers
    re.compile(r"\b(Skyfire|Bolt|Doris|MicroB|Maemo|Openwave|UP\.Browser|Teleca)\b", re.I),
    re.compile(r"\b(AvantGo|PalmSource|Blazer|EudoraWeb|Xiino|PocketLink)\b", re.I),
    re.compile(r"\b(Pocket ?IE|PIE/|IEMobile)\b", re.I),
)

# Patterns that mark a UA as obviously modern. These are checked both as a
# negative list against :func:`is_vintage` (so a modern UA doesn't get the
# vintage flag because it happens to contain "Mac OS 9" in some token) and
# as a positive filter in :func:`is_modern_browser`.
_MODERN_BROWSER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Chrome / Chromium 60+
    re.compile(r"\bChrome/(?:[6-9]\d|\d{3,})\.", re.I),
    # Firefox 60+
    re.compile(r"\bFirefox/(?:[6-9]\d|\d{3,})\.", re.I),
    # Modern Edge: Chromium-based ``Edg/``; legacy EdgeHTML ``Edge/``;
    # mobile variants ``EdgA/`` (Android) and ``EdgiOS/`` (iOS).
    re.compile(r"\bEdg(?:e|A|iOS)?/", re.I),
    # Modern Opera (Chromium-based)
    re.compile(r"\bOPR/\d+\.", re.I),
    # Modern Safari (desktop and mobile): ``Version/10`` and up.
    re.compile(r"\bVersion/(?:1[0-9]|[2-9]\d|\d{3,})\.", re.I),
    # In-system mobile/embedded browsers based on Chromium or WebKit
    re.compile(r"\b(CriOS|FxiOS|SamsungBrowser|GSA|YaBrowser|Brave|Vivaldi)/", re.I),
    re.compile(r"\b(HeadlessChrome|PhantomJS)/", re.I),
    # IE 6 through 11: not modern in spirit, but not vintage-interesting
    # either; they're enterprise legacy. Treat as "modern" for the purposes
    # of filtering them off the page.
    re.compile(r"\bMSIE (?:[6-9]|1[01])\.", re.I),
    re.compile(r"\bTrident/[4-7]\.", re.I),
    # Social/messaging in-app browsers
    re.compile(r"\b(FBAV|FBAN|FB_IAB|FBIOS|Instagram|TikTok|Snapchat)\b", re.I),
    # WeChat / DingTalk / Line
    re.compile(r"\b(MicroMessenger|DingTalk|Line)/", re.I),
)


# Bot / crawler / HTTP-library patterns. Substring-y on purpose: anything
# that looks remotely like automation is dropped. ``libwww`` would catch
# Lynx if not for the order — :func:`is_interesting` checks vintage first.
_BOT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Generic "I'm a bot" tokens
    re.compile(r"\b(bot|crawler|spider|scraper|fetcher|indexer)\b", re.I),
    # Known crawler brand names (catches some that don't include "bot")
    re.compile(
        r"\b(google|bing|baidu|yandex|duckduck|ahrefs|semrush|seznam|sogou|"
        r"applebot|petalbot|mojeek|qwantify|coccocbot)",
        re.I,
    ),
    # AI training / retrieval bots
    re.compile(
        r"\b(gptbot|chatgpt-user|oai-searchbot|perplexity|claudebot|"
        r"anthropic-ai|google-extended|ccbot|amazonbot|cohere-?ai|"
        r"diffbot|bytespider|youbot|meta-externalagent)\b",
        re.I,
    ),
    # HTTP libraries / scripting clients
    re.compile(
        r"\b(curl|wget|libwww-perl|lwp|python-requests|python-urllib|"
        r"httpx|aiohttp|httpie|node-fetch|axios|got|undici|"
        r"go-http-client|java|okhttp|apache-httpclient|jakarta|"
        r"ruby|guzzle|symfony|reqwest|hyper|ureq|reqwest)/",
        re.I,
    ),
    # Uptime / monitoring / status pollers
    re.compile(
        r"\b(pingdom|uptimerobot|statuscake|datadog|nagios|zabbix|"
        r"prometheus|blackbox|newrelic|catchpoint|appoptics|dotcom-monitor|"
        r"site24x7|gtmetrix|sentry|healthchecks)\b",
        re.I,
    ),
    # Social-platform link-preview crawlers
    re.compile(
        r"\b(facebookexternalhit|linkedinbot|twitterbot|slackbot|"
        r"telegrambot|whatsapp|discordbot|redditbot|skypeuripreview|"
        r"applenewsbot|tumblr|pinterest)\b",
        re.I,
    ),
    # Headless / scraping frameworks. ``HeadlessChrome`` also matches the
    # modern-browser pattern; matching here too is harmless and means callers
    # of either ``is_bot`` or ``is_modern_browser`` agree it's automation.
    re.compile(
        r"\b(headlesschrome|puppeteer|playwright|selenium|nightmare|"
        r"phantomjs|scrapy|colly|chromedp|jsdom)\b",
        re.I,
    ),
    # Validators / linters / RSS readers that aren't humans
    re.compile(r"\b(w3c_validator|jigsaw|feedfetcher|feedburner|feedly|inoreader)\b", re.I),
)


def is_vintage(ua: str | None) -> bool:
    """Return True if ``ua`` matches a known vintage browser/OS pattern.

    Modern browsers are excluded first: a brand-new Chrome that happens to
    contain a token like ``Mac OS 9`` in some compatibility string won't
    earn the vintage flag.
    """
    if not ua:
        return False
    for neg in _MODERN_BROWSER_PATTERNS:
        if neg.search(ua):
            return False
    return any(p.search(ua) for p in _VINTAGE_PATTERNS)


def is_modern_browser(ua: str | None) -> bool:
    """Return True if ``ua`` is clearly a current desktop or mobile browser."""
    if not ua:
        return False
    return any(p.search(ua) for p in _MODERN_BROWSER_PATTERNS)


def is_bot(ua: str | None) -> bool:
    """Return True if ``ua`` looks like a crawler, monitor, or scripting client."""
    if not ua:
        return False
    return any(p.search(ua) for p in _BOT_PATTERNS)


def is_interesting(ua: str | None) -> bool:
    """Return True if ``ua`` should be recorded on the /visitors page.

    Vintage UAs are always interesting. Beyond that, anything that isn't
    obviously a modern browser or a bot makes the cut — that's the "obscure"
    bucket the page exists to surface (weird embedded clients, hobbyist
    browsers, niche operating systems we haven't predicted).
    """
    if not ua:
        return False
    if is_vintage(ua):
        return True
    if is_modern_browser(ua):
        return False
    if is_bot(ua):
        return False
    return True


__all__ = ("is_bot", "is_interesting", "is_modern_browser", "is_vintage")
