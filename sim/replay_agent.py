#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["signalwire-sdk>=3.7", "requests>=2.31", "httpx"]
# ///
"""Replay a recorded night through the real server: the agent, its routes, and
the pacer, not just the visit state.

sim/replay.py checks the visit state's logic. This drives the code that runs on
the night: stage events go to /eyes-scene, the operator's presses to
/eyes-remote/mode, the model's recorded tool calls through the agent's own
tool handling, and the platform's debug events back into it. A small model of
the platform sits in between: it keeps the current context and step, applies
the actions each tool result returns, offers only the tools the current step
lists, restarts the call when the server asks, and plays the age check's
recorded outcome.

The model is still replayed, not simulated: its recorded tool calls stand for
what it tried, mapped to the tool the current step offers (a gentle copy for
the original, or back). When the server asks for a tool in a message and the
recording has no tool call within a few seconds, one is assumed, and counted.

    sim/replay_agent.py night.json --hold-from <epoch>
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))
os.environ.update(SWML_BASIC_AUTH_USER="u", SWML_BASIC_AUTH_PASSWORD="p", CALL_LOG_DIR=tempfile.mkdtemp())
for key in ("EYES_PASSWORD", "QC_MONITOR"):
    os.environ.pop(key, None)

# One clock for everything, set by the replay: the agent's cooldowns, the visit
# state, and the pacer all read time.time(). Patched before anything imports
# visit_state, whose clock defaults to time.time.
NOW = {"t": 0.0}
_real_time = time.time
time.time = lambda: NOW["t"] or _real_time()

import replay  # noqa: E402  (the recorded night's shape: classify, visits_from)

import pumpkin_agent as pa  # noqa: E402
import visit_pacing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from signalwire import AgentServer, FunctionResult  # noqa: E402

PROMPTED_TOOL_SECONDS = 4
REANNOUNCE_SECONDS = 4
CALL = "replayed-call"
ADULT = {"height": 0.6, "feetY": 0.9, "whole": True}
TODDLER = {"height": 0.25, "feetY": 0.9, "whole": True}
NATIVE = {"get_visual_input"}
# The tool a message asks for, as the model would hear it.
ASKS = re.compile(r"\bcall (notice_visitors|visitors_left|become_gentle|resume_act)\b", re.I)


class Platform:
    """Just enough of SignalWire's side: contexts, steps, tool lists, and the
    debug events it would send."""

    def __init__(self, agent):
        self.agent = agent
        contexts = agent.contexts.to_dict()
        self.tools = {(c, s["name"]): set(s.get("functions") or [])
                      for c, ctx in contexts.items() for s in ctx["steps"]}
        self.first_step = {c: ctx["steps"][0]["name"] for c, ctx in contexts.items()}
        self.context, self.step = "default", self.first_step["default"]
        self.global_data = dict(agent._global_data if hasattr(agent, "_global_data") else {})
        self.tokens = 0  # conversation size since the last isolated context
        self.max_tokens = 0

    def debug(self, **body):
        self.agent._debug_event("replay", {"call_info": {"call_id": CALL}, **body})

    def offered(self, name):
        """The tool the model can actually call for `name` in this step, or None."""
        here = self.tools.get((self.context, self.step), set())
        twins = pa.GENTLE_TWINS
        for candidate in (name, twins.get(name), {v: k for k, v in twins.items()}.get(name)):
            if candidate and candidate in here:
                return candidate
        return None

    def apply(self, result):
        """Apply a tool result's actions, as the platform would."""
        data = result.to_dict() if isinstance(result, FunctionResult) else result
        for action in data.get("action", []):
            for key, value in action.items():
                if key == "set_global_data":
                    self.global_data.update(value)
                elif key == "change_context" and value != self.context:
                    self.context, self.step = value, self.first_step[value]
                    self.tokens = 0  # every context is isolated
                    self.debug(context_change={"to_context": value})
                elif key == "change_step" and value != self.step:
                    self.step = value
                    self.debug(step_change={"to_step": value})

    def new_call(self):
        self.context, self.step, self.tokens = "default", self.first_step["default"], 0
        self.debug(session_start={})

    def add(self, tokens):
        self.tokens += tokens
        self.max_tokens = max(self.max_tokens, self.tokens)


