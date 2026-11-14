#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Replay a recorded night through the server's new visit state.

Reads the live log (`/eyes-live` output, or the `live-*.jsonl` file from the
call-log volume) and asks: had the server owned the state (`visit_state.py`),
what would that night have looked like?

- The stage camera's recorded "arrived" and "left" messages are reshaped by
  the page's new rules: a visit needs 3 seconds of presence, and ends 8
  seconds after the stage empties, so someone drifting in and out stays one
  visit. Shorter appearances get only a hello.
- The model is replayed, not simulated: its recorded tool calls are what it
  tried to do, at the times it did them. Every tool request first applies the
  server's correction, as the new tool handlers will.
- Conversation size is counted from the recorded token counts, cut at every
  change of mode (each mode is an isolated context) and every restart.

The model's later choices would differ once a correction lands; this replays
what it did, so it measures the state logic, not the conversation.

    sim/replay.py night.json            # summary
    sim/replay.py night.json --events   # plus every correction, restart, and alarm
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import visit_state as vs  # noqa: E402

VISIT_AFTER = 3  # seconds of presence before a hello becomes a visit
LEAVE_HOLD = 8  # seconds of empty stage before a visit ends
HELLO_GAP = 60  # at most one hello a minute outside a visit
# How long after the stage emptied the old page reported "left": 3 s until a
# cast member counts as gone, 4 s until the show ends. A deploy at the time
# given by --hold-from added an 8-second hold on top.
OLD_LEFT_DELAY = 7
OLD_HOLD = 8
TICK = 2
# The start and end of a visit each send the pumpkin a message that asks for a
# tool (look and notice them; they've gone). On the night it usually answered
# within a few seconds, so if the recording has no tool call by then, one is
# assumed, and counted.
PROMPTED_TOOL_SECONDS = 4
NATIVE = {"get_visual_input"}
LEAVING = {"visitors_left", "visitors_left_gently", "visitors_leaving", "visitors_leaving_gently"}


def load(path):
    text = Path(path).read_text()
    try:
        data = json.loads(text)
        return data["events"] if isinstance(data, dict) else data
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def classify(e):
    """What an event means for the replay, or None."""
    name, data = e["event"], e.get("data") or {}
    if name == "conversation_add":
        role, text = data.get("role"), data.get("content") or ""
        if role == "assistant":
            return ("line", text)
        if role != "system":
            return None
        if text.startswith("The people you were talking to have walked away"):
            return ("left", None)
        if text.startswith("The people you were talking to are gone"):
            return ("swap", "small child among them" in text)
        if text.startswith("New visitors have just stepped up"):
            return ("arrived", "there's a small child" in text)
        if text.startswith("A small child has just stepped up to join"):
            return ("joined", None)
        if text.startswith("The operator says this group includes a small child"):
            return ("operator", "gentle")
        if text.startswith("The operator says this group has no small children"):
            return ("operator", "normal")
        return None
    if name == "function_call":
        return ("tool", (data.get("function"), bool(data.get("blocked"))))
    if name == "context_change":
        return ("context", data.get("to_context"))
    if name == "step_change":
        return ("step", data.get("to_step"))
    if name == "session_start":
        return ("session", None)
    return None


def presence(events, hold_from):
    """The stage's recorded shows as (start, end, child) presence intervals."""
    out, start, child = [], None, False
    for e in events:
        c = classify(e)
        if not c:
            continue
        kind, arg = c
        t = e["t"]
        if kind in ("left", "swap") and start is not None:
            delay = OLD_LEFT_DELAY + (OLD_HOLD if hold_from and t >= hold_from else 0)
            out.append((start, max(start, t - delay), child))
            start = None
        if kind in ("arrived", "swap"):
            start, child = t, bool(arg)
    if start is not None:
        out.append((start, events[-1]["t"], child))
    return out


