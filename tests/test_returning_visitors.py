#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Checks for returning_visitors.py. Run with: uv run tests/test_returning_visitors.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import returning_visitors as rv  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


GRAY = [0.5, 0.5, 0.5, 0.2, 0.25, 0.4]  # gray shirt, jeans
RED = [0.8, 0.1, 0.1, 0.1, 0.1, 0.1]
GREEN = [0.1, 0.7, 0.2, 0.3, 0.3, 0.3]


def person(look, height=0.6, whole=True):
    return {"look": look, "height": height, "whole": whole, "feetY": 0.9}


now = [1000.0]
groups = rv.RecentGroups(clock=lambda: now[0])

check("a first visitor is new", groups.arrived([person(GRAY)]) is None)
groups.describe("a gray t-shirt")
now[0] += 30
back = groups.arrived([person([v + 0.02 for v in GRAY], height=0.62)])
check("the same look and height, back: recognized", back is not None and back["costumes"] == "a gray t-shirt")
check("the hint says so", "moments ago, wearing a gray t-shirt" in groups.hint(back))
now[0] += 90
check("and again, still with the costumes", groups.arrived([person(GRAY)])["costumes"] == "a gray t-shirt")

now[0] += 10
check("different clothes: new", groups.arrived([person(RED)]) is None)
now[0] += 10
check("same clothes, much shorter: new", groups.arrived([person(GRAY, height=0.3)]) is None)
now[0] += 10
check("a group with one returner of two counts", groups.arrived([person(RED), person(GREEN)]) is not None)
now[0] += 10
check("one returner among three new doesn't", groups.arrived([person(GRAY, 0.2), person(GREEN, 0.3), person([0.9] * 6)]) is None)
check("no signatures: can't tell, so new", groups.arrived([{"height": 0.6, "whole": True}]) is None)

# Which group got gentle mode, so only that one stays gentle when it comes back.
fresh = rv.RecentGroups(clock=lambda: now[0])
fresh.arrived([person(GRAY)])
fresh.arrived([person(RED, 0.3), person(RED)])
fresh.mark_gentle()
now[0] += 20
check("a returning gentle group is marked so", fresh.arrived([person(RED, 0.3), person(RED)])["gentle"])
check("a returning group that wasn't gentle isn't", fresh.arrived([person(GRAY)])["gentle"] is False)

now[0] += rv.REMEMBER_SECONDS + 1
check("forgotten after a while", groups.arrived([person(RED)]) is None)
check("the hint for new people", groups.hint(None) == rv.NOT_SEEN_BEFORE)
check("minutes", rv.ago(61) == "a minute ago" and rv.ago(300) == "5 minutes ago")

sys.exit(fails)
