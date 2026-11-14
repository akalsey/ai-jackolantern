#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["signalwire-sdk>=3.7", "requests>=2.31", "httpx"]
# ///
"""Checks for pumpkin_agent.py's safety paths with the server's visit state in
charge: who goes gentle, who can leave it, how every tool call corrects the
pumpkin's mode, and what the stage camera and the operator's switch send into
the call. Run with: uv run tests/test_agent.py"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.update(SWML_BASIC_AUTH_USER="u", SWML_BASIC_AUTH_PASSWORD="p", CALL_LOG_DIR=tempfile.mkdtemp())
os.environ.pop("EYES_PASSWORD", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pumpkin_agent as pa  # noqa: E402
import visit_pacing  # noqa: E402
import visit_state as vs  # noqa: E402

pa.LEFT_GRACE_SECONDS = 0  # "left" goes out at once, except in the test of holding it
from fastapi.testclient import TestClient  # noqa: E402
from signalwire import AgentServer  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


agent = pa.agent
told, merged = [], []
visit_pacing.send_message = lambda call_id, text, data=None: told.append((text, data))
agent.pacer._send = lambda call_id, data: merged.append(data)
server = AgentServer(port=0)
server.register(agent)
pa.add_eyes_routes(server, None)
web = TestClient(server.app)


def result(fn, args=None, global_data=None):
    return fn(args or {}, {"global_data": global_data or {}}).to_dict()


def gentle_tool(name):
    """Gentle mode's copy of a tool, as the platform would call it."""
    handler = agent._tool_registry._swaig_functions[name].handler
    return lambda args, raw_data: handler(args, raw_data)


def actions(r):
    return {k: v for a in r.get("action", []) for k, v in a.items()}


def event(**body):
    agent._debug_event("x", {"call_info": {"call_id": "c1"}, **body})
    agent.pacer._outbox.join()


def fresh(visit=False, child=False, checked=False):
    """A clean visit state (the routes hold the same object, so it's reset in
    place), optionally with a group already in front."""
    hook = agent.visits.on_event
    agent.visits.__init__()
    agent.visits.on_event = hook
    agent.visits.wait_for_reports = True  # as the live server runs it
    agent._paced_visit = None
    if visit:
        agent.visits.visit_started(child=child)
        if checked:
            agent.visits.age_check(True)
    told.clear(), merged.clear()


def stage(show_live):
    """The pumpkin page reporting its stage (None: no page, no detector)."""
    agent._stage_reporting = (lambda: False) if show_live is None else (lambda: True)
    agent._someone_on_stage = lambda: show_live is not False


def call(name, args=None, global_data=None):
    """A tool request as the platform makes it, through the correction."""
    return actions(agent.on_function_call(name, args or {}, {"global_data": global_data or {}}).to_dict())


def post(path, body):
    r = web.post(path, json=body)
    agent.pacer._outbox.join()
    return r


event(session_start={})
stage(None)
adult = {"height": 0.6, "feetY": 0.9, "whole": True}
toddler = {"height": 0.25, "feetY": 0.9, "whole": True}

# Who goes gentle when the pumpkin notices a group.
for args, name in [({"youngest": "young_child"}, "a young child"), ({"youngest": "teen", "anyone_scared": True}, "anyone scared"),
                   ({"youngest": "cant_tell"}, "can't tell")]:
    fresh(visit=True)
    r = actions(result(agent.notice_visitors, args))
    check(f"{name} goes gentle", r.get("change_context") == "gentle" and agent.visits.desired == vs.GENTLE)
fresh(visit=True)
r = actions(result(agent.notice_visitors, {"youngest": "teen", "costumes": "a witch"}))
check("teens get the act", r.get("change_context") == "act" and r.get("change_step") == "greet"
      and r["set_global_data"]["visitors"] == "a witch")
check("recording a group leaves the pacer's mood and count alone",
      "visit_mood" not in r["set_global_data"] and "visit_turns" not in r["set_global_data"])
