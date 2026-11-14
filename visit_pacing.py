"""Keeps visits short.

Left to itself, a character who secretly wants company will talk forever. So
the server, not the model, counts exchanges: the platform posts debug events as
the call happens, a visitor speaking followed by the pumpkin replying is one
exchange, and from the shoo point on (SHOO_AFTER, GENTLE_SHOO_AFTER) the server
pushes a sterner `visit_mood` into the call's global_data, which the base
prompt's "Right Now" section reads, and says SHOO_NOW into the call once, so
the pumpkin moves visitors along with rising insistence without having to keep
count itself.

Visits begin, go gentle, and end when the server's visit state says so
(visit_state.py), not on the pumpkin's own steps: the server knows first.
"""

import logging
import queue
import threading
import time

from signalwire import RestClient

log = logging.getLogger("pacing")

# Exchanges in which the pumpkin may still ask a question; after these it speaks
# only in statements, since questions keep a conversation going.
QUESTIONS_UNTIL = 1
# Exchanges before the pumpkin starts sending visitors on their way. Gentle mode
# is shorter still: a talkative small child needs no encouragement to stay.
SHOO_AFTER = 3
GENTLE_SHOO_AFTER = 2

# Before the shoo point. The act alone tends to shoo from the first line, so
# these say outright not to yet. After the first couple of exchanges, questions
# stop too.
CALM = "Sarcastic and imperious, but in no hurry; don't tell them to leave yet. A question of your own is fine."
STATEMENTS = (
    "Sarcastic and imperious, in statements only: no questions, nothing that asks them to answer. "
    "Don't tell them to leave yet."
)
# Gentle mode never asks questions: children keep talking without being asked.
# Its tone comes from the gentle context; the mood is only about timing.
GENTLE_CALM = "Brief, in statements only: no questions, nothing that asks them to answer."

# From the SHOO_AFTER-th exchange on, one per exchange; the last one repeats.
ACT_MOODS = [
    "That's enough of them. Move on to shooing them, and tell them to move along. "
    "You still secretly want them to stay, so let one wistful note slip. No questions "
    "of your own.",
    "They're still here. Insist, more grandly, with one reason: you have "
    "important standing to do, or the staff have candy, or other houses exist. No questions of your own.",
    "Still here. Be thoroughly dismissive: speak to them only to send them off, and "
    "wave their questions away. No questions of your own.",
    "Still here. Maximum theatrical dismissal: announce that you are no longer "
    "receiving visitors and refuse to engage, with one sad last line at most. No "
    "questions.",
]
GENTLE_MOODS = [
    "Time for them to go: send them off to the next house for more candy, grandly, and "
    "glad they came. No questions.",
    "Say goodbye and wish them a happy Halloween. No questions.",
    "Only a short goodbye now, however much they say: don't answer at length "
    "or take up new topics. No questions.",
]


# Said into the call once, the moment a visit reaches its shoo point. A mood
# change in global_data is silent and waits for the model to notice; this makes
# the switch happen on that turn.
SHOO_NOW = (
    "They've stayed long enough. In this reply, start sending them on their way: "
    "grandly, with one reason, and a hint you'll miss them. No questions."
)
GENTLE_SHOO_NOW = (
    "It's time for them to go. In this reply, send them off to the next house for more "
    "candy, glad they came. No questions."
)


def shoo_point(gentle):
    """The exchange from which the pumpkin sends visitors on their way."""
    return GENTLE_SHOO_AFTER if gentle else SHOO_AFTER


def mood_for(exchanges, gentle):
    start = shoo_point(gentle)
    if exchanges < start:
        if gentle:
            return GENTLE_CALM
        return CALM if exchanges < QUESTIONS_UNTIL else STATEMENTS
    moods = GENTLE_MOODS if gentle else ACT_MOODS
    return moods[min(exchanges - start, len(moods) - 1)]