def run(events, hold_from=None):
    events = sorted(events, key=lambda e: e["t"])
    visits, hellos = replay.visits_from(replay.presence(events, hold_from))
    NOW["t"] = events[0]["t"]

    agent = pa.agent
    pa.LEFT_GRACE_SECONDS = 0  # the new edge rules already hold "left" (visits_from)
    told = []  # (time, text) the server said into the call
    visit_pacing.send_message = lambda call_id, text, data=None: (
        told.append((NOW["t"], text)), platform.global_data.update(data or {}))
    agent.pacer._send = lambda call_id, data: platform.global_data.update(data)
    server = AgentServer(port=0)
    server.register(agent)
    pa.add_eyes_routes(server, None)
    web = TestClient(server.app)
    state = agent.visits
    state.keep_events = None
    state.tick_real, state.tick = state.tick, lambda: None  # the replay ticks, on its own clock
    on_stage = {"now": False}
    agent._stage_reporting = lambda: True
    agent._someone_on_stage = lambda: on_stage["now"]

    platform = Platform(agent)
    platform.new_call()

    timeline = [(e["t"], 1, "event", e) for e in events]
    for v in visits:
        timeline += [(v["start"], 0, "visit_start", v), (v["end"], 0, "visit_end", v)]
        timeline += [(t, 0, "joined", None) for t in v["joins"]]
    t = events[0]["t"]
    while t < events[-1]["t"]:
        timeline.append((t, 2, "tick", None))
        t += 2
    timeline.sort(key=lambda x: (x[0], x[1]))

    def post(path, body):
        web.post(path, json=body)
        agent.pacer._outbox.join()

    stats = {"tool_calls": 0, "unavailable": 0, "assumed": 0, "wrong_mode": 0.0, "restarts_done": 0,
             "not_offered": {}, "wrong_by": {}}
    last_tool = events[0]["t"]
    pending = []  # (due, tool) asked for in a message
    seen_told = 0
    seen_commands = 0
    reannounce_at = None
    last_t = events[0]["t"]
    args_for = {}

    def call_tool(name, args):
        nonlocal last_tool
        offered = platform.offered(name)
        if not offered:
            stats["unavailable"] += 1
            key = f"{name} in {platform.context}.{platform.step}"
            stats["not_offered"][key] = stats["not_offered"].get(key, 0) + 1
            return
        stats["tool_calls"] += 1
        last_tool = NOW["t"]
        result = agent.on_function_call(offered, args, {"global_data": dict(platform.global_data)})
        platform.apply(result)
        platform.debug(function_call={"function": offered})

    for t, _, kind, item in timeline:
        mode_now = pa.MODE_OF.get(platform.context)
        if mode_now != state.desired:
            stats["wrong_mode"] += t - last_t
            key = f"pumpkin {mode_now}, server {state.desired}"
            stats["wrong_by"][key] = stats["wrong_by"].get(key, 0) + (t - last_t) / 60
        last_t = t
        NOW["t"] = t

        # What the server sent since the last step: messages that ask for a
        # tool, and restart commands for the page.
        for at, text in told[seen_told:]:
            asked = ASKS.search(text)
            if asked:
                pending.append((at + PROMPTED_TOOL_SECONDS, asked.group(1)))
        seen_told = len(told)
        commands = web.get(f"/eyes-remote/command?after={seen_commands}").json()
        for command in commands["commands"]:
            if command["kind"] == "restart":
                stats["restarts_done"] += 1
                platform.new_call()
                reannounce_at = t + REANNOUNCE_SECONDS
        seen_commands = commands["seq"]
        if reannounce_at and t >= reannounce_at:
            reannounce_at = None
            if on_stage["now"]:
                post("/eyes-scene", {"type": "arrived", "people": [ADULT, dict(ADULT)], "reannounced": True})
        for due, tool in [p for p in pending if p[0] <= t]:
            pending.remove((due, tool))
            if t - last_tool > PROMPTED_TOOL_SECONDS:
                stats["assumed"] += 1
                call_tool(tool, {"youngest": "teen"} if tool == "notice_visitors" else {"reason": "upset"})

        if kind == "visit_start":
            on_stage["now"] = True
            post("/eyes-scene", {"type": "arrived", "people": [ADULT, TODDLER if item["child"] else dict(ADULT)]})
        elif kind == "visit_end":
            on_stage["now"] = False
            post("/eyes-scene", {"type": "left"})
        elif kind == "joined":
            post("/eyes-scene", {"type": "joined", "people": [ADULT, TODDLER]})
        elif kind == "tick":
            state.tick_real()
        else:
            e = item
            data = e.get("data") or {}
            if e["event"] == "conversation_add":
                platform.add(data.get("tokens") or 0)
            c = replay.classify(e)
            if e["event"] == "swaig_call":
                try:
                    args_for[data.get("command_name")] = json.loads(data.get("command_arg") or "{}")
                except ValueError:
                    args_for[data.get("command_name")] = {}
            if not c:
                continue
            if c[0] == "line":
                platform.debug(conversation_add={"role": "assistant", "content": c[1]})
            elif c[0] == "operator":
                post("/eyes-remote/mode", {"mode": c[1]})
            elif c[0] == "tool":
                name, blocked = c[1]
                if name in NATIVE:
                    continue
                if name == "notice_visitors" and blocked:
                    # The age check failed: the gate's on_fail, which the platform applies.
                    if platform.offered("notice_visitors"):
                        platform.apply(pa.enter_gentle("There's a young child here, or someone looks scared."))
                        platform.debug(function_call={"function": "notice_visitors", "blocked": True})
                        last_tool = t
                    else:
                        stats["unavailable"] += 1
                    continue
                call_tool(name, args_for.get(name, {}))
        agent.pacer._outbox.join()

    events_by_kind = {}
    for ev in state.events:
        events_by_kind.setdefault(ev["kind"], []).append(ev)
    return {
        "span": (events[0]["t"], events[-1]["t"]),
        "visits": len(visits), "hellos": len(hellos),
        "wrong_mode_minutes": stats["wrong_mode"] / 60,
        "tool_calls": stats["tool_calls"], "unavailable": stats["unavailable"], "assumed": stats["assumed"],
        "corrections": len(events_by_kind.get("correction", [])),
        "refused": len(events_by_kind.get("refused", [])),
        "restarts": events_by_kind.get("restart", []),
        "alarms": events_by_kind.get("alarm", []),
        "messages": len(told),
        "nudges": len(events_by_kind.get("nudge", [])),
        "not_offered": stats["not_offered"],
        "wrong_by": stats["wrong_by"],
        "max_tokens": platform.max_tokens,
    }


def clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def report(r):
    s, e = r["span"]
    return "\n".join([
        f"Replayed through the server, {clock(s)}-{clock(e)}.",
        f"Visits:        {r['visits']}, plus {r['hellos']} hellos to passersby",
        f"Wrong mode:    {r['wrong_mode_minutes']:.1f} minutes (the platform's context vs the server's choice)" + "".join(
            f"\n                 {v:5.1f}  {k}" for k, v in sorted(r["wrong_by"].items(), key=lambda kv: -kv[1])),
        f"Tool calls:    {r['tool_calls']} replayed; {r['unavailable']} not offered in that step; "
        f"{r['assumed']} assumed after a message asked for one",
        f"Corrections:   {r['corrections']}; switches refused: {r['refused']}",
        "Not offered:   " + ", ".join(f"{k} ({v})" for k, v in sorted(r["not_offered"].items(), key=lambda kv: -kv[1])[:6]),
        f"Messages:      {r['messages']} said into the call by the server, {r['nudges']} of them asking for a "
        "tool call after 8 seconds in the wrong mode",
        f"Restarts:      {len(r['restarts'])}" + "".join(f"\n                 {clock(x['t'])}  {x['why']}" for x in r["restarts"]),
        f"Alarms:        {len(r['alarms'])}" + "".join(f"\n                 {clock(x['t'])}  {x['why']}" for x in r["alarms"]),
        f"Conversation:  largest {r['max_tokens']} tokens of dialogue and injected text in one context",
    ])


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("log")
    p.add_argument("--hold-from", type=float, default=None)
    a = p.parse_args()
    print(report(run(replay.load(a.log), a.hold_from)))


if __name__ == "__main__":
    main()
