"""MCP prompts: ready-made requests for a task, which a user picks in their chat client.

A client shows prompts its own way and only when the user picks one, so ``ask`` and the tools must work without
them; a prompt only saves the person typing the steps. The built-in ones are for people who are not developers:
they start from words and codes, never from a query. A team adds its own in ``[prompts]``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from graylog_mcp.config import TeamPrompt

Environment = Annotated[
    str, Field(description="Environment or group/environment, e.g. prod or payment/prod; empty: the default")
]


def _where(environment: str, when: str = "") -> str:
    said = []
    if environment.strip():
        said.append(f"Environment: {environment.strip()} (pass it as instance).")
    if when.strip():
        said.append(f"Period: {when.strip()}.")
    return (" " + " ".join(said)) if said else ""


_PLAIN = (
    "Answer in the language the user wrote in, in plain words for someone who is not a developer: no Lucene, no "
    "field names unless they ask. Quote values as the tools return them (masked values stay masked)."
)


def scan(
    target: Annotated[
        str, Field(description="What to scan: a service, a symptom, an environment, or empty for everything")
    ] = "",
    range: Annotated[str, Field(description="Window, e.g. 15m, 1h, 24h")] = "1h",
) -> str:
    focus = f" Focus on: {target}." if target.strip() else ""
    return (
        f"Scan the logs of the last {range} for problems.{focus}\n"
        "1. Call scan once (instance from describe if the request names an environment). If the request "
        "is about something specific, add ad hoc checks for it in the same call.\n"
        "2. For each finding, most severe first: one aggregate (kind='histogram' or 'errors') with the "
        "finding's query to confirm when it started and what it is.\n"
        "3. Answer with a table: severity, rule, count vs baseline, trend, first seen, sample ref; then the "
        "rules checked and quiet, and any skipped rule (not checked, not healthy). No speculation beyond the "
        "evidence."
    )


def find_error(
    description: Annotated[
        str, Field(description="The problem in your own words, e.g. 'thanh toán bị lỗi sáng nay', 'login fails'")
    ],
    environment: Environment = "",
    when: Annotated[str, Field(description="When, in words or as 15m/2h/1d; empty: as the description says")] = "",
) -> str:
    return (
        f'A colleague describes a problem: "{description}".{_where(environment, when)}\n'
        "1. Call ask with their words as they are, the period included; do not turn them into a query.\n"
        "2. Say in one sentence how ask understood it (its summary). If ask returns 'clarify', ask the user that "
        "question, with its 'options' or 'readings', and stop there.\n"
        "3. When ask found errors that fit ('matches'), show the best one: service, error, message, how many, "
        "first and last seen. If it carries a trace id, follow it with trace_request.\n"
        "4. Say what happened, since when, how often, and where to look next; give the query that ran ('ran') "
        "so a developer can repeat it.\n" + _PLAIN
    )


def trace_code(
    code: Annotated[str, Field(description="An order code, transaction number or trace id, e.g. ORD-1001")],
    environment: Environment = "",
    when: Annotated[str, Field(description="How far back to look, e.g. 24h, 7d")] = "24h",
) -> str:
    return (
        f"Find where {code} went.{_where(environment)}\n"
        f"1. Call trace_request with trace_id={code!r} and range={when!r}.\n"
        "2. When it lists several traces, take them in time order: when, which services, whether each failed. For "
        "the one that failed, show the step that broke: service, time, the error message and its ref.\n"
        "3. When nothing is found, say so and give its suggestions (a wider period, another environment).\n" + _PLAIN
    )


def check_after_change(
    change: Annotated[str, Field(description="What changed, e.g. 'release 2.4 of checkout', 'test run 17'")],
    since: Annotated[str, Field(description="When it happened: ISO time, 'YYYY-MM-DD HH:MM', or 30m/2h ago")] = "30m",
    environment: Environment = "",
) -> str:
    return (
        f"Check the logs after this change: {change}. It happened {since} (ago, when a length).{_where(environment)}\n"
        "1. Call compare_periods with split_at at the change, to see errors before and after.\n"
        "2. Call scan over the time since the change.\n"
        "3. Report what is new or grew since the change, with counts before and after and one sample each; say "
        "plainly when nothing changed. Separate what the change likely caused from what was already there.\n" + _PLAIN
    )


def ticket_evidence(
    problem: Annotated[str, Field(description="The bug in your words, or a code / trace id to follow")],
    environment: Environment = "",
    when: Annotated[str, Field(description="Period, e.g. 1h, today, 'từ 9h đến 11h'")] = "1h",
) -> str:
    return (
        f'Gather log evidence for a bug ticket about: "{problem}".{_where(environment, when)}\n'
        "1. Call ask with the problem and the period (trace_request instead when it is a code or a trace id).\n"
        "2. Write the ticket text in Markdown, facts from the tool results only:\n"
        "   - Summary: one sentence.\n"
        "   - Environment and period, with the timezone of the results.\n"
        "   - What the logs show: counts by kind of error, the main error with one sample message and its ref.\n"
        "   - Steps across services, when a trace exists: time, service, what happened.\n"
        "   - How to see it again: the query that ran, the environment and the period.\n" + _PLAIN
    )


# name -> (function, title, description)
BUILT_IN: dict[str, tuple[Callable[..., str], str, str]] = {
    "scan": (scan, "Scan logs", "Scan the logs for problems (all rules, or a target) and report the findings"),
    "find_error": (find_error, "Find an error", "Find the error matching a description in your own words"),
    "trace_code": (trace_code, "Follow a code", "Where an order, transaction or request went, step by step"),
    "check_after_change": (
        check_after_change,
        "Check after a change",
        "What is new or worse in the logs after a release, a deploy or a test run",
    ),
    "ticket_evidence": (ticket_evidence, "Evidence for a ticket", "Log evidence for a bug ticket, ready to paste"),
}


def _team(prompt: TeamPrompt) -> Callable[..., str]:
    defaults = {a.name: a.default for a in prompt.arguments if a.default is not None}

    def render(**given: Any) -> str:
        return prompt.text.format_map({**defaults, **{k: str(v) for k, v in given.items()}})

    return render


def register(server: MCPServer, team: dict[str, TeamPrompt]) -> None:
    from mcp.server.mcpserver.prompts import Prompt
    from mcp.server.mcpserver.prompts.base import PromptArgument

    for name, (fn, title, description) in BUILT_IN.items():
        server.add_prompt(Prompt.from_function(fn, name=name, title=title, description=description))
    for prompt in team.values():
        arguments = [
            PromptArgument(name=a.name, description=a.description or None, required=a.default is None)
            for a in prompt.arguments
        ]
        server.add_prompt(
            Prompt(
                name=prompt.name,
                title=prompt.title or None,
                description=prompt.description or None,
                arguments=arguments,
                fn=_team(prompt),
                context_kwarg=None,
            )
        )
