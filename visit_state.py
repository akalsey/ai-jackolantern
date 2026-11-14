"""The server's own view of what the pumpkin should be doing, kept in code.

The platform's contexts and steps are the pumpkin's state machine, but only the
model can move it: a step or context changes when a tool's result says so, and
a tool runs only when the model calls it. Facts the server learns first (the
stage camera saw people arrive or leave, the operator pressed Gentle or Normal,
the age check failed) used to reach the model as requests in prose, and on
Halloween night it ignored them for 50 minutes.

So the server keeps the authoritative state here, and the model's machine is
made to match it:

- Every tool result carries a correction when the two disagree (`correction`).
  If no tool call comes within a few seconds, the server asks for one.
- A mismatch that lasts anyway, or a pumpkin repeating itself, restarts the
  call (`tick`, `assistant_line`), at most once every few minutes; past that,
  and for a visit that goes on far too long, the operator is alerted instead.
- Safety only ratchets one way: anything automatic can move a group toward
  gentle mode; only the operator brings the act back.

Modes: "yard" (nobody here, between visits), "act", and "gentle". Each is an
isolated context in the agent, so entering one truncates the conversation.

Pure logic with an injectable clock, so it can be tested, and replayed against
a recorded night (`sim/replay.py`).
"""

import difflib
import functools
import re
import threading
import time

YARD, ACT, GENTLE = "yard", "act", "gentle"
# The step each mode starts on. The act also has banter (after the greeting) and
# shoo (from the pacer's shoo point on).
FIRST_STEP = {YARD: "idle", ACT: "greet", GENTLE: "reassure"}

# The operator's choice with nobody there applies to a group arriving this soon.
OPERATOR_AHEAD_SECONDS = 20

# How long the pumpkin's mode may disagree with the server's before the server
# asks for a tool call in a message (the correction rides on it), and before it
# restarts the call. A tool call usually comes within seconds on its own.
NUDGE_SECONDS = 8
STUCK_SECONDS = 30
# A visit this long means someone is lingering or the stage missed them leaving:
# worth the operator's look.
MODE_HELD_SECONDS = 600
# A reply this similar to one of the recent ones counts as a repeat, and this
# many repeats this close together means the conversation is in a loop.
REPEAT_SIMILARITY = 0.8
REPEATS_TO_RESTART = 3
REPEAT_WINDOW_SECONDS = 120
RECENT_LINES = 6
# Automatic restarts are spaced at least this far apart; past that, a problem
# the restart didn't fix goes to the operator instead of becoming a redial loop.
RESTART_GAP_SECONDS = 300
# Looks that come back empty this often mean the pumpkin can't see.
BLIND_ALARM_COUNT = 10
BLIND_WINDOW_SECONDS = 600


def normalize(line):
    # The platform prefixes each spoken line with its language ("~LN(English)-;").
    line = re.sub(r"^~LN\([^)]*\)-;\s*", "", line or "")
    return re.sub(r"[^a-z ]", "", line.lower()).strip()


def locked(method):
    # The server calls in from request handlers, the debug-event feed, and a
    # timer thread at once. Restarts and alarms are handed to on_event only
    # after the lock is released, so the server's handlers (the remote, the live
    # log) can never be waiting on it.
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            self._depth += 1
            try:
                result = method(self, *args, **kwargs)
            finally:
                self._depth -= 1
            fire, self._to_fire = (self._to_fire, []) if self._depth == 0 else ([], self._to_fire)
        for kind, why in fire:
            if self.on_event:
                self.on_event(kind, why)
        return result
    return wrapper


