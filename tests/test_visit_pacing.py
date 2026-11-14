#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["signalwire-sdk>=3.7"]
# ///
"""Checks for visit_pacing.py. Run with: uv run tests/test_visit_pacing.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import visit_pacing as vp  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


sent = []
told = []
pacer = vp.VisitPacer(lambda call_id, data: sent.append(data), message=lambda call_id, text: told.append(text))


def event(**body):
    pacer.handle({"call_info": {"call_id": "c1"}, **body})
    pacer._outbox.join()  # wait for the sender thread to deliver


def exchange():
    event(speech_detect={"text": "hi"})
    event(ai_completion={"type": "normal"})


# The mood arc: questions allowed, then statements only, then shooing.
check("the first exchange allows a question", vp.mood_for(0, False) == vp.CALM)
check("then statements only", vp.mood_for(vp.QUESTIONS_UNTIL, False) == vp.STATEMENTS == vp.mood_for(vp.SHOO_AFTER - 1, False))
check("then the shoo moods, the last repeating",
      [vp.mood_for(n, False) for n in range(vp.SHOO_AFTER, vp.SHOO_AFTER + 6)] == vp.ACT_MOODS + [vp.ACT_MOODS[-1]] * 2)
check("gentle mode never asks, and sends them off sooner",
      [vp.mood_for(n, True) for n in range(0, 6)] == [vp.GENTLE_CALM] * vp.GENTLE_SHOO_AFTER + vp.GENTLE_MOODS + [vp.GENTLE_MOODS[-1]]
      and vp.GENTLE_SHOO_AFTER < vp.SHOO_AFTER and "no questions" in vp.GENTLE_CALM)

# Counting exchanges in a visit the server started.
shoos = []
pacer.on_shoo = shoos.append
event(session_start={})
pacer.start_visit("c1")
pacer._outbox.join()
check("a visit starts calm", sent[-1] == {"visit_turns": 0, "visit_mood": vp.CALM})
n = len(sent)
event(ai_completion={})
event(ai_completion={})
check("the pumpkin talking alone isn't an exchange", len(sent) == n)
exchange()
check("exchange 1: statements only", sent[-1] == {"visit_turns": 1, "visit_mood": vp.STATEMENTS})
n = len(sent)
exchange()
check("an unchanged mood isn't re-sent", len(sent) == n)
exchange()
check("exchange 3: shooing starts", sent[-1] == {"visit_turns": 3, "visit_mood": vp.ACT_MOODS[0]})
check("...the pumpkin is told to start shooing right then, once", told == [vp.SHOO_NOW])
check("...and the server is told the shoo point came", shoos == ["c1"])
event(speech_detect={}, ai_completion={})
check("speech and reply in one event still count", sent[-1]["visit_turns"] == 4)
pacer.set_gentle("c1", True)
pacer._outbox.join()
check("gentle mid-visit keeps the count", sent[-1] == {"visit_turns": 4, "visit_mood": vp.GENTLE_MOODS[2]})
pacer.end_visit("c1")
pacer._outbox.join()
check("the end of the visit resets the mood", sent[-1] == {"visit_turns": 0, "visit_mood": vp.CALM}
      and pacer.mode("c1") == "idle")
event(speech_detect={"text": "hi"})
check("speech alone doesn't start a visit while the stage is reporting", pacer.mode("c1") == "idle")
pacer.start_on_speech = lambda: True
event(speech_detect={"text": "hi"})
check("...but does when the stage detector is down", pacer.mode("c1") == "normal")
pacer.start_on_speech = lambda: False
check("the live call is tracked", pacer.current_call == "c1")
event(session_end={})
check("session end forgets the call", pacer.current_call is None and pacer.mode("c1") == "idle")


def on(call_id, **body):
    pacer.handle({"call_info": {"call_id": call_id}, **body})


on("old", session_start={})
on("new", session_start={})
on("old", ai_completion={})
check("an old call still talking doesn't take the stage back", pacer.current_call == "new")
on("old", session_end={})
check("nor does its end clear the new one", pacer.current_call == "new")

# One failure in the outbox doesn't stop what comes after it.
delivered = []
flaky = vp.VisitPacer(lambda call_id, data: (_ for _ in ()).throw(OSError("disk full")))
flaky.tell = lambda *a: None
flaky._outbox.put(lambda: (_ for _ in ()).throw(OSError("disk full")))
flaky._outbox.put(lambda: delivered.append("next"))
flaky._outbox.join()
check("a failed delivery doesn't stop the next one", delivered == ["next"])

sys.exit(fails)