fresh(visit=True)
r = actions(result(agent.notice_visitors, {"youngest": "teen"},
                   {"gentle_left_at": pa.time.time(), "returning_gentle": True}))
check("right after a gentle group, the same group coming back is still gentle", r.get("change_context") == "gentle")
fresh(visit=True)
r = actions(result(agent.notice_visitors, {"youngest": "teen"},
                   {"gentle_left_at": pa.time.time(), "seen_before": "You have seen these people before: ...",
                    "returning_gentle": False}))
check("...but a recognized group that wasn't the gentle one gets its own check", r.get("change_context") == "act")
fresh(visit=True, child=True)
r = actions(result(agent.notice_visitors, {"youngest": "teen"}))
check("a small child measured by the stage wins over what the pumpkin saw", r.get("change_context") == "gentle")
fresh()
stage(False)
r = result(agent.notice_visitors, {"youngest": "teen"})
check("with the stage reporting nobody there, noticing starts nothing",
      "change_context" not in actions(r) and agent.visits.visit is None)
stage(None)
fresh()
r = actions(result(agent.notice_visitors, {"youngest": "teen"}))
check("without a stage detector, noticing them starts the visit",
      r.get("change_context") == "act" and agent.visits.visit is not None)
fresh(visit=True)
r = actions(result(agent.notice_visitors, {"youngest": "young_child", "costumes": "a tiny dinosaur"}))
later = result(agent.explain_blindness, global_data=r["set_global_data"])
check("a child sent straight to gentle mode isn't 'unseen' a moment later", "a tiny dinosaur" in later["response"])
check("...and gentle mode knows how to answer 'are you real?' with their costume",
      "a tiny dinosaur" in r["set_global_data"]["real_answer"])

# Blindness is only for a group the pumpkin hasn't seen.
r = result(agent.explain_blindness, global_data={"visitors": "a witch and a ghost", "seen_at": pa.time.time()})
check("a failed look at a group it just saw isn't blindness", "a witch and a ghost" in r["response"])
r = result(agent.explain_blindness, global_data={"visitors": "a witch and a ghost", "seen_at": pa.time.time() - 600})
check("...but costumes seen long ago aren't trusted", "can't see right now" in r["response"])
first = result(agent.appraise_costume, {"costume": "Zombie", "craft": "homemade"})
again = result(agent.appraise_costume, {"costume": "zombie", "craft": "homemade"},
               {"appraised": actions(first)["set_global_data"]["appraised"]})
check("a costume is judged once per visit", "already judged" in again["response"])

# The ratchet: toward gentle is the model's to ask; back to the act is only the operator's.
fresh(visit=True)
agent.visits.observed(agent.visits.desired)  # the platform reports the switch
r = actions(result(agent.become_gentle, {"reason": "looks_or_sounds_young"}))
check("become_gentle during a visit switches", r.get("change_context") == "gentle")
r = result(agent.resume_act, global_data={"visitors": "a robot"})
check("resume_act refuses without the operator", "change_context" not in actions(r))
agent.visits.operator_choice("normal")
r = actions(result(agent.resume_act, global_data={"visitors": "a robot", "seen_at": 5}))
check("resume_act works for the operator, and keeps what they're wearing",
      r.get("change_context") == "act" and r["set_global_data"]["visitors"] == "a robot")
r = result(agent.become_gentle, {"reason": "looks_or_sounds_young"})
check("after the operator's Normal, become_gentle is refused", "change_context" not in actions(r))

# Leaving is the stage's call when there is a stage.
fresh(visit=True, checked=True)
agent.visits.observed(agent.visits.desired)  # the platform reports the switch
stage(True)
r = result(gentle_tool("visitors_left_gently"))
check("visitors_left refuses while the stage still sees them", "change_context" not in actions(r))
r = result(agent.visitors_leaving)
check("a goodbye while they're still there gets a parting line, not the end",
      "change_context" not in actions(r) and "parting line" in r["response"])
