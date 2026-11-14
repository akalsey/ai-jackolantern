#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Remove what the pumpkin has learned, all of it or from a stretch of time.

Two files in the call-log volume hold what it learns:

- child-labels.jsonl: the small-child learner's height examples, from groups
  arriving and from the operator's Gentle / Normal presses (child_model.py).
- costumes.jsonl: the costumes it has seen, for "what's your favorite
  costume?" (costume_log.py).

Test calls write to both, and a test with pictures of a toddler next to adults
teaches the learner things that aren't true of your yard. Remove a test's
entries afterwards with --since and --until, or start over with --all. Each
file is copied to a .bak file first. Restart the agent afterwards: it reads the
learner's examples when it starts.

    ./reset_training.py --since 2026-11-01 --dry-run    # see what would go
    ./reset_training.py --since 2026-11-01              # entries from then on
    ./reset_training.py --since "2026-11-02 15:30" --until "2026-11-02 16:30"
    ./reset_training.py --all --only learner            # forget every example

Times are in the clock of wherever this runs (UTC inside the Docker container).
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

FILES = {"learner": "child-labels.jsonl", "costumes": "costumes.jsonl"}


def parse_when(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"{text!r}: use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'")


def split(lines, since, until):
    """(kept, removed) lines: removed are those recorded in [since, until).
    A line that can't be read is kept, so nothing unknown is thrown away."""
    kept, removed = [], []
    for line in lines:
        try:
            at = json.loads(line)["at"]
        except (ValueError, KeyError, TypeError):
            kept.append(line)
            continue
        (removed if since <= at < until else kept).append(line)
    return kept, removed


def reset(path, since, until, dry_run=False):
    """Remove the entries recorded in [since, until) from one file. Returns
    (removed, kept) counts."""
    try:
        lines = [l for l in path.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return 0, 0
    kept, removed = split(lines, since, until)
    if removed and not dry_run:
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
        # Written beside it and moved into place, so a running agent never
        # reads a half-written file.
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
            f.writelines(line + "\n" for line in kept)
        os.replace(f.name, path)
    return len(removed), len(kept)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    window = p.add_mutually_exclusive_group(required=True)
    window.add_argument("--all", action="store_true", help="remove every entry")
    window.add_argument("--since", type=parse_when, help="remove entries from this time on")
    p.add_argument("--until", type=parse_when, help="...and before this time (default: now)")
    p.add_argument("--only", choices=list(FILES), help="just the learner or just the costumes")
    p.add_argument("--dir", default=os.environ.get("CALL_LOG_DIR", Path(tempfile.gettempdir()) / "pumpkin-calls"),
                   help="the call-log directory (default: $CALL_LOG_DIR)")
    p.add_argument("--dry-run", action="store_true", help="say what would be removed, change nothing")
    a = p.parse_args()
    if a.all and a.until:
        p.error("--until goes with --since, not --all")
    since = 0 if a.all else a.since
    until = a.until or float("inf")
    verb = "would remove" if a.dry_run else "removed"
    for name, filename in FILES.items():
        if a.only and a.only != name:
            continue
        removed, kept = reset(Path(a.dir) / filename, since, until, a.dry_run)
        print(f"{filename}: {verb} {removed}, kept {kept}")
    if not a.dry_run:
        print("Restart the agent so the learner forgets them too (docker compose restart).")


if __name__ == "__main__":
    sys.exit(main())
