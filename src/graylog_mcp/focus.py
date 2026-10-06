"""Focus on the current repository's service: tools search that service unless asked for more.

Inside a repository, "errors in the last hour" means this service's errors, not every stream's. The
service comes from ``[focus]`` in the config or is guessed from the repository name by matching it
against the values of the service fields (``application``, ``service``...). Other streams and services
are searched only when asked: ``streams=["*"]``, an explicit stream, or a query that names a service field.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence

# words that often differ between a repository and the service name it logs under
_AFFIXES = ("service", "svc", "server", "api", "app", "backend", "be")
MIN_SCORE = 0.85


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _core(text: str) -> str:
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]
    kept = [w for w in words if w not in _AFFIXES] or words
    return "".join(kept)


def _score(repo: str, value: str) -> float:
    if _norm(repo) == _norm(value):
        return 1.0
    if _core(repo) and _core(repo) == _core(value):
        return 0.95
    return difflib.SequenceMatcher(None, _core(repo), _core(value)).ratio()


def guess_service(repo_names: Iterable[str], values: Sequence[str]) -> str | None:
    """The service value that clearly matches one of the repository names, else None."""
    scored = sorted(
        ((max(_score(r, v) for r in repo_names), v) for v in values if v and _norm(v)),
        key=lambda item: -item[0],
    )
    if not scored or scored[0][0] < MIN_SCORE:
        return None
    best, runner_up = scored[0][0], scored[1][0] if len(scored) > 1 else 0.0
    exact_wins = best == 1.0 > runner_up
    if runner_up >= best - 0.03 and not exact_wins:
        return None  # two candidates are as close: do not guess
    return scored[0][1]


def wants_everything(streams: Sequence[str] | None) -> bool:
    return bool(streams) and any(s.strip().lower() in ("*", "all") for s in streams or ())


def mentions_field(query: str | None, fields: Iterable[str]) -> bool:
    """Whether the Lucene query already filters on one of these fields (then the focus steps aside)."""
    return bool(query) and any(re.search(rf"(?<![\w.]){re.escape(name)}\s*:", query or "") for name in fields)
