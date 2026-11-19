"""What a question asks for and where, read by rule from its words.

Words that name an intent ("how many", "so với", "nguyên nhân") are read for sure. An error word or a term alone
only suggests one: that reading is a guess, said as one, with the other readings it could have been.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass

from graylog_mcp.config import ENVIRONMENT_WORDS, Config, ConfigError, ErrorWords
from graylog_mcp.glossary import alias_key, error_kind, match_words

# where two intents are named the first of these is meant: "how many more than yesterday" compares
ORDER = ("compare", "count", "root_cause", "error_summary", "follow_flow", "search")
UNREAD = ("follow_flow", "error_summary", "search")  # offered when nothing was read


@dataclass(frozen=True)
class IntentRead:
    value: str | None  # a key of understand.INTENTS; None: not read
    sure: bool  # read from words that name it; False: a guess
    text: str | None  # the words it was read from
    others: tuple[str, ...]  # the readings it could also be


def read_intent(
    words: Mapping[str, tuple[str, ...]], errors: ErrorWords, text: str, has_code: bool, has_term: bool
) -> IntentRead:
    """``has_code``: the question holds a code of the glossary. ``has_term``: it names a term of the team."""
    found = match_words(words, text)
    named = {name for name, *_ in found}
    said = {name: " ".join(written for group, written, *_ in found if group == name) for name in named}
    if has_code or "trace_code" in named:
        return IntentRead("trace_code", True, said.get("trace_code"), ())
    if {"why", "whole_system"} <= named:
        named.add("root_cause")
        said.setdefault("root_cause", f"{said['why']} {said['whole_system']}")
    stated = [name for name in ORDER if name in named]
    if stated:
        return IntentRead(stated[0], True, said[stated[0]], tuple(stated[1:]))
    kind = error_kind(errors, text)
    if "why" in named or kind:
        return IntentRead("follow_flow", False, said.get("why") or (kind[1] if kind else None), _others("follow_flow"))
    if has_term:
        return IntentRead("search", False, None, _others("search"))
    return IntentRead(None, False, None, UNREAD)


def _others(guess: str) -> tuple[str, ...]:
    return ("error_summary", "root_cause") if guess == "follow_flow" else ("error_summary", "follow_flow")


@dataclass(frozen=True)
class InstanceRead:
    name: str | None  # None: the words fit several instances, listed in ``options``
    text: str  # the words it was read from
    options: tuple[str, ...] = ()


def read_instance(config: Config, text: str) -> InstanceRead | None:
    """The instance ``text`` names: by an alias, its name, its environment or its group. None when it names none.

    Each word stands for the instances it may mean: an alias for every instance that carries it, an environment
    or a group for its members. Several words narrow each other ("shop prod"); words that exclude each other
    ("prod and dev"), or one that still fits several, are left to the asker."""
    instances = list(config.instances.values())
    meant: dict[str, set[str]] = {}  # a word, folded -> the instances it may mean
    spelled: dict[str, str] = {}

    def means(word: str, names: list[str]) -> None:
        key = alias_key(word)
        spelled.setdefault(key, word)
        meant.setdefault(key, set()).update(names)

    for inst in instances:
        # an instance without a group is known by its name alone: "staging" is also "stg"
        own = [inst.name, *inst.aliases, *([] if inst.group else _other_words(inst.name))]
        for word in own:
            means(word, [inst.name])
    for env in {i.environment for i in instances if i.environment}:
        for word in (env, *_other_words(env)):
            means(word, [i.name for i in instances if i.environment == env])
    for group in {i.group for i in instances if i.group}:
        means(group, [i.name for i in instances if i.group == group])

    found = match_words({key: [word] for key, word in spelled.items()}, text)
    if not found:
        return None
    said = " ".join(dict.fromkeys(written for _, written, *_ in found))
    sets = [meant[key] for key, *_ in found]
    pool = set.intersection(*sets)
    if not pool:  # the words exclude each other: every instance one of them names is a choice
        return InstanceRead(None, said, tuple(sorted(set.union(*sets))))
    if len(pool) > 1 and config.default_group:
        pool = {name for name in pool if config.instances[name].group == config.default_group} or pool
    if len(pool) == 1:
        return InstanceRead(next(iter(pool)), said)
    groups = {config.instances[name].group for name in pool}
    if len(groups) == 1 and (only := next(iter(groups))):
        chosen: str | None = None
        with contextlib.suppress(ConfigError):
            # one group, whichever word named it: its default environment, as its name alone means elsewhere
            chosen = config.instance(only).name
        if chosen in pool:
            return InstanceRead(chosen, said)
    return InstanceRead(None, said, tuple(sorted(pool)))


def _other_words(name: str) -> list[str]:
    known = next((words for words in ENVIRONMENT_WORDS if name.lower() in words), ())
    return [w for w in known if w != name.lower()]
