"""A stand-in for a local command used as a provider of ask: run as ``python fake_command.py RECORD [MODE]``.

Appends what it read on stdin to the file RECORD (one JSON document a line), then answers every question: the
first choice offered, a probability of 0.9, the word "refund". MODE makes it misbehave: "hang", "fail", "flood"
or "prose" (the answer inside a sentence).
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    record, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    if mode == "hang":
        with open(f"{record}.pid", "w", encoding="utf-8") as fh:  # so a test can see whether it was stopped
            fh.write(str(os.getpid()))
        time.sleep(30)
    asked = json.loads(sys.stdin.read())
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asked) + "\n")
    if mode == "fail":
        print("something secret went wrong", file=sys.stderr)
        return 3
    if mode == "flood":
        sys.stdout.write("x" * (2 * 1024 * 1024))
        return 0
    answers = {}
    for name, question in asked["questions"].items():
        if question["type"] == "choice":
            answers[name] = {"choice": next(iter(question["criteria"])), "confidence": 0.9}
        elif question["type"] == "noul":
            answers[name] = {"noul": 0.9}
        else:
            answers[name] = {"words": ["refund"]}
    reply = json.dumps({"answers": answers})
    print(f"Here you go: {reply} Anything else?" if mode == "prose" else reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
