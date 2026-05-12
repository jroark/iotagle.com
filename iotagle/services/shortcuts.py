"""Query-prefix shortcuts.

The only shortcut in v1 is the Wikipedia bounce: ``w:apple ii`` or
``wiki apple ii`` short-circuits the DDG round-trip and sends the user
straight to a transcoded Wikipedia article.

The function returns a *URL to redirect to* — usually a ``/read?url=…``
target. Routes pick what status code to use (302 in our case, so vintage
browsers don't cache the bounce).
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

WIKI_BASE = "https://en.wikipedia.org/wiki/"

# Each prefix is matched case-insensitively. The matching is *prefix on the
# trimmed query*, not a regex, so we don't accidentally strip "wike " or
# similar misspellings.
_PREFIXES: tuple[str, ...] = ("w:", "wiki ")


def maybe_shortcut(query: str) -> str | None:
    """Return a redirect target for shortcut queries, or None for normal ones."""
    if not isinstance(query, str):
        return None
    q = query.strip()
    if not q:
        return None
    lower = q.lower()

    for prefix in _PREFIXES:
        if lower.startswith(prefix):
            term = q[len(prefix) :].strip()
            if not term:
                return None
            # Wikipedia uses underscores for word separators in URLs.
            article = term.replace(" ", "_")
            # ``safe="_"`` keeps the underscore from being percent-encoded, and
            # ``:`` is needed for "Category:Foo" / "Talk:Foo" style targets.
            wiki_url = WIKI_BASE + quote(article, safe="_:")
            return "/read?" + urlencode({"url": wiki_url})

    return None


__all__ = ("maybe_shortcut",)