def visits_from(intervals):
    """The new page's visits: merge gaps under LEAVE_HOLD, keep those of at
    least VISIT_AFTER. Returns (visits, hellos)."""
    merged = []
    for s, e, child in intervals:
        if merged and s - merged[-1][1] < LEAVE_HOLD:
            ps, pe, pc, joins = merged[-1]
            merged[-1] = (ps, max(pe, e), pc, joins + ([s] if child else []))
        else:
            merged.append((s, e, child, []))
    visits, hellos, last_hello = [], [], None
    for s, e, child, joins in merged:
        if e - s >= VISIT_AFTER:
            visits.append({"start": s + VISIT_AFTER, "end": e + LEAVE_HOLD, "child": child, "joins": joins})
        elif last_hello is None or s - last_hello >= HELLO_GAP:
            hellos.append(s)
            last_hello = s
    return visits, hellos


def old_mode(context, step):
    if context == "gentle":
        return vs.GENTLE
    return vs.YARD if step in (None, "idle") else vs.ACT


def replay(events, hold_from=None):
    events = sorted(events, key=lambda e: e["t"])
    visits, hellos = visits_from(presence(events, hold_from))
    clock = {"now": events[0]["t"]}
    state = vs.VisitState(clock=lambda: clock["now"])
    state.keep_events = None
    # A restart's re-announcement brings the tool call that sets the new call
    # straight; the replay lands it at once.
    state.on_event = lambda kind, why: kind == "restart" and state.call_restarted()

    # Everything that happens, merged on one timeline.
    timeline = [(e["t"], 1, "event", e) for e in events]
    for v in visits:
        timeline.append((v["start"], 0, "visit_start", v))
        timeline.append((v["end"], 0, "visit_end", v))
        timeline.append((v["start"] + PROMPTED_TOOL_SECONDS, 0, "prompted", "notice_visitors"))
        timeline.append((v["end"] + PROMPTED_TOOL_SECONDS, 0, "prompted", "visitors_left"))
        timeline += [(t, 0, "joined", None) for t in v["joins"]]
    t = events[0]["t"]
    while t < events[-1]["t"]:
        timeline.append((t, 2, "tick", None))
        t += TICK
    timeline.sort(key=lambda x: (x[0], x[1]))

    old = {"context": "default", "step": None}
    old_mismatch = new_mismatch = 0.0
    last_t = events[0]["t"]
    old_tokens = new_tokens = old_max = new_max = 0
    last_actual = state.actual
    pending_notice = None  # a passed notice_visitors, waiting to see where it went
    messages_old = 0
    last_tool = events[0]["t"]
    assumed = 0
    early_age = None  # an age check that came before its visit's start

    for t, _, kind, item in timeline:
        dt = t - last_t
        if old_mode(old["context"], old["step"]) != state.desired:
            old_mismatch += dt
        if state.actual != state.desired:
            new_mismatch += dt
        last_t = t
        clock["now"] = t
        if pending_notice and t - pending_notice > 3:
            state.age_check(True)
            state.model_requested(vs.ACT, "notice_visitors")
            pending_notice = None

        if kind == "visit_start":
            state.visit_started(child=item["child"])
            # The old page started shows sooner, so the age check on this group
            # can come before the new visit start; it belongs to this visit.
            if early_age and t - early_age[0] <= VISIT_AFTER + 1:
                state.age_check(*early_age[1:])
                state.correction("notice_visitors")
            early_age = None
        elif kind == "visit_end":
            state.visit_ended()
        elif kind == "joined":
            state.child_joined()
        elif kind == "tick":
            state.tick()
        elif kind == "prompted":
            if t - last_tool > PROMPTED_TOOL_SECONDS and state.correction(item + " (assumed)"):
                assumed += 1
        else:
            c = classify(item)
            if item["event"] == "conversation_add":
                tokens = (item.get("data") or {}).get("tokens") or 0
                old_tokens += tokens
                new_tokens += tokens
            if not c:
                pass
            elif c[0] == "line":
                state.assistant_line(c[1])
            elif c[0] == "operator":
                messages_old += 1
                state.operator_choice(c[1])
            elif c[0] in ("left", "arrived", "swap", "joined"):
                messages_old += 1
            elif c[0] == "context":
                old["context"] = c[1]
                old["step"] = "reassure" if c[1] == "gentle" else "idle"
                if c[1] == "gentle" and pending_notice:
                    if not state.visit:
                        early_age = (t, True, True)
                    state.age_check(True, small_child=True)
                    state.model_requested(vs.GENTLE, "notice_visitors")
                    pending_notice = None
            elif c[0] == "step":
                old["step"] = c[1]
            elif c[0] == "session":
                old.update(context="default", step=None)
                old_tokens = 0
                state.call_restarted()
            elif c[0] == "tool":
                name, blocked = c[1]
                if name not in NATIVE:
                    last_tool = t
                    state.correction(name)
                    if name == "notice_visitors":
                        if blocked:
                            if not state.visit:
                                early_age = (t, False)
                            state.age_check(False)
                            state.model_requested(vs.GENTLE, "age check failed")
                        else:
                            pending_notice = t
                    elif name == "become_gentle":
                        state.model_requested(vs.GENTLE, "become_gentle")
                    elif name in LEAVING and not blocked:
                        state.model_requested(vs.YARD, name)
                    elif name == "resume_act":
                        state.model_requested(vs.ACT, "resume_act")
                    elif name == "explain_blindness":
                        state.empty_look()
        if state.actual != last_actual or (state.events and state.events[-1]["kind"] == "restart"
                                           and state.events[-1]["t"] == t):
            new_tokens = 0  # isolated context, or a new call
        last_actual = state.actual
        old_max, new_max = max(old_max, old_tokens), max(new_max, new_tokens)

    arrivals = sum(1 for e in events if (classify(e) or ("",))[0] in ("arrived", "swap"))
    return {
        "span": (events[0]["t"], events[-1]["t"]),
        "arrivals_old": arrivals,
        "visits_new": len(visits),
        "gentle_starts_new": sum(1 for e in state.events if e["kind"] == "desired" and e["mode"] == vs.GENTLE),
        "hellos_new": len(hellos),
        "scene_messages_old": messages_old,
        "scene_messages_new": 2 * len(visits),
        "mismatch_minutes_old": old_mismatch / 60,
        "mismatch_minutes_new": new_mismatch / 60,
        "corrections": sum(1 for e in state.events if e["kind"] == "correction"),
        "refused": sum(1 for e in state.events if e["kind"] == "refused"),
        "assumed": assumed,
        "restarts": [e for e in state.events if e["kind"] == "restart"],
        "alarms": [e for e in state.events if e["kind"] == "alarm"],
        "history_tokens_max_old": old_max,
        "history_tokens_max_new": new_max,
        "events": state.events,
    }


