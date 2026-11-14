"""Remembers what the night's visitors wore, so the pumpkin can talk about them.

Each group's costumes, as the pumpkin described them in notice_visitors, go to
a file in the call-log volume, so a code reload mid-evening doesn't forget
them. Only the pumpkin's own words are kept, never images.
"""

import json
import threading
import time
from pathlib import Path

# "Tonight" for a pumpkin: anything from the last twelve hours.
TONIGHT_SECONDS = 12 * 3600


class CostumeLog:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()

    def add(self, costumes):
        costumes = (costumes or "").strip()
        if not costumes:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"at": self.clock(), "costumes": costumes}) + "\n")

    def tonight(self, leave_out="", limit=12):
        """Tonight's groups, newest first, without repeats or leave_out (the
        group in front of the pumpkin now)."""
        since = self.clock() - TONIGHT_SECONDS
        seen, found = {leave_out.strip().lower()}, []
        with self.lock:
            try:
                lines = self.path.read_text().splitlines()
            except FileNotFoundError:
                return []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            costumes = entry.get("costumes", "")
            key = costumes.strip().lower()
            if entry.get("at", 0) < since or not key or key in seen:
                continue
            seen.add(key)
            found.append(costumes)
            if len(found) == limit:
                break
        return found
