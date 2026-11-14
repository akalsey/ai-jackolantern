#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""A Markdown report of the QC monitor's findings, for improving the agent.

Fetches today's findings from /eyes-qc, groups the problems by kind, and puts
each one next to what the visitor said, what the reviewer expected, and what
the pumpkin said. Paste it into a prompt-tuning session along with the call
transcripts from /eyes/calls.html.

    ./qc_report.py https://pumpkin.example.com > qc-report.md

Set EYES_PASSWORD if the pages are password protected.
"""

import os
import sys
from collections import Counter, defaultdict

import requests

PROBLEMS = {"deviation", "unanswered", "off_personality", "broke_character", "too_long",
            "unexpected", "wrong_tool"}


def report(entries):
    verdicts = Counter(e["verdict"] for e in entries if e.get("verdict"))
    tools = Counter(e["tool"] for e in entries if e["kind"] == "tool_used")
    predicted = {e.get("visitor_said"): e.get("expected") for e in entries if e["kind"] == "predict_answer"}
    problems = defaultdict(list)
    for e in entries:
        if e.get("verdict") in PROBLEMS:
            problems[e["verdict"]].append(e)
        elif e["kind"] in ("sidecar_error", "sidecar_insight"):
            problems[e["kind"]].append(e)

    reviewed = sum(n for v, n in verdicts.items())
    good = reviewed - sum(verdicts[v] for v in PROBLEMS)
    out = ["# Pumpkin QC report", "",
           f"{reviewed} reviews, {good} as expected or acceptable.", "", "## Verdicts", ""]
    out += [f"- {v.replace('_', ' ')}: {n}" for v, n in verdicts.most_common()]
    out += ["", "## Tools used", ""] + [f"- {t}: {n}" for t, n in tools.most_common()]
    for kind, items in sorted(problems.items(), key=lambda kv: -len(kv[1])):
        out += ["", f"## {kind.replace('_', ' ').capitalize()} ({len(items)})", ""]
        for e in items:
            if e["kind"] == "review_answer":
                out += [f"- **Visitor:** {e.get('visitor_said', '–')}",
                        f"  **Expected:** {predicted.get(e.get('visitor_said'), '–')}",
                        f"  **Pumpkin:** {e.get('actual', '–')}",
                        f"  **Note:** {e.get('notes', '–')}"]
            elif e["kind"] == "review_tool":
                out.append(f"- `{e.get('tool')}`: {e.get('notes', '–')}")
            else:
                out.append(f"- {e.get('raw') or e.get('detail') or e.get('error_reason')}")
    return "\n".join(out) + "\n"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    password = os.environ.get("EYES_PASSWORD")
    response = requests.get(sys.argv[1].rstrip("/") + "/eyes-qc",
                            auth=("pumpkin", password) if password else None, timeout=30)
    response.raise_for_status()
    print(report(response.json()), end="")


if __name__ == "__main__":
    main()
