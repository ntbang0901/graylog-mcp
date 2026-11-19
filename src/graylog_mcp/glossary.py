"""The team's own words: business terms and code shapes of the config, found in a question.

Nothing here imports the rest of the package at run time, so the config loader can use ``fold`` to compare aliases.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graylog_mcp.config import CodeKind, ErrorWords, Glossary, GlossaryTerm, InstanceConfig


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


_TONE_NAMES = ("ACUTE ACCENT", "GRAVE ACCENT", "HOOK ABOVE", "TILDE", "DOT BELOW")
_TONES = frozenset(unicodedata.lookup(f"COMBINING {name}") for name in _TONE_NAMES)
_VOWELS = frozenset("aăâeêioôơuưy")
_WORD = re.compile(r"[^\W\d_]+")


def _settle_tones(folded: str) -> str:
    """``folded`` with the tone mark of each word on its first vowel, character for character: Vietnamese writes
    one word both ways (hoá, hóa), and a reader of either means the same word."""
    out = list(folded)
    for word in _WORD.finditer(folded):
        parts = [unicodedata.normalize("NFD", c) for c in word.group()]
        toned = [i for i, part in enumerate(parts) if any(mark in _TONES for mark in part[1:])]
        if len(toned) != 1:
            continue
        at = toned[0]
        tone = next(mark for mark in parts[at][1:] if mark in _TONES)
        parts[at] = parts[at].replace(tone, "")
        plain = [unicodedata.normalize("NFC", part) for part in parts]
        if plain[at] not in _VOWELS:
            continue  # a mark on a consonant is not a Vietnamese tone
        first = next(i for i, char in enumerate(plain) if char in _VOWELS)
        moved = unicodedata.normalize("NFC", parts[first] + tone)
        if len(moved) == 1:
            out[word.start() + at] = plain[at]
            out[word.start() + first] = moved
    return "".join(out)


def alias_key(alias: str) -> str:
    """What makes two aliases the same words: case, spacing and the place of a tone mark do not."""
    return " ".join(_settle_tones(fold(alias)).split())


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
    terms = {term.name: term for term in glossary.terms_for(inst)}
    typed, kept, unclear = _read({name: term.aliases for name, term in terms.items()}, text)
    once: dict[str, TermMatch] = {}
    for (start, end), exact, name in kept:
        once.setdefault(name, TermMatch(terms[name], typed[start:end], start, end, exact))
    return list(once.values()), unclear


def match_words(groups: Mapping[str, Sequence[str]], text: str) -> list[tuple[str, str, int, int]]:
    """Every place ``text`` holds a word of ``groups``, read like a term's alias: (group, the words as typed, start,
    end), in the order of the text. Words that fit two groups are left out."""
    typed, kept, _ = _read(groups, text)
    return [(name, typed[start:end], start, end) for (start, end), _, name in kept]


def _read(
    groups: Mapping[str, Sequence[str]], text: str
) -> tuple[str, list[tuple[tuple[int, int], bool, str]], list[Unclear]]:
    """``text`` in NFC, the matches kept in its order as (span, typed with its marks, group), and the unclear words."""
    typed = unicodedata.normalize("NFC", text)
    folded = _settle_tones(fold(typed))
    bare = strip_marks(folded)
    found: list[tuple[tuple[int, int], bool, str]] = []
    for name, aliases in groups.items():
        keys = [_settle_tones(fold(alias)) for alias in aliases]
        # every alias as written before any alias without its marks: "ma" listed beside "mã" is an exact word
        exact = {m.span() for key in keys for m in _whole_words(key).finditer(folded)}
        loose = {m.span() for key in keys for m in _whole_words(strip_marks(key)).finditer(bare)}
        loose = {s for s in loose if folded[s[0] : s[1]] == bare[s[0] : s[1]]} - exact
        found += [(s, True, name) for s in sorted(exact)] + [(s, False, name) for s in sorted(loose)]
    found.sort(key=lambda f: (f[0][0] - f[0][1], not f[1], f[0][0]))  # longer, then exact, then earlier
    taken: list[tuple[int, int]] = []
    kept: list[tuple[tuple[int, int], bool, str]] = []
    unclear: list[Unclear] = []
    for span, exact_match, name in found:
        if _overlaps(span, taken):
            continue
        taken.append(span)
        rivals = [n for s, e, n in found if s == span and not e]
        if not exact_match and len(rivals) > 1:
            if (word := Unclear(typed[span[0] : span[1]], tuple(rivals))) not in unclear:
                unclear.append(word)
        else:
            kept.append((span, exact_match, name))
    return typed, sorted(kept), unclear


def error_kind(words: ErrorWords, text: str) -> tuple[str, str] | None:
    """Which errors ``text`` speaks of, "technical", "business" or "both", with the words that say so; None when it
    names none. A bare "error" means both kinds."""
    groups = {"either": words.either, "technical": words.technical, "business": words.business}
    found = match_words(groups, text)
    if not found:
        return None
    named = {name for name, *_ in found} - {"either"}
    kind = named.pop() if len(named) == 1 else "both"
    return kind, " ".join(written for _, written, *_ in found)


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
