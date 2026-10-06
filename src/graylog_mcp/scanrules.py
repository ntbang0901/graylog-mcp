"""Built-in scan rules (see ``scan.py``).

Each rule is the same table a user writes under ``[scan.rules.<name>]``; a user table with the same name
overrides only the keys it sets, and ``[scan] disable = [...]`` turns rules off. Queries are full-text
phrases on purpose: they work whatever the field names are, and a phrase is cheap for the search backend
(no leading wildcard, no regex).
"""

from __future__ import annotations

from typing import Any

BUILTIN_SCAN_RULES: dict[str, dict[str, Any]] = {
    "crash": {
        "description": "Process crashes and unhandled errors (panic, segfault, uncaught exception)",
        "query": '"panic:" OR "Segmentation fault" OR "core dumped" OR "fatal error:" OR "Fatal Python error" '
        'OR "Unhandled exception" OR "uncaughtException" OR "unhandledRejection"',
        "severity": "critical",
        "threshold": 0,
        "tags": ["errors", "runtime"],
    },
    "resource_exhaustion": {
        "description": "Memory, disk, file descriptor or pool exhaustion",
        "query": 'OutOfMemoryError OR "out of memory" OR OOMKilled OR StackOverflowError '
        'OR "No space left on device" OR "Too many open files" OR "pool exhausted" OR "disk full"',
        "severity": "critical",
        "threshold": 0,
        "tags": ["errors", "infra"],
    },
    "error_spike": {
        "description": "The error rate (error_query) grew against the baseline",
        "errors_only": True,
        "severity": "high",
        "growth": 2.0,
        "min_count": 10,
        "group_by": "exception",
        "tags": ["errors"],
    },
    "new_error_types": {
        "description": "Error groups (by exception) never seen in the 24 hours before the window",
        "errors_only": True,
        "severity": "high",
        "group_by": "exception",
        "new_groups": True,
        "min_count": 1,
        "baseline": "24h",
        "tags": ["errors"],
    },
    "http_5xx": {
        "description": "Server errors (HTTP 5xx) grew against the baseline",
        "query": "http_status:[500 TO 599] OR status_code:[500 TO 599] OR response_status:[500 TO 599]",
        "requires": ["http_status", "status_code", "response_status"],
        "severity": "high",
        "growth": 2.0,
        "min_count": 10,
        "tags": ["errors", "http"],
    },
    "connectivity": {
        "description": "Timeouts, refused or reset connections, DNS failures, open circuit breakers",
        "query": 'timeout OR "timed out" OR "Connection refused" OR "Connection reset" OR ECONNREFUSED '
        'OR ECONNRESET OR ETIMEDOUT OR UnknownHostException OR "Name or service not known" OR "Broken pipe" '
        'OR "circuit breaker"',
        "severity": "high",
        "growth": 3.0,
        "min_count": 10,
        "tags": ["errors", "dependencies"],
    },
    "database": {
        "description": "Deadlocks, lock timeouts, serialization failures, too many connections",
        "query": 'deadlock OR "lock wait timeout" OR "could not serialize" OR "too many connections" '
        'OR "Too many connections" OR SQLException OR PSQLException OR "duplicate key"',
        "severity": "high",
        "growth": 3.0,
        "min_count": 5,
        "tags": ["errors", "dependencies"],
    },
    "auth_failures": {
        "description": "Authentication and authorization failures grew (expired tokens, brute force, bad config)",
        "query": '"authentication failed" OR "Unauthorized" OR "invalid token" OR "token expired" '
        'OR "invalid credentials" OR "login failed" OR "access denied" OR "Forbidden"',
        "severity": "medium",
        "growth": 3.0,
        "min_count": 20,
        "tags": ["security"],
    },
}