agent.visits.visit_ended()
r = actions(result(agent.visitors_left))
check("once the stage says they've gone, leaving goes back to the yard", r.get("change_context") == "default")
stage(None)
fresh(visit=True, checked=True)
agent.visits.observed(agent.visits.desired)  # the platform reports the switch
r = actions(result(agent.visitors_leaving))
check("without a stage, the pumpkin's word ends the visit", r.get("change_context") == "default")

# Every other tool call carries the server's correction.
fresh(visit=True, checked=True)
agent.visits.observed(agent.visits.desired)  # the platform reports the switch
agent.visits.child_joined()
r = call("consult_memory", {"topic": "name"})
check("in the act with a small child wanted, any tool switches to gentle", r.get("change_context") == "gentle")
fresh()
agent.visits.observed(vs.GENTLE)
r = call("tell_a_gentle_story", {"question": "why?"})
check("gentle with nobody here: any tool goes back to the yard, once the platform reports it",
      r.get("change_context") == "default" and agent.visits.pending[0] == vs.YARD)
r = call("decline_gently", {"request": "profanity"})
check("...and the next tool before that report gets the switch again, not gentle wording",
      r.get("change_context") == "default")
event(context_change={"to_context": "default"})
check("...the platform's report makes it so", agent.visits.actual == vs.YARD and agent.visits.pending is None)
fresh(visit=True)
r = call("become_gentle", {"reason": "upset"})
check("a mode tool isn't pre-empted: it asks the state itself", r.get("change_context") == "gentle")
fresh(visit=True, checked=True)
agent.visits.observed(agent.visits.desired)  # the platform reports the switch
agent.visits.observed(step="banter")
agent.visits.shoo_point()
r = call("consult_memory", {"topic": "age"})
check("past the shoo point, the next tool moves the act to shoo, after answering",
      r.get("change_step") == "shoo" and "set_global_data" in r)

fresh(visit=True)  # a new, unchecked group while the pumpkin is still in the act
agent.visits.observed(vs.ACT)
r = agent.on_function_call("consult_memory", {"topic": "name"}, {"global_data": {}}).to_dict()
check("back to the yard for a new group says nothing in the act's voice",
      actions(r).get("change_context") == "default" and "parting" not in r["response"] and not r.get("post_process"))

# The platform's own reports keep the state true.
fresh()
event(context_change={"to_context": "gentle"})
check("a context change is believed", agent.visits.actual == vs.GENTLE)
event(session_start={})
check("a new call starts in the yard", agent.visits.actual == vs.YARD)
fresh(visit=True)
event(function_call={"function": "notice_visitors", "blocked": True})
check("a failed age check wants gentle mode", agent.visits.desired == vs.GENTLE)
fresh()
agent.visits.last_restart = None
line = "~LN(English)-; Ah, visitors. My fiery eyes are clouded by smoke and shadows tonight."
for _ in range(4):
    event(conversation_add={"role": "assistant", "content": line})
commands = web.get("/eyes-remote/command?after=0").json()["commands"]
check("a pumpkin repeating itself restarts the call through the page",
      any(c["kind"] == "restart" for c in commands))
status = web.get("/eyes-remote/status").json()
check("...and the remote shows why", any("repeating" in a["text"] for a in status["alarms"]))

# No tool call comes: after 8 seconds the server asks for one.
fresh()
agent.visits.observed(vs.GENTLE)  # gentle with nobody here, say after a failed age check
agent.visits.mismatch_since -= 9
agent.visits.tick()
agent.pacer._outbox.join()
check("stuck in the wrong mode, the server asks for the tool that fixes it",
      told and told[-1][0] == pa.SCENE_MESSAGES["left"])

