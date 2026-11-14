"""Learns when a group in front of the pumpkin includes a small child.

Two measurements from the stage camera, both feet-to-head and only for people
whose whole body is in frame:

- ratio: the shortest person's height over the tallest's. It works for a group
  without knowing anything else, so it's the starting rule.
- height: the shortest person's height, as a fraction of the frame. Everyone on
  stage stands about the same distance from the camera, so once the small-child
  height is learned it applies anywhere on the stage, and to a lone visitor.

The height cutoff is learned from examples. Groups with a large height
difference supply them automatically (the shortest of a very uneven group is a
small child and its tallest isn't; a group of near-equals has none), and every
operator press on the remote's Gentle / Normal switch adds weightier ones. A
missed child counts as worse than a needlessly gentle show. Only numbers are
kept, never images.
"""

import json
import threading
import time
from pathlib import Path

# The ratio rule, until the height cutoff is learned.
DEFAULT_RATIO_CUTOFF = 0.6
# Automatic examples come from groups this uneven, or this even.
AUTO_CHILD_RATIO = 0.5
AUTO_PEERS_RATIO = 0.9
MIN_EXAMPLES = 6  # height examples, of both kinds, before the height cutoff is used
MISSED_CHILD_COST = 3  # a missed child counts this many times a needless gentle
OPERATOR_WEIGHT = 3  # an operator press counts this many automatic examples


def measure_group(people):
    """people: [{height, feetY, whole}] from the stage. Returns ratio, shortest,
    and tallest, from whole-body people only; None where they can't be had."""
    whole = [p["height"] for p in people or [] if p.get("whole") and p.get("height", 0) > 0]
    if not whole:
        return {"ratio": None, "shortest": None, "tallest": None, "people": len(people or [])}
    return {
        "ratio": round(min(whole) / max(whole), 3) if len(whole) >= 2 else None,
        "shortest": min(whole),
        "tallest": max(whole),
        "people": len(people),
    }


def fit_cutoff(examples, default=None):
    """The value below which something counts as a small child.

    examples: [(value, child, weight)]. Picks the cutoff with the lowest weighted
    error, a missed child costing MISSED_CHILD_COST times more; ties go to the
    higher (safer) cutoff. Returns default without examples of both kinds.
    """
    if not any(c for _, c, _ in examples) or all(c for _, c, _ in examples):
        return default
    values = sorted({v for v, _, _ in examples})
    candidates = [values[0] - 0.01] + [(a + b) / 2 for a, b in zip(values, values[1:])] + [values[-1] + 0.01]

    def cost(cutoff):
        return sum(
            w * (MISSED_CHILD_COST if child else 1)
            for v, child, w in examples
            if (child and v >= cutoff) or (not child and v < cutoff)
        )

    best = min(cost(c) for c in candidates)
    return round(max(c for c in candidates if cost(c) == best), 3)


class ChildModel:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._examples = {"ratio": [], "height": []}  # feature -> [(value, child, weight)]
        self._presses = set()  # timestamps of operator presses, to count them
        self._load()

    # ------------------------------------------------------------ examples

    def _load(self):
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            lines = []  # nothing learned yet
        for line in lines:
            try:
                self._add(json.loads(line))
            except (ValueError, KeyError, TypeError):
                continue
        self._refit()

    def _add(self, row):
        self._examples[row["feature"]].append((row["value"], bool(row["child"]), row["weight"]))
        if row.get("source") == "operator":
            self._presses.add(row["at"])

    def _save(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def _record(self, rows):
        rows = [r for r in rows if r["value"] is not None]
        with self._lock:
            if rows:
                self._save(rows)
            for r in rows:
                self._add(r)
            self._refit()

    def _refit(self):
        self.ratio_cutoff = fit_cutoff(self._examples["ratio"], DEFAULT_RATIO_CUTOFF)
        height = self._examples["height"]
        enough = len(height) >= MIN_EXAMPLES
        self.height_cutoff = fit_cutoff(height) if enough else None

    def record_operator(self, measured, child, **context):
        """An operator press: the group in front has (child) or hasn't a small child."""
        at = time.time()
        base = {"at": at, "child": child, "weight": OPERATOR_WEIGHT, "source": "operator", **context}
        self._record([
            {**base, "feature": "ratio", "value": measured.get("ratio")},
            # Has a small child: the shortest is one. Hasn't: nobody is, so the
            # shortest isn't either.
            {**base, "feature": "height", "value": measured.get("shortest")},
        ])

    def record_arrival(self, measured):
        """Automatic examples from a group with a large height difference, or none."""
        ratio = measured.get("ratio")
        if ratio is None:
            return
        base = {"at": time.time(), "weight": 1, "source": "auto", "feature": "height"}
        if ratio <= AUTO_CHILD_RATIO:
            self._record([
                {**base, "value": measured["shortest"], "child": True},
                {**base, "value": measured["tallest"], "child": False},
            ])
        elif ratio >= AUTO_PEERS_RATIO:
            self._record([{**base, "value": measured["shortest"], "child": False}])

    # ------------------------------------------------------------ judging

    def judge(self, measured):
        """(likely_child, rule): True/False/None, and "height", "ratio", or None."""
        shortest, ratio = measured.get("shortest"), measured.get("ratio")
        if self.height_cutoff is not None and shortest is not None:
            return shortest < self.height_cutoff, "height"
        if ratio is not None:
            return ratio < self.ratio_cutoff, "ratio"
        return None, None

    def summary(self):
        with self._lock:
            height = self._examples["height"]
            return {
                "operator_choices": len(self._presses),
                "height_examples": len(height),
                "height_child_examples": sum(1 for _, c, _ in height if c),
                "height_cutoff": self.height_cutoff,
                "ratio_cutoff": self.ratio_cutoff,
            }