def clock_time(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def report(r, show_events=False):
    s, e = r["span"]
    out = [
        f"Replayed {clock_time(s)}-{clock_time(e)} ({(e - s) / 3600:.1f} hours).",
        "",
        f"Stage:        {r['arrivals_old']} arrivals then -> {r['visits_new']} visits, "
        f"{r['hellos_new']} hellos to passersby",
        f"Messages:     {r['scene_messages_old']} stage and operator messages then -> "
        f"about {r['scene_messages_new']} (a start and an end per visit)",
        f"Wrong mode:   {r['mismatch_minutes_old']:.1f} minutes then -> {r['mismatch_minutes_new']:.1f} minutes",
        f"Corrections:  {r['corrections']} tool results carried a mode fix "
        f"({r['assumed']} on a tool call assumed after a visit's start or end message); "
        f"{r['refused']} switches the model asked for were refused",
        f"Restarts:     {len(r['restarts'])}" + "".join(
            f"\n                {clock_time(x['t'])}  {x['why']}" for x in r["restarts"]),
        f"Alarms:       {len(r['alarms'])}" + "".join(
            f"\n                {clock_time(x['t'])}  {x['why']}" for x in r["alarms"]),
        f"Conversation: largest history {r['history_tokens_max_old']} tokens then -> "
        f"{r['history_tokens_max_new']} (dialogue and injected text only; the prompt and tools add more)",
    ]
    if show_events:
        out += ["", "Events:"] + [
            f"  {clock_time(x['t'])}  {x['kind']:10} " + ", ".join(f"{k}={v}" for k, v in x.items() if k not in ("t", "kind"))
            for x in r["events"]]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("log", help="live log: /eyes-live JSON or a live-*.jsonl file")
    p.add_argument("--hold-from", type=float, default=None,
                   help="epoch seconds when the old page's 8-second 'left' hold went live")
    p.add_argument("--events", action="store_true", help="list every correction, restart, and alarm")
    a = p.parse_args()
    print(report(replay(load(a.log), a.hold_from), a.events))


if __name__ == "__main__":
    main()
