#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Checks for child_model.py. Run with: uv run tests/test_child_model.py"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import child_model as cm  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


def person(height, whole=True):
    return {"height": height, "feetY": 0.9, "whole": whole}


# Measuring a group.
m = cm.measure_group([person(0.6), person(0.3), person(0.9, whole=False)])
check("only whole bodies are measured", m["shortest"] == 0.3 and m["tallest"] == 0.6 and m["ratio"] == 0.5)
check("a lone visitor has a height but no ratio", cm.measure_group([person(0.5)]) == {"ratio": None, "shortest": 0.5, "tallest": 0.5, "people": 1})
check("nobody whole: nothing measured", cm.measure_group([person(0.5, whole=False)])["shortest"] is None)

# Fitting a cutoff.
check("no examples of both kinds: the default", cm.fit_cutoff([(0.3, True, 1)], 0.6) == 0.6)
c = cm.fit_cutoff([(0.25, True, 1), (0.3, True, 1), (0.5, False, 1), (0.55, False, 1)])
check(f"separable heights: cutoff {c} between 0.3 and 0.5", 0.3 < c < 0.5)
# A child example at 0.52 among adults at 0.5-0.55: missing it costs 3, so the
# cutoff rises above it even at the price of a needless gentle or two.
c = cm.fit_cutoff([(0.25, True, 1), (0.52, True, 1), (0.5, False, 1), (0.55, False, 1)])
check(f"overlap: cutoff {c} rises above the 0.52 child", c > 0.52)
# Weight: one operator example (3) outvotes two automatic ones the other way.
c = cm.fit_cutoff([(0.25, True, 1), (0.6, False, 1), (0.45, False, 1), (0.45, False, 1), (0.42, True, 3)])
check(f"an operator example outweighs automatic ones: cutoff {c} above 0.42", c > 0.42)

with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "labels.jsonl"
    model = cm.ChildModel(path)
    check("starts on the ratio rule", model.judge(cm.measure_group([person(0.6), person(0.3)])) == (True, "ratio"))
    check("a lone visitor can't be judged yet", model.judge(cm.measure_group([person(0.3)])) == (None, None))

    # Uneven groups (an adult and a small child) and even groups (peers)
    # teach the height automatically.
    for _ in range(2):
        model.record_arrival(cm.measure_group([person(0.62), person(0.28)]))
    model.record_arrival(cm.measure_group([person(0.58), person(0.55)]))
    model.record_arrival(cm.measure_group([person(0.6), person(0.57)]))
    check("in-between groups add nothing", (model.record_arrival(cm.measure_group([person(0.6), person(0.45)])), model.summary()["height_examples"])[1] == 6)
    s = model.summary()
    check(f"six automatic examples learn the height: cutoff {s['height_cutoff']}", s["height_cutoff"] is not None and 0.28 < s["height_cutoff"] < 0.55)

    check("now a lone small visitor is judged by height", model.judge(cm.measure_group([person(0.3)])) == (True, "height"))
    check("and a lone adult", model.judge(cm.measure_group([person(0.6)])) == (False, "height"))
    check("an even group of short peers is judged by height, not ratio",
          model.judge(cm.measure_group([person(0.3), person(0.31)]))[1] == "height")

    model.record_operator(cm.measure_group([person(0.6), person(0.3)]), True, call_id="c")
    model.record_operator(cm.measure_group([]), False, call_id="c")  # nothing measured: nothing saved
    check("operator presses are counted when they measured something", model.summary()["operator_choices"] == 1)

    reloaded = cm.ChildModel(path)
    check("reloads the same cutoffs and counts", reloaded.summary() == model.summary())

sys.exit(fails)
