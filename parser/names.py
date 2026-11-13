"""
Name normalisation shared by the log parser and the Google Sheets lookups.

Names typed in Discord and names typed in the sheet often differ in ways a
human wouldn't notice: bold/italic markdown, a leading "@", invisible
zero-width characters, non-breaking spaces, "fancy font" letters, escaped
underscores, accents, or different capitalisation. Comparing raw text made
real players show up as "not found". Always compare through name_key().
"""

from __future__ import annotations

import re
import unicodedata

# Discord markdown that can wrap or sit inside a name: bold/italic (*), code (`),
# strikethrough (~), escapes (\), spoilers (||), underline (__), quotes (>).
_MARKDOWN_RUNS = re.compile(r"\*+|`+|~~|\|\||__|\\")
_EDGE_JUNK = " \t@_\"'“”‘’<>.,;:!?-–—"
_SEPARATORS = re.compile(r"[\s_.\-]+")


def _drop_invisible(text: str) -> str:
    # Category Cf covers zero-width spaces/joiners, BOMs, direction marks, etc.
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def strip_markdown(text: str) -> str:
    """Remove Discord markdown/escapes and invisible characters, keep the words."""
    text = _drop_invisible(unicodedata.normalize("NFKC", text))
    text = text.replace(" ", " ")
    return _MARKDOWN_RUNS.sub("", text)


def clean_name(text: str) -> str:
    """A display-friendly cleaned name: markdown, '@' and stray edge punctuation removed."""
    text = strip_markdown(text)
    text = " ".join(text.split())
    return text.strip(_EDGE_JUNK)


def name_key(text: str | None) -> str:
    """
    Comparison key for a username or lore name.

    "**@Smaxhy**", "smaxhy​", "𝐒𝐦𝐚𝐱𝐡𝐲" and " SMAXHY " all give "smaxhy".
    Accents are dropped so "Förscher" == "Forscher".
    """
    if not text:
        return ""
    text = clean_name(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.casefold()


def loose_key(text: str | None) -> str:
    """name_key with spaces, underscores, dots and dashes removed ("Smax_hy" == "smax hy")."""
    return _SEPARATORS.sub("", name_key(text))
