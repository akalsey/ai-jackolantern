#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""The server's own visit state, and the replay of a recorded night."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))
import replay  # noqa: E402
import visit_state as vs  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


clock = {"now": 1000.0}


def fresh():
    return vs.VisitState(clock=lambda: clock["now"])


def at(t):
    clock["now"] = t


def kinds(s, kind):
    return [e for e in s.events if e["kind"] == kind]


# A group arrives: the next tool call carries the switch into the act.
at(1000)
s = fresh()
s.visit_started()
check("a new group waits in the yard for its age check", s.desired == vs.YARD)
s.age_check(True)
check("...and passing it means the act", s.desired == vs.ACT and s.actual == vs.YARD)
fix = s.correction("notice_visitors")
check("the next tool call corrects the pumpkin", fix == {"mode": vs.ACT, "step": "greet", "frm": vs.YARD}
      and s.actual == vs.ACT)
check("...and the one after has nothing to fix", s.correction("consult_memory") is None)

# A group re-reported as new while gentle stays gentle until it passes the check.
g = fresh()
g.visit_started(child=True)
g.observed(vs.GENTLE)
g.visit_ended(), g.visit_started()
check("an unchecked group after a gentle one stays gentle", g.desired == vs.GENTLE)
g.age_check(True)
check("...and a passed check brings the act", g.desired == vs.ACT)
g.visit_ended(), g.visit_started(child=True), g.observed(vs.GENTLE)
g.visit_ended(), g.visit_started()
g.age_check(False)
g.age_check(True)
check("a group that failed its own check stays gentle even if a later look passes", g.desired == vs.GENTLE)

# The ratchet: the model may go gentle; only the operator brings the act back.
check("become_gentle during a visit is granted", s.model_requested(vs.GENTLE, "become_gentle") == vs.GENTLE)
check("resume_act without the operator is refused", s.model_requested(vs.ACT, "resume_act") == vs.GENTLE)
s.operator_choice("normal")
check("the operator's Normal wants the act", s.desired == vs.ACT)
check("...and resume_act is granted", s.model_requested(vs.ACT, "resume_act") == vs.ACT)
check("after Normal, become_gentle is refused", s.model_requested(vs.GENTLE, "become_gentle") == vs.ACT)
s.age_check(False)
check("...and so is a failed age check", s.desired == vs.ACT)

# Leaving is the stage's call, not the model's.
check("a leaving tool while they're still here is refused", s.model_requested(vs.YARD, "visitors_left") == vs.ACT)
s.visit_ended()
check("the stage's 'left' wants the yard and forgets the operator", s.desired == vs.YARD and s.operator is None)
check("...and the leaving tool is granted", s.model_requested(vs.YARD, "visitors_left") == vs.YARD)

# A small child measured on arrival, or joining, means gentle.
s = fresh()
s.visit_started(child=True)
check("a small child measured on arrival means gentle", s.desired == vs.GENTLE)
s = fresh()
s.visit_started()
s.age_check(True)
s.correction("notice_visitors")
s.child_joined()
check("a small child joining means gentle", s.desired == vs.GENTLE)
check("become_gentle with nobody here doesn't start gentle mode",
      fresh().model_requested(vs.GENTLE, "become_gentle") == vs.YARD)

# Stuck: the pumpkin's mode disagrees and no tool call comes to fix it.
at(2000)
s = fresh()
told = []
s.on_event = lambda kind, why: told.append(kind)
s.visit_started()
s.age_check(True)
at(2007)
s.tick()
check("a short mismatch waits for a tool call", not told)
at(2009)
s.tick()
at(2025)
s.tick()
check("past 8 seconds the server asks for one, once", told == ["nudge"])
at(2031)
s.tick()
check("a mismatch past 30 seconds restarts the call", len(kinds(s, "restart")) == 1 and told == ["nudge", "restart"])
s.new_call()  # the page redialed; still no tool call
for t in (2070, 2072, 2074):
    at(t)
    s.tick()
