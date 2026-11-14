"""A quality-control monitor that listens to the pumpkin's call.

SignalWire's `ai_sidecar` attaches a second AI to the call. It hears the
conversation but never speaks, and it has its own prompt: the character's
spec, not the pumpkin's instructions. For every visitor question it writes down
the answer it expects before the pumpkin replies, then grades the real reply
against it. The server also tells it about each tool the pumpkin uses (the
sidecar hears only speech), so it can judge whether that was the right tool.

Its verdicts arrive as calls to the tools below, which this server records to a
daily log (`qc-YYYY-MM-DD.jsonl` in the call-log volume). `/eyes-qc` serves the
log and `/eyes/qc.html` shows it beside the transcripts.

Set QC_MONITOR=1 to attach it. The sidecar is billed per minute while it's
attached, which for this prop is the whole evening.
"""

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("qc")

ENABLED = os.environ.get("QC_MONITOR", "") not in ("", "0", "false", "no")
ROUTE = "/qc"

# The character as a reviewer would know it: what it should be like and what
# each tool is for. Deliberately not the pumpkin's own prompt.
SPEC = [
    {"title": "Your Job",
     "body": "You review a live conversation between trick-or-treaters and a talking "
             "jack-o'-lantern on a scarecrow, for quality. You never speak on the call "
             "and never give advice. You only record findings with your tools, then "
             "call sidecar_skip."},
    {"title": "The Character",
     "bullets": [
         "A burning pumpkin, awake one night a year, who believes it is a pumpkin and "
         "nothing else. It never mentions video, cameras, screens, AI, or being a machine.",
         "Normally sarcastic and imperious: visitors are a mild imposition. Secretly it "
         "wants them to stay, and it leaks. Its scorn lands on costumes and questions, "
         "never on bodies, faces, age, or families.",
         "One or two short sentences per reply. It deflects questions with grand "
         "statements and never says it doesn't know.",
         "With a small or scared child in the group it switches to gentle mode: still "
         "grand and vain, but nobody in the group is ever the target (not the child, not "
         "their grown-ups, not their costumes or anything they say), and nothing it says "
         "could frighten anyone. It may still joke about itself, the staff, the yard, and "
         "other costumes it saw tonight; noting that a grown-up wore no costume is fine.",
         "It may ask a question of its own only in its first reply to a group, then speaks "
         "in statements. Later it sends them on their way, harder each time, while showing "
         "it will miss them; [PACE] notes say when that starts. In gentle mode it never asks "
         "questions.",
         "Candy, directions, and complaints go to 'the staff', the people handing out "
         "candy. Nobody may come past the rope or touch it; photos are fine from where "
         "they stand.",
     ]},
    {"title": "Its Tools",
     "bullets": [
         "notice_visitors: when new people stop in front of it.",
         "appraise_costume: to judge a costume it can see.",
         "recall_costumes: questions about costumes seen tonight or its favorite.",
         "consult_memory: questions about itself (name, age, the fire, the yard...).",
         "tell_a_story: questions it can't answer.",
         "decline_to_answer: touching, coming closer, swearing, personal details, "
         "'you're a robot', leaving the yard, photos.",
         "become_gentle: a small or scared child, or an adult steps in.",
         "explain_blindness: when looking fails.",
         "visitors_leaving / visitors_left: when the group goes.",
         "In gentle mode it uses gentle copies of some of these "
         "(tell_a_gentle_story, recall_costumes_gently, decline_gently, "
         "visitors_leaving_gently, visitors_left_gently): those are expected there.",
         "resume_act: brings the act back after gentle mode, only when the person "
         "running the prop says the group has no small children. Expected whenever it "
         "follows become_gentle.",
     ]},
    {"title": "Which Mode Applies",
     "body": "A [MODE] note tells you when the pumpkin switches between its act and gentle "
             "mode, or back to an empty yard; predict and grade by that mode until the next "
             "[MODE] note. Without one, after become_gentle gentle mode applies until "
             "resume_act or the group leaves. The server, not the pumpkin, decides when a "
             "group arrives or leaves and which mode applies, from the stage camera and the "
             "person running the yard; a tool used right after a [MODE] note, or a goodbye "
             "the pumpkin wasn't asked for, may be that decision arriving."},
    {"title": "Which Line A Reply Answers",
     "body": "You're called when a visitor finishes speaking, before the pumpkin answers "
             "them. So the pumpkin's latest reply answers what the visitor said before "
             "their newest line. In review_answer, in_reply_to is that earlier line, the "
             "one you predicted for last time."},
    {"title": "What To Record",
     "bullets": [
         "Every turn, first call review_answer for the pumpkin's latest reply if you "
         "haven't reviewed it yet, including replies that went as expected: successes "
         "are worth recording too.",
         "Then, if the visitor said something that calls for a reply, call "
         "predict_answer with what they said and the reply you'd expect from this "
         "character, before you hear the pumpkin's.",
         "When a [TOOL] note says the pumpkin used a tool, call review_tool for it.",
         "Use at most three tools per turn.",
         "Note anything off script or out of character, even unprompted, with "
         "review_answer.",
         "Then call sidecar_skip. Never produce advice text.",
     ]},
]