fresh(visit=True, checked=True)
agent.visits.observed(vs.GENTLE)  # a gentle goodbye's switch never landed; a new group can take the act
agent.visits.mismatch_since -= 9
agent.visits.tick()
agent.pacer._outbox.join()
check("...and doesn't claim the operator decided when it didn't", told and told[-1][0] == pa.NUDGE_ACT)

# The stage camera.
LEFT = pa.SCENE_MESSAGES["left"]
fresh()
post("/eyes-scene", {"type": "arrived", "people": [adult, dict(adult)]})
check("an arrival starts a visit that waits for its age check", told[-1][0] == pa.SCENE_MESSAGES["arrived"]
      and agent.visits.desired == vs.YARD and agent.pacer.mode("c1") == "normal")
agent.visits.age_check(True)
post("/eyes-scene", {"type": "joined", "people": [adult, dict(adult)]})
check("an adult joining mid-show says nothing", told[-1][0] == pa.SCENE_MESSAGES["arrived"])
post("/eyes-scene", {"type": "joined", "people": [adult, toddler]})
check("a small child joining mid-show goes gentle", told[-1][0] == pa.JOINED_WITH_CHILD
      and agent.visits.desired == vs.GENTLE and agent.pacer.mode("c1") == "gentle")
first_visit = agent.visits.visit["id"]
before = len(agent.recent.groups)
post("/eyes-scene", {"type": "arrived", "people": [adult, toddler], "reannounced": True})
check("a re-announced group is the same visit, not a new arrival",
      agent.visits.visit["id"] == first_visit and len(agent.recent.groups) == before)
post("/eyes-scene", {"type": "arrived", "people": [adult, dict(adult)]})
check("a new arrival during a visit ends the old one first",
      told[-1][0] == pa.ARRIVED_DURING_VISIT and agent.visits.visit["id"] != first_visit)
post("/eyes-scene", {"type": "left"})
check("'left' ends the visit and clears the group's hints", told[-1][0] == LEFT
      and told[-1][1]["stage_hint"] == pa.NO_STAGE_HINT and agent.visits.visit is None
      and agent.pacer.mode("c1") == "idle")
post("/eyes-scene", {"type": "arrived", "people": [adult, toddler]})
check("an arrival with a much shorter person says small child",
      told[-1][0] == pa.ARRIVED_WITH_CHILD and agent.visits.desired == vs.GENTLE)
post("/eyes-scene", {"type": "left"})

# The operator's switch overrides the automatic decision for the group in front.
fresh()
post("/eyes-scene", {"type": "arrived", "people": [adult, toddler]})
agent.visits.observed(agent.visits.desired)  # the platform reports the switch
told.clear()
post("/eyes-remote/mode", {"mode": "normal"})
check("Normal for a gentle group asks for resume_act", told and told[-1][0] == pa.OPERATOR_NORMAL
      and agent.visits.desired == vs.ACT)
event(function_call={"function": "notice_visitors", "blocked": True})
check("...and holds: a failed age check later doesn't undo it", agent.visits.desired == vs.ACT)
told.clear()
post("/eyes-scene", {"type": "joined", "people": [adult, toddler]})
check("...nor does a child measured joining", not told and agent.visits.desired == vs.ACT)
post("/eyes-scene", {"type": "left"})
post("/eyes-scene", {"type": "arrived", "people": [adult, toddler]})
check("after 'left', the last group's Normal doesn't cover a small child arriving",
      told[-1][0] == pa.ARRIVED_WITH_CHILD)
post("/eyes-scene", {"type": "left"})
post("/eyes-remote/mode", {"mode": "normal"})
post("/eyes-scene", {"type": "arrived", "people": [adult, toddler]})
check("Normal pressed just before an arrival: no small-child message", told[-1][0] == pa.SCENE_MESSAGES["arrived"])
post("/eyes-scene", {"type": "left"})
post("/eyes-remote/mode", {"mode": "gentle"})
post("/eyes-scene", {"type": "arrived", "people": [adult, dict(adult)]})
check("Gentle pressed just before an arrival: gentle even for adults", told[-1][0] == pa.ARRIVED_WITH_CHILD)
post("/eyes-scene", {"type": "left"})