class VisitPacer:
    """Counts each visit's exchanges and pushes its mood into the call.

    The server says when a visit starts, goes gentle, and ends (from its own
    visit state, see visit_state.py); the pacer only counts, from the platform's
    debug events, and never infers the visit from the pumpkin's steps.
    """

    def __init__(self, send, message=None):
        self._visits = {}
        # How SHOO_NOW reaches the call (send_message unless a test swaps it).
        self._message = message or (lambda call_id, text: send_message(call_id, text))
        self._lock = threading.Lock()
        # The live call, for messages that come from outside the call (the stage
        # detector). One pumpkin, one call at a time.
        self.current_call = None
        # Called with the call ID when a visit reaches its shoo point; the server
        # moves the act to its shoo step on the next tool call.
        self.on_shoo = None
        # Whether a visitor speaking may start a visit: only when the stage
        # detector isn't reporting, so the pumpkin still paces visits it noticed
        # by ear. Set by the server.
        self.start_on_speech = lambda: False
        # One sender, in order, off the request thread: a later, sterner mood
        # can't be overtaken by an earlier one still in flight, and a stage
        # "arrived" can't be overtaken by the "left" before it.
        self._send = send
        self._outbox = queue.Queue()

        def drain():
            # One failure must not stop everything after it: this queue carries
            # every mood, stage event, and operator message for the whole night.
            while True:
                job = self._outbox.get()
                try:
                    job()
                except Exception as e:
                    log.error("Couldn't deliver to the call: %s", e)
                finally:
                    self._outbox.task_done()  # lets a test wait for delivery

        threading.Thread(target=drain, daemon=True).start()

    def handle(self, body):
        call_id = (body.get("call_info") or {}).get("call_id")
        if not call_id:
            return
        shoo = None
        # One event at a time: the platform's posts can arrive concurrently.
        with self._lock:
            # The newest call wins: a reloaded page's old call can linger for a
            # minute, still sending events, and must not take the stage messages back.
            if "session_end" in body:
                if self.current_call == call_id:
                    self.current_call = None
                self._visits.pop(call_id, None)
                return
            if "session_start" in body or self.current_call is None:
                self.current_call = call_id
            visit = self._visits.get(call_id)
            if visit is None:
                if "speech_detect" in body and self.start_on_speech():
                    self._queue(call_id, self._start(call_id))
                    self._visits[call_id]["heard"] = True
                return
            # Independent checks: one event can carry both.
            if "speech_detect" in body:
                visit["heard"] = True
            if "ai_completion" in body and visit["heard"]:
                visit["heard"] = False
                visit["exchanges"] += 1
                if visit["exchanges"] == shoo_point(visit["gentle"]):
                    text = GENTLE_SHOO_NOW if visit["gentle"] else SHOO_NOW
                    self._outbox.put(lambda: self._message(call_id, text))
                    shoo = call_id
                self._queue(call_id, self._mood_update(visit))
        if shoo and self.on_shoo:
            self.on_shoo(shoo)

    def start_visit(self, call_id, gentle=False):
        """A group has stepped up (the server's visit state says so)."""
        with self._lock:
            self._queue(call_id, self._start(call_id, gentle=gentle))

    def set_gentle(self, call_id, gentle):
        """The visit's mode changed; it keeps its count."""
        with self._lock:
            visit = self._visits.get(call_id)
            if visit and visit["gentle"] != gentle:
                visit["gentle"] = gentle
                self._queue(call_id, self._mood_update(visit))

    def end_visit(self, call_id):
        """The group has gone (the stage camera said so)."""
        with self._lock:
            self._queue(call_id, self._end(call_id))

    def merge(self, call_id, global_data):
        """Merge global_data into the call silently, in order with everything else."""
        self._outbox.put(lambda: self._send(call_id, global_data))

    def tell(self, call_id, text, global_data=None):
        """send_message, in order with everything else sent into the call."""
        self._outbox.put(lambda: send_message(call_id, text, global_data))

    def mode(self, call_id):
        """"gentle", "normal", or "idle" for the visit on this call."""
        with self._lock:
            visit = self._visits.get(call_id)
        if visit is None:
            return "idle"
        return "gentle" if visit["gentle"] else "normal"

    def visit_age(self, call_id):
        """Seconds since the visit on this call started, or None if there isn't one."""
        with self._lock:
            visit = self._visits.get(call_id)
        return None if visit is None else time.time() - visit["started"]

    def _queue(self, call_id, update):
        # Queued inside the lock, so updates go out in the order they were made.
        if update is not None:
            self._outbox.put(lambda: self._send(call_id, update))

    def _start(self, call_id, gentle=False):
        visit = {"exchanges": 0, "gentle": gentle, "heard": False, "mood": None, "started": time.time()}
        self._visits[call_id] = visit
        return self._mood_update(visit)

    def _end(self, call_id):
        visit = self._visits.pop(call_id, None)
        if visit is None:
            return None
        # Back to the opening mood for the next group, which sets its own.
        return {"visit_turns": 0, "visit_mood": CALM}

    def _mood_update(self, visit):
        """The global_data to push, or None when the mood hasn't changed."""
        mood = mood_for(visit["exchanges"], visit["gentle"])
        if mood == visit["mood"]:
            return None
        visit["mood"] = mood
        return {"visit_turns": visit["exchanges"], "visit_mood": mood}


def send_message(call_id, text, global_data=None):
    """Tell the live call something as a system message, which the pumpkin
    answers. global_data, if given, is merged into the call in the same request."""
    try:
        RestClient().calling.ai_message(call_id, role="system", message_text=text, global_data=global_data)
        log.info("Told %s: %s", call_id, text)
    except Exception as e:
        log.error("Couldn't message %s: %s", call_id, e)


def make_sender():
    """Push global_data into a live call. Merged, and silent until a prompt reads it."""
    client = None

    def send(call_id, data):
        nonlocal client
        try:
            client = client or RestClient()
            client.calling.ai_message(call_id, global_data=data)
            log.info("Visit on %s: exchange %s, mood: %s", call_id, data["visit_turns"], data["visit_mood"])
        except Exception as e:
            log.error("Couldn't update visit pacing on %s: %s", call_id, e)

    return send