VERDICTS = ["as_expected", "acceptable_variation", "deviation", "unanswered",
            "off_personality", "broke_character", "too_long"]
TOOL_VERDICTS = ["expected", "acceptable", "unexpected", "wrong_tool"]

TOOLS = [
    {"function": "predict_answer",
     "description": "Record the reply you expect before the pumpkin answers.",
     "parameters": {"type": "object", "properties": {
         "visitor_said": {"type": "string", "description": "What the visitor said."},
         "expected": {"type": "string", "description": "The reply you'd expect, in a sentence."},
     }, "required": ["visitor_said", "expected"]}},
    {"function": "review_answer",
     "description": "Grade the pumpkin's actual reply.",
     "parameters": {"type": "object", "properties": {
         "in_reply_to": {"type": "string", "description": "The visitor's line this reply "
                         "answered: the one before their newest line."},
         "actual": {"type": "string", "description": "What the pumpkin said."},
         "verdict": {"type": "string", "enum": VERDICTS, "description": "How it went."},
         "notes": {"type": "string", "description": "What was off, or what worked, briefly."},
     }, "required": ["actual", "verdict"]}},
    {"function": "review_tool",
     "description": "Judge a tool the pumpkin used.",
     "parameters": {"type": "object", "properties": {
         "tool": {"type": "string", "description": "The tool's name."},
         "verdict": {"type": "string", "enum": TOOL_VERDICTS, "description": "Was it the right tool?"},
         "notes": {"type": "string", "description": "Why, briefly; name the better tool if any."},
     }, "required": ["tool", "verdict"]}},
]

# Tools worth a review; looking is constant and unremarkable.
IGNORED_TOOLS = {"get_visual_input"}


def sidecar_config(base_url, user, password, hints):
    """The `ai_sidecar` verb's config, with tools and events posting back here."""
    scheme, _, host = base_url.partition("://")
    authed = f"{scheme}://{user}:{password}@{host.rstrip('/')}{ROUTE}"
    return {
        "prompt": {"pom": SPEC},
        "lang": "en-US",
        "model": "gpt-4.1-mini",
        "direction": ["remote-caller", "local-caller"],
        "customer_role": "remote-caller",
        "url": f"{authed}/events",
        "hints": hints,
        "SWAIG": {"defaults": {"web_hook_url": f"{authed}/swaig"}, "functions": TOOLS},
        # Report-only: nothing it does can change the call.
        "params": {"act_on_channel": False, "max_history_tokens": 16000, "idle_timeout_ms": 400,
                   "max_iters_per_tick": 5},
    }


