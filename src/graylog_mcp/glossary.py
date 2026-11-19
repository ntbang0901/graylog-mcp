"""The team's own words: business terms and code shapes of the config, found in a question.

Nothing here imports the rest of the package at run time, so the config loader can use ``fold`` to compare aliases.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graylog_mcp.config import CodeKind, Glossary, GlossaryTerm, InstanceConfig


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


@dataclass(frozen=True)
class TermMatch:
    term: GlossaryTerm
    text: str  # what the user wrote, from the question in NFC
    start: int
    end: int
    exact: bool  # False: matched without its diacritics


@dataclass(frozen=True)
class Unclear:
    """Words typed without diacritics that stand for more than one term."""

    text: str
    terms: tuple[str, ...]  # names of the terms it could be, in config order


@dataclass(frozen=True)
class CodeMatch:
    kind: CodeKind
    value: str


def _whole_words(alias: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in alias.split()) + r"(?!\w)")


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in taken)


def match_terms(glossary: Glossary, inst: InstanceConfig, text: str) -> tuple[list[TermMatch], list[Unclear]]:
    """The terms of ``inst`` named in ``text``, in its order, and the words that could be several terms.

    The longest phrase wins. An alias also matches without its diacritics, but only words typed with none: "ma"
    may stand for "mã", "má" never does."""
    typed = unicodedata.normalize("NFC", text)
    folded = fold(typed)
    bare = strip_marks(folded)
    found: list[tuple[tuple[int, int], bool, GlossaryTerm]] = []
    for term in glossary.terms_for(inst):
        spans: set[tuple[int, int]] = set()
        for alias in term.aliases:
            key = fold(alias)
            exact = {m.span() for m in _whole_words(key).finditer(folded)}
            loose = {m.span() for m in _whole_words(strip_marks(key)).finditer(bare)}
            loose = {s for s in loose if folded[s[0] : s[1]] == bare[s[0] : s[1]]} - exact
            found += [(s, True, term) for s in exact - spans]
            found += [(s, False, term) for s in loose - spans]
            spans |= exact | loose
    found.sort(key=lambda f: (f[0][0] - f[0][1], not f[1], f[0][0]))  # longer, then exact, then earlier
    taken: list[tuple[int, int]] = []
    matches: list[TermMatch] = []
    unclear: list[Unclear] = []
    for span, exact_match, term in found:
        if _overlaps(span, taken):
            continue
        taken.append(span)
        written = typed[span[0] : span[1]]
        rivals = [t.name for s, e, t in found if s == span and not e]
        if not exact_match and len(rivals) > 1:
            unclear.append(Unclear(written, tuple(rivals)))
        else:
            matches.append(TermMatch(term, written, span[0], span[1], exact_match))
    once: dict[str, TermMatch] = {}
    for match in sorted(matches, key=lambda m: m.start):
        once.setdefault(match.term.name, match)
    return list(once.values()), unclear


def find_codes(glossary: Glossary, inst: InstanceConfig, text: str) -> list[CodeMatch]:
    """The codes in ``text`` in its order, each once. Where two kinds overlap the longer match stands, then the
    kind declared first."""
    found = [
        (m.span(), order, kind, m.group(0))
        for order, kind in enumerate(glossary.codes_for(inst))
        for m in re.finditer(kind.pattern, text)
        if m.group(0)
    ]
    found.sort(key=lambda f: (f[0][0] - f[0][1], f[1], f[0][0]))
    taken: list[tuple[int, int]] = []
    kept = []
    for span, _, kind, value in found:
        if not _overlaps(span, taken):
            taken.append(span)
            kept.append((span[0], CodeMatch(kind, value)))
    once: dict[str, CodeMatch] = {}
    for _, match in sorted(kept, key=lambda k: k[0]):
        once.setdefault(match.value, match)
    return list(once.values())


def code_kind(glossary: Glossary, inst: InstanceConfig, value: str) -> CodeKind | None:
    """The first kind of code that ``value`` as a whole has the shape of."""
    return next((k for k in glossary.codes_for(inst) if re.fullmatch(k.pattern, value)), None)