# "What is that?" gets an answer without a look.
r = result(agent.guess_what_it_is, {"thing": "the squirrel"})
check("the act pronounces on a thing without looking", "Don't look" in r["response"] and "the squirrel" in r["response"])
r = result(gentle_tool("guess_what_it_is_gently"), {"thing": "my bag"})
check("gentle mode guesses, delighted", any(g in r["response"] for g in pa.GENTLE_WHAT_IT_IS))

# A story one beat per call.
r = actions(result(agent.tell_a_story, {"question": "Why is the sky?"}))
story = r["set_global_data"]["story"]
r2 = result(agent.tell_a_story, {"question": "Why is the sky?"}, {"story": story})
check("a story goes on, one beat per call", story["beat"] == 1 and story["turns"][0] in r2["response"])

# The context decides the wording, never a flag: gentle mode has its own copies.
check("gentle mode's decline is gentle mode's",
      result(gentle_tool("decline_gently"), {"request": "profanity"})["response"] == pa.GENTLE_BOUNDARY_GUIDANCE["profanity"])
check("the act's decline is the act's",
      result(agent.decline_to_answer, {"request": "profanity"})["response"] == pa.BOUNDARY_GUIDANCE["profanity"])
check("gentle mode's costume answer puts theirs above everyone else's tonight",
      pa.GENTLE_TONIGHT_SHAPE in result(gentle_tool("recall_costumes_gently"))["response"])
names = set(agent._tool_registry._swaig_functions)
check("every gentle copy exists", set(pa.GENTLE_TWINS.values()) <= names)

# In the dark a person standing still drops out or changes color for a moment:
# a "left" is held, and an "arrived" soon after cancels it.
fresh()
post("/eyes-scene", {"type": "arrived", "people": [adult, dict(adult)]})
pa.LEFT_GRACE_SECONDS = 0.3
told.clear()
post("/eyes-scene", {"type": "left"})
post("/eyes-scene", {"type": "arrived", "people": [adult, dict(adult)]})
pa.time.sleep(0.5)
check("a 'left' followed quickly by an 'arrived' says nothing", not told and agent.visits.visit)
post("/eyes-scene", {"type": "left"})
pa.time.sleep(0.5)
agent.pacer._outbox.join()
check("a 'left' with nobody coming back goes out after the wait",
      told and told[-1][0] == LEFT and agent.visits.visit is None)
told.clear()
witch = {**adult, "look": [0.9, 0.1, 0.1, 0.2, 0.2, 0.2]}
pirate = {**adult, "look": [0.2, 0.3, 0.8, 0.5, 0.5, 0.5]}
post("/eyes-scene", {"type": "arrived", "people": [witch]})
first_visit = agent.visits.visit["id"]
post("/eyes-scene", {"type": "left"})
post("/eyes-scene", {"type": "arrived", "people": [dict(witch)]})
check("the same person back within the hold continues the visit", agent.visits.visit["id"] == first_visit)
post("/eyes-scene", {"type": "left"})
post("/eyes-scene", {"type": "arrived", "people": [pirate]})
check("someone else stepping up within the hold is a new visit",
      agent.visits.visit["id"] != first_visit and told[-1][0] == pa.ARRIVED_DURING_VISIT)
pa.LEFT_GRACE_SECONDS = 0
post("/eyes-scene", {"type": "left"})
told.clear()
post("/eyes-scene", {"type": "left"})
check("a second 'left' with no visit says nothing", not told)

# The page APIs.
r = web.post("/eyes-scene", content='{"type": "left"}', headers={"content-type": "text/plain"})
check("a text/plain POST is refused", r.status_code == 415)
check("call IDs with a trailing newline are refused", not pa.SAFE_CALL_ID.fullmatch("abc\n"))

sys.exit(fails)