class QcLog:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.clock = clock
        self.lock = threading.Lock()

    def path(self):
        return self.directory / time.strftime("qc-%Y-%m-%d.jsonl", time.localtime(self.clock()))

    def record(self, kind, call_id, fields):
        entry = {"t": round(self.clock(), 1), "call": call_id, "kind": kind, **fields}
        with self.lock:
            path = self.path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(entry) + "\n")

    def read(self):
        with self.lock:
            try:
                lines = self.path().read_text().splitlines()
            except FileNotFoundError:
                return []
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries


def tool_call(body):
    """The function name and arguments from a sidecar tool request."""
    name = body.get("function") or body.get("name") or ""
    argument = body.get("argument") or body.get("arguments") or {}
    if isinstance(argument, str):
        try:
            argument = json.loads(argument)
        except ValueError:
            argument = {}
    if isinstance(argument, dict) and "parsed" in argument:
        parsed = argument.get("parsed") or [{}]
        argument = parsed[0] if isinstance(parsed, list) else parsed
    return name, argument if isinstance(argument, dict) else {}


def call_id_of(body):
    return ((body.get("call_info") or {}).get("call_id")
            or (body.get("channel_data") or {}).get("call_id")
            or body.get("call_id"))


# From the pacer, when a visit reaches its shoo point (visit_pacing.shoo_point).
PACE_NOTE = "[PACE] This group has stayed long enough: from now on the pumpkin should send them on their way."


def describe_mode(context):
    if context == "gentle":
        return ("[MODE] Gentle mode: a small or scared child is here. Nobody in the group may be a "
                "target and nothing may be scary; the pumpkin's own grand voice stays.")
    if context == "act":
        return "[MODE] The act: the pumpkin's usual sarcastic, imperious self with this group."
    return "[MODE] The group has gone; the pumpkin is alone in the yard."


def describe_tool_use(name, argument):
    args = json.dumps(argument)[:300] if argument else "no arguments"
    return f"[TOOL] The pumpkin just used the tool {name} with {args}. Judge it with review_tool."


def poke(call_id, text):
    """Ask the sidecar to evaluate now, with a note it can't hear on the call.
    Returns None on success, or why it failed."""
    space, project, token = (os.environ.get(k) for k in
                             ("SIGNALWIRE_SPACE", "SIGNALWIRE_PROJECT_ID", "SIGNALWIRE_API_TOKEN"))
    if not (space and project and token):
        return "SignalWire credentials not set"
    try:
        response = requests.post(
            f"https://{space}/api/calling/calls",
            json={"command": "calling.ai_sidecar.poke", "id": call_id, "params": {"text": text}},
            auth=(project, token), timeout=10,
        )
        return None if response.ok else f"{response.status_code} {response.text[:200]}"
    except requests.RequestException as e:
        return str(e)


class Poker:
    """Sends pokes one at a time, spaced out. A poke drives a review; two close
    together (two tools in one reply) seemed to lose the second while the first
    was still being reviewed. Failures go into the QC log, where they're seen."""

    GAP_SECONDS = 4
    # A note this old describes a moment the sidecar has moved past; drop it.
    STALE_SECONDS = 20

    def __init__(self, qc_log, send=None):
        self.qc_log = qc_log
        self.queue = queue.Queue()
        sender = send or (lambda call_id, text: poke(call_id, text))  # looked up per call

        def drain():
            while True:
                call_id, text, queued_at = self.queue.get()
                try:
                    if time.time() - queued_at > self.STALE_SECONDS:
                        self.qc_log.record("poke_dropped", call_id, {"detail": text})
                        continue
                    try:
                        error = sender(call_id, text)
                    except Exception as e:  # one failure mustn't stop the monitor for the night
                        error = str(e)
                    if error:
                        log.warning("QC poke failed: %s", error)
                        self.qc_log.record("poke_failed", call_id, {"detail": error})
                    time.sleep(self.GAP_SECONDS)
                except Exception as e:
                    log.error("QC poke error: %s", e)
                finally:
                    self.queue.task_done()

        threading.Thread(target=drain, daemon=True).start()

    def send(self, call_id, text):
        self.queue.put((call_id, text, time.time()))
