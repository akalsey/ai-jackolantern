#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Checks for costume_log.py. Run with: uv run tests/test_costume_log.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import costume_log as cl  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


with tempfile.TemporaryDirectory() as d:
    now = [1000.0]
    log = cl.CostumeLog(Path(d) / "costumes.jsonl", clock=lambda: now[0])
    check("nothing yet", log.tonight() == [])
    for c in ("a witch", "two princesses", "", "a ninja", "Two Princesses "):
        log.add(c)
        now[0] += 60
    check("newest first, blanks skipped, repeats once", log.tonight() == ["Two Princesses", "a ninja", "a witch"])
    check("leaves out the group in front", log.tonight(leave_out="a ninja") == ["Two Princesses", "a witch"])
    check("a limit", len(log.tonight(limit=2)) == 2)
    now[0] += cl.TONIGHT_SECONDS - 100
    check("last night's costumes drop off", log.tonight() == ["Two Princesses"])

sys.exit(fails)
