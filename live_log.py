"""A live record of what's said during a call, from the platform's debug events.

The end-of-call report has the whole transcript, but the pumpkin's call lasts
all night. This keeps a compact line per event as it happens (what visitors
said, what the pumpkin said, which functions it called, step changes) so the
night can be read while it's still going, at /eyes-live.

Large events, like the full request sent to the language model, are left out.
"""

import json
import threading
import time
from pathlib import Path

# Keys that describe the call rather than an event, and level 2's raw model
# traffic, which is all prompt and no dialogue.
NOT_EVENTS = {"call_info", "call_id", "timestamp", "event_type", "project_id", "space_id", "node_id",
              "llm_request", "llm_response"}
MAX_EVENT_BYTES = 4000  # bigger than this is a raw payload, not a line of dialogue
MAX_TEXT = 600


def compact(value):
    """Short strings and numbers from an event, nested one level, truncated."""
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if not isinstance(v, (dict, list))}
    if isinstance(value, list):
        return [compact(v) for v in value[:10] if not isinstance(v, (dict, list))]
    return str(value)[:MAX_TEXT]


class LiveLog:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.clock = clock
        self.lock = threading.Lock()

    def path(self, day=None):
        """Today's file, or a given day's ("YYYY-MM-DD"). Days roll over at the
        server's midnight (UTC in the container), which falls in the evening in
        the Americas, so a night's show spans two files."""
        return self.directory / f"live-{day or time.strftime('%Y-%m-%d', time.localtime(self.clock()))}.jsonl"

    def record(self, event_type, body):
        call_id = (body.get("call_info") or {}).get("call_id") or body.get("call_id")
        lines = []
        for key, value in body.items():
            if key in NOT_EVENTS or len(json.dumps(value, default=str)) > MAX_EVENT_BYTES:
                continue
            lines.append({"t": round(self.clock(), 1), "call": call_id, "type": event_type,
                          "event": key, "data": compact(value)})
        if not lines:
            return
        with self.lock:
            path = self.path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.writelines(json.dumps(line) + "\n" for line in lines)

    def read(self, after=0, day=None):
        """Today's (or `day`'s) lines after line number `after`, and the new line
        count. A cursor past the end means the day rolled over, so it starts again."""
        with self.lock:
            try:
                lines = self.path(day).read_text().splitlines()
            except FileNotFoundError:
                return [], 0
        if after > len(lines):
            after = 0
        events = []
        for line in lines[after:]:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # a line torn by a restart mid-write
        return events, len(lines)