check("another within 5 minutes alarms once instead",
      len(kinds(s, "restart")) == 1 and len(kinds(s, "alarm")) == 1 and told[-1] == "alarm")
at(2400)
s.tick()
check("after 5 minutes it restarts again", len(kinds(s, "restart")) == 2)

# The shoo point moves the act to its shoo step on the next tool call.
s = fresh()
s.visit_started()
s.age_check(True)
s.correction("notice_visitors")
s.observed(step="banter")
s.shoo_point()
check("past the shoo point, the next tool call moves to shoo",
      s.correction("consult_memory") == {"mode": vs.ACT, "step": "shoo", "frm": vs.ACT})
check("...once", s.correction("consult_memory") is None)

# The operator's choice made just before a group arrives applies to them.
at(2500)
s = fresh()
s.operator_choice("gentle")
at(2510)
s.visit_started()
check("Gentle pressed just before an arrival applies to it", s.desired == vs.GENTLE)
s.visit_ended()
s.operator_choice("normal")
at(2540)
s.visit_started()
check("...but not to a group arriving long after", s.desired == vs.YARD and s.operator is None)

# Repeating itself.
at(3000)
s = fresh()
line = "Ah, visitors. My fiery eyes are clouded by smoke and shadows tonight."
for i, text in enumerate([line, line.replace("tonight", "again"), line + " Truly.", line]):
    at(3000 + i * 10)
    s.assistant_line(text)
check("three near-repeats within two minutes restart the call",
      [e["why"] for e in kinds(s, "restart")] == ["repeating itself"])
s = fresh()
for i, text in enumerate(["You again? How tiresome.", "A witch. How original.",
                          "The staff have the candy.", "Off you go, then."]):
    s.assistant_line(text)
check("different lines don't", not kinds(s, "restart"))

# Alarms.
at(4000)
s = fresh()
for i in range(10):
    at(4000 + i * 30)
    s.empty_look()
check("ten empty looks in ten minutes alarm once", len(kinds(s, "alarm")) == 1)
at(4000)
s = fresh()
s.visit_started()
s.age_check(True)
s.correction("notice_visitors")
at(4000 + 601)
s.tick()
s.tick()
check("a visit over ten minutes alarms once, without a restart",
      len(kinds(s, "alarm")) == 1 and not kinds(s, "restart"))


# The replay, on a tiny made-up night.
def ev(t, event, **data):
    return {"t": t, "event": event, "data": data}


ARRIVED = "New visitors have just stepped up in front of you. Acknowledge them"
LEFT = "The people you were talking to have walked away. Call visitors_left."
night = [
    ev(0, "session_start"),
    # A passerby: on stage for less than 3 seconds.
    ev(10, "conversation_add", role="system", content=ARRIVED),
    ev(19, "conversation_add", role="system", content=LEFT),
    # A real group that drifts out of frame and back for a moment.
    ev(100, "conversation_add", role="system", content=ARRIVED),
    ev(101, "function_call", function="notice_visitors"),
    ev(102, "step_change", to_step="greet"),
    ev(105, "conversation_add", role="assistant", content="A witch. How original.", tokens=10),
    ev(130, "conversation_add", role="system", content=LEFT),  # reported 7 s after they went
    ev(130.5, "conversation_add", role="system", content=ARRIVED),  # 7.5 s after the stage emptied
    ev(160, "conversation_add", role="system", content=LEFT),
    ev(161, "function_call", function="visitors_left"),
    ev(162, "step_change", to_step="farewell"),
    ev(200, "step_change", to_step="idle"),
]
r = replay.replay(night)
check("replay: a passerby gets a hello, not a visit", r["hellos_new"] == 1)
check("replay: drifting out and back is one visit", r["visits_new"] == 1)
check("replay: the night replays without restarts", not r["restarts"])
check("replay: the report renders", "visits" in replay.report(r))

sys.exit(fails)
