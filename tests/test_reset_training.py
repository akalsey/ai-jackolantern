#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Checks for reset_training.py. Run with: uv run tests/test_reset_training.py"""

import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import reset_training as rt  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


def at(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


d = Path(tempfile.mkdtemp())
labels = d / "child-labels.jsonl"
costumes = d / "costumes.jsonl"
labels.write_text("\n".join(json.dumps(r) for r in [
    {"at": at("2026-10-31 18:00"), "feature": "height", "value": 0.3, "child": True, "weight": 1},
    {"at": at("2026-11-02 15:40"), "feature": "height", "value": 0.25, "child": True, "weight": 1},
    {"at": at("2026-11-02 15:50"), "feature": "ratio", "value": 0.4, "child": True, "weight": 3},
]) + "\nnot json\n")
costumes.write_text("\n".join(json.dumps(r) for r in [
    {"at": at("2026-10-31 19:00"), "costumes": "a dinosaur"},
    {"at": at("2026-11-02 15:45"), "costumes": "a witch and a vampire"},
]) + "\n")


def run(*args):
    return subprocess.run([sys.executable, str(ROOT / "reset_training.py"), "--dir", str(d), *args],
                          capture_output=True, text=True)


out = run("--since", "2026-11-02", "--dry-run").stdout
check("a dry run says what would go", "would remove 2, kept 2" in out and "would remove 1, kept 1" in out)
check("...and changes nothing", len(labels.read_text().splitlines()) == 4)

out = run("--since", "2026-11-02 15:30", "--until", "2026-11-02 15:45").stdout
check("a window removes only what was recorded in it", "child-labels.jsonl: removed 1, kept 3" in out
      and "costumes.jsonl: removed 0, kept 2" in out)
check("the file is backed up first", list(d.glob("child-labels.jsonl.bak-*")))
check("an unreadable line is kept, not thrown away", "not json" in labels.read_text())

out = run("--all", "--only", "costumes").stdout
check("--all with --only clears just that file", costumes.read_text() == "" and "child-labels" not in out)
check("a missing file is fine", rt.reset(d / "nothing.jsonl", 0, float("inf")) == (0, 0))
check("--until without --since is refused", run("--all", "--until", "2026-11-02").returncode != 0)

sys.exit(fails)
