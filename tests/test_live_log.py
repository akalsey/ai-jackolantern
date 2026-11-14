#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["signalwire-sdk>=3.7"]
# ///
"""Checks for live_log.py, and that level-2 debug events leave pacing alone.
Run with: uv run tests/test_live_log.py"""

import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import live_log as ll  # noqa: E402
import visit_pacing as vp  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


call = {"call_info": {"call_id": "c1"}}
with tempfile.TemporaryDirectory() as d:
    log = ll.LiveLog(d)
    log.record("speech_detect", {**call, "speech_detect": {"text": "Is that a real fire?"}})
    log.record("llm_request", {**call, "llm_request": {"tokens": 5}})
    log.record("conversation_add", {**call, "conversation_add": {"role": "assistant", "content": "It is my crown."}})
    log.record("other", {**call, "huge": {"text": "x" * 9000}})
    events, count = log.read()
    check("dialogue kept, model traffic and huge payloads left out",
          [e["event"] for e in events] == ["speech_detect", "conversation_add"] and count == 2)
    check("reading after the end gives nothing new", log.read(2) == ([], 2))
    check("a cursor past the end (a new day) starts over", len(log.read(99)[0]) == 2)
    today = time.strftime("%Y-%m-%d")
    check("a day can be read by name", len(log.read(0, today)[0]) == 2 and log.read(0, "1999-01-01") == ([], 0))

sent = []
pacer = vp.VisitPacer(lambda call_id, data: sent.append(data))
for body in ({"llm_request": {}}, {"llm_response": {}}, {"conversation_add": {"role": "user", "content": "hi"}}):
    pacer.handle({**call, **body})
pacer._outbox.join()
check("level-2 events don't move pacing", sent == [])

sys.exit(fails)
