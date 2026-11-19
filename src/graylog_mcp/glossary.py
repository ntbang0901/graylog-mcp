"""The team's own words: business terms and code shapes of the config, found in a question.

Nothing here imports the rest of the package at run time, so the config loader can use ``fold`` to compare aliases.
"""

from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """``text`` in NFC and lower case, as long as its NFC form: positions in one are positions in the other."""
    # a letter whose lower case is two characters keeps its case, or every later position would shift
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in unicodedata.normalize("NFC", text))


def _bare(char: str) -> str:
    if char in "đĐ":  # no decomposition: the stroke is part of the letter
        return "d" if char == "đ" else "D"
    base = "".join(c for c in unicodedata.normalize("NFD", char) if unicodedata.category(c) != "Mn")
    return base if len(base) == 1 else char


def strip_marks(text: str) -> str:
    """``text`` without its diacritics, character for character."""
    return "".join(_bare(c) for c in text)