class VisitState:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.desired = YARD
        self.actual = YARD
        self.step = None  # the act's step, as the platform last reported it
        self.shoo = False  # the pacer reached the shoo point for this visit
        self.operator = None  # "gentle" or "normal" for the group in front
        self.operator_at = None
        self.visit = None  # {"id", "started"}; a long visit also gets "alarmed"
        self.visits = 0
        self.mismatch_since = None
        self.recent_lines = []  # (time, normalized line)
        self.repeats = []  # times of repeated lines
        self.blind = []  # times of empty looks
        self.last_restart = None
        self.blind_alarmed = None
        self.held_back = None  # the restart reason already alarmed while waiting
        self.events = []  # what the state did: corrections, restarts, alarms
        self.nudged = False
        # With the platform's reports wired in (the live server), a switch counts
        # as done only when the platform says so: a switch can be lost, as when
        # a goodbye's post_process switch never landed because the next arrival
        # interrupted it. Without them (the state replay), it counts at once.
        self.wait_for_reports = False
        self.pending = None  # (mode, when) of a switch sent but not yet reported
        self.keep_events = 500  # all night, keep the recent ones (None: keep all, for a replay)
        self._depth = 0
        self._to_fire = []
        # Told about each restart and alarm: (kind, why). The server sends the
        # restart to the page and the alarm to the operator.
        self.on_event = None

    # ------------------------------------------------------------ the record
    def _log(self, kind, **fields):
        self.events.append({"t": self.clock(), "kind": kind, **fields})
        if self.keep_events:
            del self.events[:-self.keep_events]
        if kind in ("restart", "alarm", "nudge"):
            self._to_fire.append((kind, fields["why"]))

    def _want(self, mode, why):
        if mode != self.desired:
            self._log("desired", mode=mode, why=why)
        self.desired = mode
        self._check_match()

    def _switched(self, mode, why):
        """A tool result carries a switch to `mode`."""
        if self.wait_for_reports and mode != self.actual:
            self.pending = (mode, self.clock())
        else:
            self._set_actual(mode, why)

    def _set_actual(self, mode, why):
        if mode != self.actual:
            self.actual = mode
            self.step = FIRST_STEP[mode]
            self._log("actual", mode=mode, why=why)
        self._check_match()

    def _check_match(self):
        if self.actual == self.desired:
            self.mismatch_since = None
        elif self.mismatch_since is None:
            self.mismatch_since = self.clock()
            self.nudged = False

    # ------------------------------------------------------ what the server learns
    @locked
    def visit_started(self, child=False):
        """The stage says a group has stepped up and stayed."""
        now = self.clock()
        if self.operator and (self.operator_at is None or now - self.operator_at > OPERATOR_AHEAD_SECONDS):
            self.operator = None  # made for someone else
        self.visits += 1
        self.visit = {"id": self.visits, "started": now}
        self.shoo = False
        self.visit["checked"] = False
        if self.operator == "normal":
            self._want(ACT, "operator chose the act")
        elif self.operator == "gentle" or child:
            self._want(GENTLE, "operator chose gentle" if self.operator else "small child measured")
        elif self.actual == GENTLE:
            # Gentle until this group passes the age check: the camera can report
            # the same small child again as a "new" group (a picture change), and
            # on a test call that reset threw away the age check's verdict and put
            # a child in the act.
            self.visit["carried"] = True  # gentle only until this group's check
            self._want(GENTLE, "unchecked group, still gentle")
        else:
            # Nobody gets the act before the age check passes; until then the
            # pumpkin greets them from the yard, in a line that suits anyone.
            self._want(YARD, "unchecked group")

    @locked
    def visit_ended(self):
        """The stage says the group has gone (after its hold)."""
        self.visit = None
        self.operator = self.operator_at = None
        self.shoo = False
        self._want(YARD, "group left")

    @locked
    def child_joined(self):
        """A small child measured joining. Returns whether that changes the mode."""
        if self.visit and self.operator != "normal" and self.desired != GENTLE:
            self._want(GENTLE, "small child joined")
            return True
        return False

    @locked
    def operator_choice(self, mode):
        """The operator's Gentle / Normal switch, for the group in front, or
        for one arriving within OPERATOR_AHEAD_SECONDS."""
        self.operator, self.operator_at = mode, self.clock()
        if self.visit:
            self._want(GENTLE if mode == "gentle" else ACT, f"operator chose {mode}")

    @locked
    def age_check(self, passed, small_child=False):
        """The gate on notice_visitors, and the handler's own reading. Failing
        means gentle mode; passing is what lets an unchecked group have the act."""
        if not self.visit:
            return
        self.visit["checked"] = True
        if (not passed or small_child) and self.operator != "normal":
            self.visit["carried"] = False  # gentle for this group now, by its own check
            self._want(GENTLE, "age check")
        elif (passed and not small_child and self.operator != "gentle"
              and (self.desired != GENTLE or self.visit.get("carried"))):
            self._want(ACT, "age check passed")

    @locked
    def model_requested(self, mode, why):
        """A switch the model asked for with a tool (become_gentle, a leaving
        tool, notice_visitors, resume_act). Toward gentle during a visit it's
        granted (the ratchet), unless the operator chose the act. Anything else
        is the server's call: leaving needs the stage to say they've gone, and
        the act needs a group that's passed the checks or the operator's say-so.
        Returns the mode the tool's result switches to."""
        if mode == GENTLE and self.visit and self.operator != "normal":
            self._want(GENTLE, why)
        elif mode != self.desired:
            self._log("refused", asked=mode, why=why, kept=self.desired)
        self._switched(self.desired, why)
        return self.desired

    @locked
    def observed(self, mode=None, step=None):
        """What the platform reports (its context and step changes): the truth
        about where the pumpkin is."""
        if mode:
            self.pending = None
            self._set_actual(mode, "platform")
        if step:
            self.step = step

    @locked
    def shoo_point(self):
        """The pacer says this visit has stayed long enough."""
        if self.visit:
            self.shoo = True

    @locked
    def snapshot(self):
        """(visit, desired, actual) together, for a caller outside the lock
        that needs more than one of them read as of the same moment."""
        return self.visit, self.desired, self.actual

    @locked
    def label(self):
        """"idle" (nobody here), "gentle", or "normal": for the remote and the
        operator's switch."""
        if not self.visit:
            return "idle"
        return "gentle" if self.desired == GENTLE else "normal"

    # --------------------------------------------- keeping the model's machine in step
    @locked
    def correction(self, tool):
        """Called at the start of every tool request. Returns where the tool's
        result must switch the pumpkin ({"mode", "step", "frm"}), or None when
        it's where it should be."""
        # A switch already on its way is sent again rather than skipped: on a
        # test call, an act tool ran during a pending switch to gentle mode and
        # mocked a child's costume.
        if self.actual != self.desired:
            frm = self.actual
            self._log("correction", tool=tool, frm=frm, to=self.desired)
            self._switched(self.desired, f"corrected on {tool}")
            step = FIRST_STEP[self.desired]
            if self.desired == ACT and frm == GENTLE:
                step = "banter"  # the operator brought the act back mid-visit
            if self.desired == ACT and self.shoo:
                step = "shoo"
            self.step = step
            return {"mode": self.desired, "step": step, "frm": frm}
        if self.actual == ACT and self.shoo and self.step != "shoo":
            self._log("correction", tool=tool, frm=self.step, to="shoo")
            self.step = "shoo"
            return {"mode": ACT, "step": "shoo", "frm": ACT}
        return None

    @locked
    def assistant_line(self, text):
        now = self.clock()
        line = normalize(text)
        if len(line) > 12 and any(difflib.SequenceMatcher(None, line, old).ratio() >= REPEAT_SIMILARITY
                                  for _, old in self.recent_lines):
            self.repeats = [t for t in self.repeats if now - t <= REPEAT_WINDOW_SECONDS] + [now]
            if len(self.repeats) >= REPEATS_TO_RESTART:
                self._restart("repeating itself")
        self.recent_lines = (self.recent_lines + [(now, line)])[-RECENT_LINES:]

    @locked
    def empty_look(self):
        now = self.clock()
        self.blind = [t for t in self.blind if now - t <= BLIND_WINDOW_SECONDS] + [now]
        if len(self.blind) >= BLIND_ALARM_COUNT and (
                self.blind_alarmed is None or now - self.blind_alarmed > BLIND_WINDOW_SECONDS):
            self.blind_alarmed = now
            self._log("alarm", why=f"{len(self.blind)} empty looks in {BLIND_WINDOW_SECONDS // 60} minutes")

    @locked
    def tick(self):
        """Time-based checks; call every few seconds."""
        now = self.clock()
        if self.mismatch_since is not None and now - self.mismatch_since > STUCK_SECONDS:
            self._restart(f"stuck in {self.actual}, should be {self.desired}")
        elif self.mismatch_since is not None and now - self.mismatch_since > NUDGE_SECONDS and not self.nudged:
            self.nudged = True
            self._log("nudge", why=f"in {self.actual}, should be {self.desired}")
        elif (self.visit and not self.visit["checked"] and not self.visit.get("nudged")
              and now - self.visit["started"] > NUDGE_SECONDS):
            # Nobody has looked at this group yet, so nobody gets the act.
            self.visit["nudged"] = True
            self._log("nudge", why="group not checked yet")
        elif self.visit and now - self.visit["started"] > MODE_HELD_SECONDS and not self.visit.get("alarmed"):
            # The server agrees with the pumpkin, so a restart wouldn't help;
            # either someone is lingering or the stage missed them leaving.
            self.visit["alarmed"] = True
            self._log("alarm", why=f"one visit has lasted {MODE_HELD_SECONDS // 60} minutes")

    @locked
    def new_call(self):
        """A call began. Every call starts in the yard; the next tool call
        corrects it if a group is already there."""
        self.recent_lines, self.repeats = [], []
        self._set_actual(YARD, "new call")

    @locked
    def call_restarted(self):
        """For the replay: a restart lands the pumpkin where the server says
        (on a live call, the page's re-announcement brings the tool call that
        does it)."""
        self.recent_lines, self.repeats = [], []
        self._set_actual(self.desired, "new call")

    def _restart(self, why):
        now = self.clock()
        if self.last_restart is not None and now - self.last_restart < RESTART_GAP_SECONDS:
            if self.held_back != why:
                self.held_back = why
                self._log("alarm", why=f"would restart ({why}), but restarted {int(now - self.last_restart)}s ago")
            return
        self.last_restart, self.held_back = now, None
        self._log("restart", why=why)
        # The mismatch clock starts over: the new call gets its own 30 seconds.
        self.mismatch_since = None if self.actual == self.desired else now
