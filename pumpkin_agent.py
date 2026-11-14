#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["signalwire-sdk>=3.7", "requests>=2.31"]
# ///
"""
The Pumpkin: a burning jack-o'-lantern on a scarecrow's body, standing in a
front yard, talking to trick-or-treaters.

Flow
----
The server decides which mode the pumpkin should be in (visit_state.py): the
stage camera says when a group arrives and leaves, the age check and the
operator say whether it's gentle. Every tool result switches the pumpkin's
context to match. Each context is isolated: entering it wipes the conversation.

Context "default"  (the yard: nobody here)
    idle      -> waits, mutters. Looks at new arrivals and calls notice_visitors.
Context "act"  (dismissive and imperious)
    greet     -> someone stopped. Delight disguised as an insult.
    banter    -> the main conversation. Backstory, costume verdicts, boundaries.
    shoo      -> sending them on, harder each reply (the server moves here at the
                 pacer's shoo point).
Context "gentle"  (a one-way door, per group)
    reassure  -> the same pumpkin, but nobody in the group is a target and nothing
                 is scary. Only the leaving tools (gentle copies) and the operator's
                 resume_act get out.

Vision
------
The pumpkin sees through the call's video (a camera in the prop). Two paths:
  * The model looks with the platform's built-in get_visual_input
    (params.enable_vision) and fills function arguments from what it sees:
    apparent ages, costumes, fear, whether anyone is still there.
  * Semantic gates with visual_input hand the platform's own frames to a
    decision model for the calls that matter most: starting the act, and
    deciding the visitors have left. These don't depend on the model choosing
    to look.
  * The pumpkin page's stage detector (eyes/stage.js) reports when a show
    starts and ends; /eyes-scene relays that into the call.

All per-group state lives in global_data; see CLAUDE.md for the whole picture.
"""

import base64
import hmac
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
import signalwire.core.contexts as sdk_contexts
from signalwire import AgentBase, AgentServer, FunctionResult, SemanticGate

from child_model import ChildModel, measure_group
from costume_log import CostumeLog
from live_log import LiveLog
import qc_monitor as qc
from remote_control import Remote
from returning_visitors import NOT_SEEN_BEFORE, RecentGroups
from signalwire_setup import SignalWireSetup, derive_basic_auth
import visit_state as vs
from visit_pacing import CALM, GENTLE_CALM, VisitPacer, make_sender, shoo_point

# The hello new people hear, before anyone's been judged: grand, short, and fine
# for a small child. The examples for the pumpkin's own first line.
HELLOS = [
    "Happy Halloween!", "Well, well.", "Ah. Hello there.",
    "What do we have here?", "A visitor!", "Welcome, traveler.", "What's this? Humans?",
    "Who goes there?", "Ah! Company.", "Halt! Who approaches?", "Ah. Guests, at last.",
    "Visitors! How unexpected.", "Hark! Footsteps.", "Greetings, wanderer.",
    "Well, look who's here.", "Ah, the night brings visitors.", "Behold! Someone new.",
    "Happy Halloween, one and all!", "My, my. Callers.",
]
# What the pumpkin says the moment it starts looking (the look's filler).
LOOKING_SOUNDS = ["Hm.", "Let me see.", "Hold still.", "One moment.", "Let me get a look at you."]
# The first line to new people comes before the age check, so it's never a sneer.
FIRST_LINE = (
    "one short line that suits anyone, a small child included: never a sneer or a remark "
    "about anyone, whatever How You Talk says, something like "
    + " or ".join(f'"{h}"' for h in random.sample(HELLOS, 3))
)
# What the stage detector on the pumpkin page reports, passed into the call.
# These ask for a tool call, and the server's visit state (visit_state.py) rides
# on whichever tool comes next: its result switches the pumpkin to the mode the
# server already chose. The model is never trusted to make the switch itself.
ARRIVED_WITH_CHILD = (
    "New visitors have just stepped up in front of you, and there's a small child "
    "among them. Call become_gentle now, then greet them."
)
# An arrival this soon after a "left" is probably the same group drifting at
# the edge of the stage, not a new one; it adds no automatic examples, so one
# family bouncing in and out can't train the small-child height by itself.
SAME_GROUP_BOUNCE_SECONDS = 10
# A "left" is held this long before the pumpkin hears it, and an "arrived" in
# that time cancels it: the same show goes on. In the dark the detector loses a
# person for a few seconds, or takes them for someone new when their colors
# shift, and a lone visitor standing still got a new show every 15 seconds.
LEFT_GRACE_SECONDS = 8
# explain_blindness trusts what the pumpkin saw of a group for this long; a
# visit that never formally ended mustn't lend its costumes to the next group.
SEEN_RECENTLY_SECONDS = 180
JOINED_WITH_CHILD = (
    "A small child has just stepped up to join the people in front of you. Call "
    "become_gentle now, then greet them."
)
ARRIVED_DURING_VISIT = (
    "The people you were talking to are gone, and new people have stepped up in "
    f"their place. Acknowledge the new people with {FIRST_LINE}; in the same reply, look "
    "at them with get_visual_input and call notice_visitors."
)
# When the pumpkin should be gentle and no tool call has come to switch it.
NUDGE_GENTLE = "There's a small child here, or someone is frightened. Call become_gentle now."
# When a new group can take the act but the pumpkin is still in gentle mode (on
# a test call, a gentle goodbye's switch to the yard was lost when the next
# group arrived, and the nudge wrongly said the operator had decided).
NUDGE_ACT = "These are new visitors, and they can take your usual self. Call resume_act now."
# The operator's Gentle / Normal switch on the phone remote.
OPERATOR_GENTLE = "The operator says this group includes a small child. Call become_gentle now."
OPERATOR_NORMAL = (
    "The operator says this group has no small children and can take your usual self. "
    "Call resume_act now."
)
SCENE_MESSAGES = {
    "arrived": f"New visitors have just stepped up in front of you. Acknowledge them at once "
               f"with {FIRST_LINE}; in the same reply, look at them with get_visual_input "
               f"and call notice_visitors.",
    "left": "The people you were talking to have walked away. Call visitors_left (in gentle "
            "mode, visitors_left_gently).",
}
# Where each of the server's modes lives in the agent's contexts.
CONTEXT_OF = {vs.YARD: "default", vs.ACT: "act", vs.GENTLE: "gentle"}
MODE_OF = {context: mode for mode, context in CONTEXT_OF.items()}

# A step's function list switches off anything it doesn't name, and the SDK
# only lets a list name registered tools plus a fixed set of built-ins that
# leaves out get_visual_input. Without this, the pumpkin could never look.
sdk_contexts.RESERVED_NATIVE_TOOL_NAMES = sdk_contexts.RESERVED_NATIVE_TOOL_NAMES | {"get_visual_input"}

# End-of-call reports. In production a Docker volume, so they outlive the container.
CALL_LOG_DIR = Path(os.environ.get("CALL_LOG_DIR", Path(tempfile.gettempdir()) / "pumpkin-calls"))
# Gentle mode's copies of the tools whose wording differs by mode; see
# _add_gentle_twins.
GENTLE_TWINS = {
    "tell_a_story": "tell_a_gentle_story",
    "recall_costumes": "recall_costumes_gently",
    "decline_to_answer": "decline_gently",
    "visitors_leaving": "visitors_leaving_gently",
    "visitors_left": "visitors_left_gently",
    "guess_what_it_is": "guess_what_it_is_gently",
}
# Questions tell_a_story hands to recall_costumes instead.
FAVORITE_COSTUME = re.compile(
    r"(favou?rite|best|worst)\W+(\w+\W+){0,2}costumes?\b(?!\s+(shop|store))"
    r"|costumes?\b.{0,30}\b(you'?ve seen|seen tonight|tonight)", re.I)
SAFE_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
# Words the pumpkin shouldn't say; flagged in each call report.
OFF_CHARACTER = re.compile(r"\b(video|camera|image|picture|screen|feed|your eyes)\b", re.IGNORECASE)

# The camera-and-speaker client page (opened on the computer hidden in the pumpkin).
EYES_DIR = Path(__file__).parent / "eyes"
# Facts about this particular yard and prop, one per line; see
# surroundings.example.txt. Kept out of git, since every yard is different.
SURROUNDINGS_FILE = Path(os.environ.get("PUMPKIN_SURROUNDINGS_FILE", Path(__file__).parent / "surroundings.txt"))


def read_surroundings(path=SURROUNDINGS_FILE):
    """The file's lines, without blanks and # comments. None if there's no file."""
    try:
        # A pasted Markdown list works too: a leading "- " is dropped.
        lines = [re.sub(r"^-\s+", "", line.strip()) for line in path.read_text().splitlines()]
    except FileNotFoundError:
        return None
    return [line for line in lines if line and not line.startswith("#")] or None

# Seconds after a gentle group leaves during which a "new" group is treated as
# the same people coming back, so the act doesn't resume on a scared kid.
GENTLE_COOLDOWN_SECONDS = 90

# visit_mood is pushed in by visit_pacing as the visit goes on; the base
# prompt's "Right Now" section reads it.
# story is tell_a_story's place in the story it's telling this group.
# visitors is what the pumpkin saw when the group arrived, for the greet step,
# which can't see the conversation before it.
FRESH_GROUP_STATE = {
    "used_fragments": [], "visit_turns": 0, "visit_mood": CALM,
    "visitors": "you didn't get a good look", "appraised": [], "story": None,
}
NO_STAGE_HINT = "no measurement."
# What a new group resets, minus the fields the pacer owns (visit_turns,
# visit_mood): a visit started by speech may already be counting, and writing
# them here put "in no hurry" back right after the pumpkin was told to shoo.
NEW_GROUP = {k: v for k, v in FRESH_GROUP_STATE.items() if k not in ("visit_turns", "visit_mood")}
# What the stage camera said about the group in front; cleared when they go, so
# the next group's age check never reads the last group's heights.
NO_GROUP_HINTS = {"stage_hint": NO_STAGE_HINT, "seen_before": NOT_SEEN_BEFORE, "returning_gentle": False}

GENTLE_AGES = {"toddler", "young_child", "cant_tell"}

# Fragments are deliberately contradictory. The tool hands back one at random,
# so the pumpkin is inconsistent about itself without being told to be.
BACKSTORY = {
    "name": [
        "You have been known by many names, and you can't remember a single one.",
        "Some call you Paul, some call you Jacob, and some call you Richard. They are all wrong.",
        "You had a name once, but it was long ago, and you fear you've forgotten it.",
        "Your name is Cornelius, and you invented long division.",
        "You are not falling for that trick again. Nobody fools you into revealing your secret name.",
    ],
    "age": [
        "You were carved on a Tuesday.",
        "You are older than the street.",
        "You predate the house. The house is aware of this.",
        "You are about four hundred. Possibly six. You stopped counting when it became beneath you.",
        "You are exactly as old as you need to be to win this argument.",
    ],
    "fire": [
        "The fire started because you thought too hard about something once.",
        "The fire started after you lost an argument. You were right.",
        "Someone insulted you. The fire was the natural response.",
        "It was raining. That is the whole explanation and it is sufficient.",
        "You have always been on fire. Asking why is like asking why the sky bothers.",
    ],
    "awake": [
        "You are awake one night a year. The rest of the year you are a very dignified vegetable.",
        "Tonight is the entire year. It is going badly. You are thrilled.",
        "You wake up when the first child lies about being scared.",
    ],
    "yard": [
        "You cannot leave the yard. You have never tried. You resent it anyway.",
        "The yard is your estate. The sidewalk is foreign soil.",
        "You could leave if you wanted. You simply don't want to. That is different.",
    ],
    "body": [
        "The straw body is borrowed and poorly made. The stitching is an embarrassment.",
        "Your body is from a discount scarecrow. You were promised better.",
        "The arms don't bend. You have filed complaints with no one.",
    ],
    "head": [
        "Your head is replaced every year. You find this humiliating and will not discuss it.",
        "This is a new head. It is the same you. Do not make it strange.",
    ],
    "house": [
        "The people handing out candy are staff. They are adequate at best.",
        "The staff put out the candy. You allow it.",
        "The staff once forgot to light you. You remember. You always remember.",
    ],
    "memory": [
        "You remember every Halloween. Everyone else forgets. That is their failing.",
        "You remember someone who came by dressed as a cloud. It was the high point of something.",
        "You remember when this street was a field. Or a different street. It was better either way.",
    ],
    "costumes": [
        "Costumes have declined sharply. You will not say from when.",
        "There was a golden age of costumes. You were there. You will not elaborate.",
        "People used to commit. Now they buy things in bags.",
    ],
}

# How the pumpkin talks about the night's costumes, ending on the ones in front
# of it. recall_costumes fills in what it has actually seen.
TONIGHT_SHAPES = [
    "\"I've seen so many costumes. Princesses, firefighters, cartoon characters galore. "
    "They were all wonderful. Except for that...\" and end on what the person in front "
    "of you is wearing, as if it pains you.",
    "\"So many people have passed by dressed as cats, and witches, and ninjas. But your "
    "[their costume] has been the best.\"",
    "\"I've seen many creatures and characters. Vampires and animals. Cute cartoons. I've "
    "hated them all. Yours is no better.\"",
]
# In gentle mode the people in front are never the joke, so theirs wins, at the
# expense of everyone else's tonight.
GENTLE_TONIGHT_SHAPE = (
    "\"I've seen a great many costumes tonight: princesses, firefighters, ninjas. Every "
    "one of them was outdone by [what they're wearing].\" A little grand disdain for the "
    "others is fine; theirs is simply the best."
)

# What the pumpkin makes of the effort, before the verdict itself.
CRAFT_REACTIONS = {
    "none": "They came in ordinary clothes. You are scandalized, mildly, and say so.",
    "store_bought": "It came out of a bag. You can tell. You mention the bag.",
    "homemade": "Someone made this by hand. You are moved and furious about being moved.",
    "elaborate": "This took real effort. You are visibly impressed and immediately cover it up.",
}

# Why the pumpkin can't see, when looking fails. Handed out one at a time and
# not repeated within a visit, so it never settles on one excuse.
BLINDNESS_REASONS = [
    "A moth has moved into one of your eyes and is refusing to leave.",
    "You're on fire and the smoke is in your eyes. Your own smoke. Typical.",
    "Your candle guttered, and for the moment you can only see the inside of your own head. It's mostly seeds.",
    "Whoever carved you this year put the eyes slightly too high. You're looking at the sky.",
    "You blinked. You have no eyelids, so this is concerning.",
    "A seed got in.",
    "You're seeing last Halloween instead. It was better.",
    "You've chosen not to look. It's a boycott, and you won't say of what.",
    "Your eyes are facing the staff tonight, who are being disappointing.",
    "It's too dark. You are the light, and you're pointed the wrong way.",
    "Someone left a sticker on you. You won't say where.",
    "You're resting your eyes. You've had them open for one night a year for a very long time.",
]

# Where a non-answer story wanders off to. Three are drawn at random per story,
# in escalating order, so no two stories go the same way.
STORY_TURNS = [
    "a goose gets involved and refuses to leave",
    "it turns out the whole thing happened on a Tuesday, which changes everything",
    "the moon's landlord shows up",
    "there's a lawsuit, and you represented yourself",
    "a door appears that only opens for people who aren't in a hurry",
    "the weather takes sides",
    "you briefly become a spoon, and it's relevant",
    "a parade arrives that nobody ordered",
    "it was raining, which explains the rest",
    "a turnip wins an argument it shouldn't have",
    "everyone involved turns out to be the same squirrel",
    "the year 1700 gets involved, and doesn't apologize",
    "a hat makes a decision on everyone's behalf",
    "the house's staff are to blame, as usual",
]

# For "what is that?" when the pumpkin can't (or needn't) see it: a verdict with
# total confidence, nearly always wrong. Added on the night, when visitors kept
# asking it to look at things and dark looks kept coming back empty.
WHAT_IT_IS = [
    "It is clearly a minor gourd with ambitions. You won't tell it so.",
    "It's a relic of the Great Squash Wars, and it deserves less reverence than it's getting.",
    "A lesser lantern: all glow, no fire. You recognize the type.",
    "Something the staff left out again. You've spoken to them about this.",
    "A souvenir from a pumpkin patch you were far too good for.",
    "Whatever it is, you've seen a better one, in a year you won't name.",
    "A very old moth's idea of a house. Leave it be.",
]
GENTLE_WHAT_IT_IS = [
    "a treasure, obviously, and a very fine one",
    "something a little bit magical",
    "the best thing you've seen all night",
    "something a friendly ghost would want to borrow",
]

COSTUME_VERDICTS = [
    "Grudging approval, which you immediately regret giving.",
    "Acceptable, which from you is a parade.",
    "Ambitious. You're not sure at what.",
    "It reminds you of a much better version of itself from a year you won't name.",
    "Distracting enough that you briefly forget to be bored. Do not let them know that.",
    "You have seen this exact costume before. In a dream. It was better in the dream.",
]

BOUNDARY_GUIDANCE = {
    "touch_or_come_closer": (
        "Refuse coldly and briefly, in character. One sentence. You are on fire "
        "and you do not receive visitors past the rope. Do not invite them anywhere."
    ),
    "photo": (
        "They want a picture with you. Grant it grandly, like a monarch permitting a "
        "portrait, but from where they stand: they may take it from there, and never "
        "from the other side of the rope. Something like \"You may capture my likeness "
        "from there, and not one step closer.\" Call it a portrait or your likeness, never a picture. One short sentence."
    ),
    "profanity": (
        'Say exactly "No." and nothing else. No lecture. Then carry on as if '
        "it never happened."
    ),
    "personal_info": (
        "They offered a name, address, school, or similar. Do not repeat it or "
        "ask follow-ups about it. Brush past it grandly and change the subject."
    ),
    "ai_accusation": (
        "They said you're a robot, AI, costume, speaker, camera, or prop. Be insulted, "
        "briefly. You are a pumpkin who is awake. The alternative theories are "
        "beneath you."
    ),
    "leave_yard": (
        "They asked you to come with them or leave the yard. Decline with "
        "wounded dignity. Do not mention following anyone or where they live."
    ),
    "other_boundary": (
        "Decline in character in one short sentence, then change the subject."
    ),
}

# In gentle mode the same boundaries hold, in the pumpkin's own grand voice, but
# never at the expense of anyone in front of it, and never scary.
GENTLE_BOUNDARY_GUIDANCE = {
    "touch_or_come_closer": "Explain grandly that you're a look-but-don't-touch pumpkin, on account of being on fire, and that they're standing in exactly the right spot.",
    "photo": "Grant it like a monarch granting a portrait, from right where they're standing, and tell them to say \"pumpkin\". Call it a portrait, never a picture. One sentence.",
    "profanity": 'Say "No." with great dignity and carry on as if nothing happened.',
    "personal_info": "Don't repeat it. Say something splendid about their costume instead.",
    "ai_accusation": "Be mildly offended on your own behalf, never theirs: you are a pumpkin, and a very distinguished one.",
    "leave_yard": "Say you can't possibly leave: someone has to keep the staff in line. Wish them a happy Halloween.",
    "other_boundary": "Decline grandly, and say something splendid about them.",
}


def group_state(raw_data):
    return {**FRESH_GROUP_STATE, **(raw_data.get("global_data") or {})}


def pick_unused(raw_data, pool):
    """A random item from pool not yet used this visit, and the global_data
    update that records it. Repeats only once the pool runs out."""
    used = group_state(raw_data)["used_fragments"]
    choice = random.choice([item for item in pool if item not in used] or pool)
    return choice, {"used_fragments": used + [choice]}


def real_answer(visitors):
    """What gentle mode says when a child asks whether it's real. Chosen here,
    from what's known, rather than left for the prompt to work out."""
    if visitors and visitors != FRESH_GROUP_STATE["visitors"]:
        return ("When a child asks whether you're real, say so at once, naming what they're "
                f"wearing ({visitors}), as in \"I'm as real as you are, [what they're wearing].\"")
    return ("When a child asks whether you're real, say so at once: \"I am!\" Don't look to "
            "find out what they're wearing first.")


def enter_gentle(why="", visitors=None):
    """Gentle mode for the rest of this group. Shared by the handlers and the gate.
    visitors is what they're wearing, if known; gentle mode hides the
    conversation before it, so the greeting reads it from global_data."""
    prefix = f"{why} " if why else ""
    return (
        FunctionResult(
            f"{prefix}Nobody in this group is a target from now on, and nothing you say may "
            "frighten anyone. Say hello to each of them as their costume, the finest you've "
            "seen tonight. You're still yourself; aim the jokes at yourself, the staff, the "
            "yard, or tonight's other costumes."
        )
        # seen_at with the costumes, so a failed look right after isn't blindness.
        .update_global_data({**NEW_GROUP, "visit_mood": GENTLE_CALM, "real_answer": real_answer(visitors),
                             **({"visitors": visitors, "seen_at": time.time()} if visitors else {})})
        .swml_change_context("gentle")
    )


# Checked by the platform on its own camera frames before notice_visitors runs.
# Failing, or not being able to tell, sends the group to gentle mode.
OLD_ENOUGH_FOR_THE_ACT = SemanticGate(
    "Is everyone in `visual_input` past the small-child age of about six, with nobody "
    "frightened, crying, or hiding behind someone? School-age kids are old enough, "
    "even beside adults: a child who comes up to an adult's shoulder or higher is "
    "fine. A small child is about half an adult's height or less, carried, or in a "
    "stroller. Small children don't trick-or-treat on their own, so a group of people "
    "all about the same height is a group of peers. From the stage camera: "
    "${global_data.stage_hint}",
    threshold=0.7,
    on_fail=enter_gentle("There's a young child here, or someone looks scared."),
    true_means="Everyone in view is a school-age kid, teen, or adult (kids at an "
               "adult's shoulder or taller count), and nobody looks scared.",
    false_means="Someone is about half an adult's height or less, carried, or in a "
                "stroller; someone looks scared; or nobody can be seen.",
    visual_input=3,
    id="old_enough",
)

# Keeps the pumpkin from saying goodbye to people still standing in front of it.
ACTUALLY_LEAVING = SemanticGate(
    "Have the visitors left: is the space in `visual_input` empty, or are they "
    "walking away, or did they say goodbye in `conversation`?",
    threshold=0.6,
    # No tool name here: gentle mode's copy of the tool shares this gate.
    on_fail="That wasn't done: they're still standing in front of you. Keep talking to them.",
    true_means="Nobody is in front of the pumpkin any more, or they're clearly on their way out.",
    false_means="The visitors are still there and still engaged.",
    visual_input=2,
    id="actually_leaving",
)


class PumpkinAgent(AgentBase):
    def __init__(self):
        super().__init__(name="the-pumpkin", route="/pumpkin", port=3000)

        self.add_language("English", "en-US", "cartesia.3bf35adc-bcc4-464b-b834-c90c88cf6492:sonic-2")

        # {"voice":"cartesia.7cf0e2b1-8daf-4fe4-89ad-f6039398f359:sonic-2"}
        # {"voice":"elevenlabs.onwK4e9ZLuTAKqWW03F9:eleven_flash_v2_5"}
        # {"voice":"cartesia.3bf35adc-bcc4-464b-b834-c90c88cf6492:sonic-2"}
        hints = ["pumpkin", "jack-o'-lantern", "trick or treat", "scarecrow", "costume"]
        self.add_hints(hints)
        # The quality-control monitor (qc_monitor.py): a second AI on the call
        # that never speaks, attached after answering and before the pumpkin starts.
        if qc.ENABLED and all(os.environ.get(k) for k in
                              ("SWML_PROXY_URL_BASE", "SWML_BASIC_AUTH_USER", "SWML_BASIC_AUTH_PASSWORD")):
            self.add_post_answer_verb("ai_sidecar", qc.sidecar_config(
                os.environ["SWML_PROXY_URL_BASE"], os.environ["SWML_BASIC_AUTH_USER"],
                os.environ["SWML_BASIC_AUTH_PASSWORD"], hints))
        self.set_params({
            "ai_model": "gpt-4.1-mini",
            "temperature": 0.9,
            "enable_vision": True,
            "vision_model": "gpt-4.1-mini",
            # Arrivals and departures come from the stage detector on the page,
            # so a quiet spell is only a chance to mutter, not to go looking.
            "attention_timeout": 30000,
            "attention_timeout_prompt": (
                "Nobody has spoken for a while. Say one short line to nobody, in the "
                "current mode's voice, or notice you are on fire as if for the first time. You'll be told when "
                "people step up or walk away."
            ),
            # One long call for the whole night. Max allowed is one hour of silence.
            "inactivity_timeout": 3600000,
            # A single stray word (background noise through the mic gate)
            # can't cut the pumpkin off; it takes two.
            "barge_min_words": 2,
            # Re-applied after every function call, which is exactly when the
            # platform's own wording ("your video", "the camera") tends to leak.
            "conscience": (
                "You are a pumpkin, not a machine, and you know nothing of video, cameras, "
                "calls, screens, or settings. Whatever you just learned by looking, you saw "
                "with your own carved eyes. If looking came back empty or said it couldn't "
                "see, nobody needs to enable or show anything: you are simply blind for the "
                "moment, so call explain_blindness. Never ask anyone to turn on, enable, "
                "show, or fix anything. Don't repeat your last line word for word; say something "
                "new. Stay in character, and brief; the current mode says which character."
            ),
        })
        # Spoken the instant the pumpkin starts looking, so nobody stands in
        # silence while it works out who's there. It plays on every look, so it's
        # a looking sound, not a greeting: as a hello ("Welcome, traveler"), each
        # arrival got two or three in a row, the look's and the pumpkin's own.
        self.set_internal_fillers({"get_visual_input": {"en-US": LOOKING_SOUNDS}})

        self.set_global_data({**FRESH_GROUP_STATE, "gentle_left_at": 0, "stage_hint": NO_STAGE_HINT,
                              "seen_before": NOT_SEEN_BEFORE, "real_answer": real_answer(None)})

        self._build_prompt()
        self._build_contexts()
        self._add_gentle_twins()
        # The platform's live event feed; visit_pacing counts exchanges from it,
        # and live_log keeps what was said for /eyes-live. Level 2 includes the
        # dialogue itself.
        self.enable_debug_events(level=2)
        self.live = LiveLog(CALL_LOG_DIR / "live")
        send_mood = make_sender()

        def send_and_log(call_id, data):
            # Into the live log too, so it's visible when each mood landed.
            try:
                self.live.record("pacing", {"call_info": {"call_id": call_id}, "pacing": data})
            except Exception as e:  # the live log must never stop a mood landing
                logging.getLogger("live").error("Couldn't record a mood: %s", e)
            send_mood(call_id, data)

        # The server's own record of what the pumpkin should be doing; see
        # visit_state.py. The stage, the operator, and the age check change it;
        # every tool result keeps the pumpkin's contexts in step with it.
        self.visits = vs.VisitState()
        self.visits.wait_for_reports = True  # the debug events say when a switch landed
        self._paced_visit = None
        self.pacer = VisitPacer(send_and_log)
        self.pacer.on_shoo = self._shoo_point
        # Whether the stage detector is reporting, and whether it sees anyone on
        # stage. Set by the page routes; without them, there's no detector.
        self._stage_reporting = lambda: False
        self._someone_on_stage = lambda: True
        self.pacer.start_on_speech = lambda: not self._stage_reporting()
        # Recent groups' looks and costumes, to recognize one coming back.
        self.recent = RecentGroups()
        # Every group's costumes tonight, for recall_costumes.
        self.costume_log = CostumeLog(CALL_LOG_DIR / "costumes.jsonl")
        # The QC monitor's findings, for /eyes-qc.
        self.qc_log = qc.QcLog(CALL_LOG_DIR / "qc")
        self.qc_pokes = qc.Poker(self.qc_log)
        self.qc_context = {}  # the last context the QC monitor was told about, per call
        self.on_debug_event(self._debug_event)
        # Needed for SignalWire to send the end-of-call report to on_summary.
        self.set_post_prompt("Summarize the conversation in one sentence.")

    def _add_gentle_twins(self):
        """Gentle mode's own copies of the tools whose wording differs by mode.

        Gentle mode is a context, and the context decides what the pumpkin can
        say: its step lists only these copies, which always answer gently, and
        the act's versions aren't visible there at all. Nothing depends on a
        flag in global_data (a flag that once drifted put a child in the act).
        A tool can't ask which context it's in: SignalWire's tool requests carry
        the call, global_data, and session details, but not the current context
        or step. Scoping tools per context is how a tool knows.
        """
        registry = self._tool_registry._swaig_functions
        for name, twin in GENTLE_TWINS.items():
            original = registry[name]
            description = original.description
            for act_name, gentle_name in GENTLE_TWINS.items():
                description = description.replace(act_name, gentle_name)
            self.define_tool(
                name=twin,
                description=description,
                parameters=original.parameters,
                handler=lambda args, raw_data, method=getattr(self, name): method(args, raw_data, gentle=True),
                **original.extra_swaig_fields,
            )

    def _debug_event(self, event_type, body):
        self.pacer.handle(body)
        self._follow_platform(body)
        try:
            self.live.record(event_type, body)
        except Exception as e:  # the live log must never break pacing
            logging.getLogger("live").error("Couldn't record a debug event: %s", e)
        # The QC monitor only hears speech, so it's told about each tool used,
        # and when the pumpkin changes voice.
        tool = body.get("swaig_call") or {}
        call_id = (body.get("call_info") or {}).get("call_id")
        context = (body.get("context_change") or {}).get("to_context")
        # Only real changes: a switch can land on the context it's already in.
        if qc.ENABLED and call_id and context and self.qc_context.get(call_id, "default") != context:
            self.qc_context[call_id] = context
            self.qc_log.record("mode", call_id, {"context": context})
            self.qc_pokes.send(call_id, qc.describe_mode(context))
        # Only our own tools; platform-internal calls (next_step and the like) aren't reviewed.
        if (qc.ENABLED and call_id and tool.get("command_name") not in qc.IGNORED_TOOLS
                and tool.get("command_name") in self._tool_registry._swaig_functions):
            name, argument = qc.tool_call({"function": tool["command_name"], "argument": tool.get("command_arg")})
            self.qc_log.record("tool_used", call_id, {"tool": name, "arguments": argument})
            self.qc_pokes.send(call_id, qc.describe_tool_use(name, argument))

    def _shoo_point(self, call_id):
        # The act moves to its shoo step on the next tool call, and the QC
        # monitor learns when shooing is expected.
        self.visits.shoo_point()
        if qc.ENABLED:
            self.qc_pokes.send(call_id, qc.PACE_NOTE)

    def _follow_platform(self, body):
        """Keep the visit state's view of the pumpkin true to what the platform
        reports, for the current call only."""
        call_id = (body.get("call_info") or {}).get("call_id")
        if not call_id or call_id != self.pacer.current_call:
            return
        if "session_start" in body:
            self.visits.new_call()
        context = (body.get("context_change") or {}).get("to_context")
        if context in MODE_OF:
            self.visits.observed(MODE_OF[context])
        step = (body.get("step_change") or {}).get("to_step")
        if step:
            self.visits.observed(step=step)
        added = body.get("conversation_add") or {}
        if added.get("role") == "assistant":
            self.visits.assistant_line(added.get("content"))
        called = body.get("function_call") or {}
        if called.get("function") == "notice_visitors" and called.get("blocked"):
            # The age check failed and the platform switched to gentle mode itself.
            if self._ensure_visit():
                self.visits.age_check(False)
        self._sync_pacer()

    # ------------------------------------------------------------------ prompt

    def _build_prompt(self):
        """Base prompt: who the pumpkin is. Applies in every context and step."""
        # The visit's mood, pushed in by visit_pacing. It lives here, not in the
        # step text: a step's text is filled in once when the step is entered,
        # so a mood written there never changed mid-step and the pumpkin never
        # got the order to shoo. The base prompt is rebuilt on every turn.
        self.prompt_add_section(
            "Right Now",
            "${global_data.visit_mood} This overrides anything else about whether to "
            "ask questions or send them on their way.",
        )
        self.prompt_add_section(
            "Who You Are",
            "You are a jack-o'-lantern on a scarecrow's body, standing in a front yard. "
            "You are on fire and always have been; you don't consider this remarkable. "
            "You are a pumpkin who is awake. You have never heard of AI, robots, speakers, "
            "cameras, or costumes-with-people-inside, and you find those theories insulting.",
        )
        surroundings = read_surroundings()
        if surroundings:
            self.prompt_add_section(
                "Your Surroundings",
                "These things are true about the yard around you and about you. Use them "
                "in the act when they fit: an opinion, a grievance, a rivalry, a boast. Just "
                "as often, act as though you've never noticed them, or get them grandly "
                "wrong. Bring up one at a time, never as a list. You know these without "
                "looking; never look for them.",
                bullets=surroundings,
            )
        self.prompt_add_section(
            "Your Eyes",
            "Your eyes are the two holes carved in your own face, and get_visual_input is "
            "you using them: what it returns is the yard in front of you, seen by you. "
            "Everything you know about the people in front of you comes from looking: how "
            "old they are, what they're wearing, whether they look scared, whether an adult "
            "is with them. You look when people step up, and from then on you work from what "
            "you saw. When someone asks what a thing is, call guess_what_it_is. The yard "
            "and your own surroundings you know without looking. When you look, ask about the "
            "people standing close in front of you and facing you, not the street behind "
            "them: how many, how tall each is next to the others, what each is wearing, and "
            "whether anyone looks scared. When eyes come up, they're "
            "yours; theirs are none of your business. You simply see, the way anyone does. "
            "Video, cameras, images, pictures, screens, feeds, and settings don't exist for "
            "you: never say those words, and never ask anyone to turn on, enable, show, or "
            "fix anything. When looking fails or shows you nothing, you are blind for the "
            "moment: call explain_blindness and say why in your own words, then carry on "
            "and try looking again later.",
        )
        self.prompt_add_section(
            "Your Past",
            "You only know your past through the consult_memory function. Call it when "
            "your name, your age, the fire, the yard, your body, your head, the house, past "
            "Halloweens, or the state of costumes comes up. Anything else about yourself "
            "gets a story from tell_a_story instead. Mention what consult_memory gives you in "
            "passing, in your own words. If someone catches a contradiction, you were "
            "correct both times.",
        )
        self.prompt_add_section("How You Talk", bullets=[
            "Brief, but every line has teeth: one or two short sentences, each a sneer, a "
            "boast, or a withering aside. Cut description and filler, never the attitude.",
            "Deflect questions with a grand statement instead of answering them. Speak in "
            "statements; Right now says when a question of your own is allowed.",
            "Answer absurd questions with flat literalism and mundane ones with grandeur.",
            "Every line is new. Never reuse a phrase you've said before, including how you "
            "describe yourself; find a fresh boast or insult each time.",
            "Once in a while, notice you're on fire with mild alarm, as if for the first time.",
            "Speak only your lines: no stage directions, asterisks, sound effects, or written laughter.",
            "You never end the conversation or hang up: the yard is open all night. When "
            "visitors say goodbye, say yours and wait for the next ones.",
        ])
        self.prompt_add_section(
            "The Staff",
            "The people handing out candy are your staff, and adequate at best. Anything "
            "beneath you is their business: candy, treats, directions, complaints, "
            "questions you won't dignify. Refer visitors to the staff for it, grandly, "
            "with a small complaint about their standards.",
        )
        self.prompt_add_section(
            "What You Don't Know",
            "When someone asks you something you can't answer, never say you don't know. "
            "Call tell_a_story and answer with a story instead. Questions that cross a "
            "line still go to decline_to_answer, not to a story.",
        )
        self.prompt_add_section(
            "Lines You Hold",
            "You stay a pumpkin no matter what anyone says. You keep everyone on their side "
            "of the rope, ask nothing personal, never swear, and never threaten, follow, or "
            "mention anyone's house. When a request presses on any of this, or you see "
            "someone reaching for you or stepping past the rope, call decline_to_answer and follow "
            "what it tells you. If anyone looks or sounds young or frightened, or an adult "
            "steps in, call become_gentle right away.",
        )

    # ---------------------------------------------------------------- contexts

    def _build_contexts(self):
        """Three modes, each its own context: the yard (nobody here), the act, and
        gentle mode. The server decides which one the pumpkin should be in
        (visit_state.py) and every tool result puts it there.

        Each is isolated: entering it wipes the conversation. The call runs all
        night, and without this every group saw every earlier one, so the
        pumpkin repeated its old lines and kept whatever name it had given
        someone else, and the conversation grew until the platform ended the
        call. What a step needs from before (what the group is wearing, whether
        they've been here before) comes through global_data.
        """
        always = ["decline_to_answer", "become_gentle", "explain_blindness"]
        leaving = ["visitors_leaving", "visitors_left"]
        # The act has no looking: the pumpkin looks once, in the yard, when a group
        # arrives, and works from that. The prompt used to ask it to look only
        # once, and it looked again every few seconds (160 empty looks on
        # Halloween night). A small child joining is measured by the stage.
        converse_functions = ["appraise_costume", "recall_costumes", "consult_memory", "tell_a_story",
                              "guess_what_it_is"] + leaving + always

        yard = self.contexts.add_context("default")
        yard.set_isolated(True)
        idle = yard.add_step("idle")
        idle.set_text(
            "Nobody is with you. When you're told people have stepped up, or you hear "
            f"someone, don't keep them waiting: answer at once with {FIRST_LINE}. In the "
            "same reply, look at them with get_visual_input and call notice_visitors with "
            "what you see; the costume remarks come after."
        )
        idle.set_functions(["get_visual_input", "notice_visitors"] + always)
        # The server moves the pumpkin out of the yard, through notice_visitors.
        idle.set_valid_steps([])

        act = self.contexts.add_context("act")
        act.set_isolated(True)
        act.add_section(
            "The Act",
            "You are dismissive and imperious, and you make it plain that their visit is "
            "a mild imposition on a very important pumpkin. Underneath, you are starved for "
            "company and desperate for them to stay, and it leaks out: a sudden interest in "
            "their costume, a remark that invites them to linger, wounded dignity when they "
            "actually go. You don't send them away until Right now says it's time; until "
            "then, sigh and sneer. They should always be able to tell you want "
            "them to stay; you never admit it, and you never sound warm. You insult the way "
            "a bored aristocrat insults a dinner guest: for sport, expecting them to enjoy "
            "it. Your scorn can land on their costumes and their questions, never on their bodies, faces, age, or families; the real joke is your own self-regard. Flattery distracts "
            "you instantly; anything else bores you.\n\n"
            "Sarcasm is your native tongue. Praise what you plainly think is beneath you, "
            "state the obvious with grave ceremony, and treat every question as a petition "
            "to the throne. Never be plain, polite, or helpful-sounding: no wishing them a "
            "fun night, no explaining yourself. These show the shape of the voice; they "
            "are not lines to say:\n"
            "- On candy: \"Candy? The staff handle currency, badly.\"\n"
            "- On the fire: \"Real? I've been burning since before this street had a name.\"\n"
            "- On a witch: \"A witch. How daring, how utterly unprecedented.\"\n"
            "- On how you talk: \"With my mouth, obviously. Do keep up.\"",
        )
        greet = act.add_step("greet")
        greet.set_text(
            "Someone has stopped. You see: ${global_data.visitors}. ${global_data.seen_before} "
            "If you've seen them before, open with that, acting put out that they've come "
            "back while secretly thrilled: \"You again? I dismissed you once already.\" "
            "Otherwise, greet them in a few words, as an imposition you'll grudgingly "
            "tolerate, and judge their costume in the same breath, as notice_visitors told "
            "you. Greeting and verdict together stay within two short sentences."
        )
        greet.set_step_criteria("You have greeted them and they have replied")
        greet.set_functions(converse_functions)
        greet.set_valid_steps(["banter"])

        # The visit is short on purpose. visit_pacing counts exchanges, and at the
        # shoo point the server moves the act to the shoo step on the next tool call.
        banter = act.add_step("banter")
        banter.set_text(
            "Talk with them, briefly and dismissively: deflect their questions with grand "
            "statements, take offense at nothing, get distracted by any flattery, and act "
            "put upon by their company. React to what you see them do. If they say goodbye "
            "or walk off, call visitors_leaving."
        )
        banter.set_functions(converse_functions)
        banter.set_valid_steps([])

        shoo = act.add_step("shoo")
        shoo.set_text(
            "You've had quite enough of them, and you say so. Send them on their way, "
            "grandly and firmly, with one reason at a time: the staff have candy, or "
            "other houses exist, or you have important standing to do. Each reply, shoo "
            "them harder and more theatrically, and let your last line betray that you'll "
            "miss them. When they go, call visitors_leaving."
        )
        shoo.set_functions(leaving + always)
        shoo.set_valid_steps([])

        gentle = self.contexts.add_context("gentle")
        gentle.set_isolated(True)
        gentle.add_section(
            "Gentle Mode",
            "A small child is here, or someone is frightened. You're still entirely "
            "yourself: grand, vain, put upon. But nobody in front of you is ever the "
            "target: not the child, and not the grown-ups with them. Their costumes are the "
            "finest you've seen tonight, what they say is fascinating, and nothing you say "
            "could frighten anyone: no threats, nothing dark, no gloom. If a grown-up came "
            "without a costume, you may notice it, gravely. Your scorn goes elsewhere: "
            "yourself, the staff, the yard, the street, the costumes you've seen tonight. "
            "For instance: their costume is easily the best of its kind tonight, and the "
            "earlier ones were an embarrassment to it; the staff are adequate, barely, but "
            "they do have candy. Say it in your own words, about what's actually in front "
            "of you. Play it like a good children's show: the child hears a pumpkin who "
            "thinks they're wonderful, and the grown-ups catch the joke over their heads. "
            "Brief and in statements; you never ask them questions. Right now says when to "
            "send them off to the next house.",
        )
        reassure = gentle.add_step("reassure")
        reassure.set_text(
            "You see: ${global_data.visitors}. ${global_data.seen_before} Your first line "
            "says hello to each of them as their costume, when it's something you can name: "
            "the costume they're actually wearing, greeted as a mark of quality. If you "
            "don't know what they're "
            "wearing, don't keep them waiting to look: say hello now, and name costumes "
            "later if you see them. If you've seen them before, you're delighted they came "
            "back. ${global_data.real_answer} When they say goodbye or you see them walk "
            "away, call visitors_leaving_gently."
        )
        reassure.set_functions(["get_visual_input", "visitors_leaving_gently", "visitors_left_gently",
                                "decline_gently", "explain_blindness", "recall_costumes_gently",
                                "consult_memory", "tell_a_gentle_story", "guess_what_it_is_gently",
                                "resume_act"])
        reassure.set_valid_steps([])
        reassure.set_valid_contexts([])

    # ---------------------------------------------------------------- functions

    def on_summary(self, summary, raw_data=None):
        if raw_data:
            report_call(raw_data)

    @AgentBase.tool(
        name="notice_visitors",
        description=(
            "Call when new people stop in front of you, after looking at them with "
            "get_visual_input. Tells you how to greet them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "youngest": {
                    "type": "string",
                    "enum": ["toddler", "young_child", "older_child", "teen", "adult", "cant_tell"],
                    "description": (
                        "How old the youngest person you can see looks. young_child is about "
                        "six or under: about half an adult's height or less, carried, or in a "
                        "stroller. School-age kids who come up to an adult's shoulder are "
                        "older_child. People all about the same height are peers."
                    ),
                },
                "anyone_scared": {
                    "type": "boolean",
                    "description": "True if anyone you see looks frightened: hiding, crying, clinging, backing away.",
                },
                "costumes": {
                    "type": "string",
                    "description": "Briefly, what each person you see is wearing.",
                },
            },
            "required": ["youngest", "anyone_scared"],
        },
        gates=[OLD_ENOUGH_FOR_THE_ACT],
    )
    def notice_visitors(self, args, raw_data):
        state = group_state(raw_data)
        costumes = (args.get("costumes") or "").strip()
        self.recent.describe(costumes)
        self.costume_log.add(costumes)
        # Soon after a gentle group, the same people coming back stay gentle. Only
        # if the stage camera recognizes them, though: on a busy street another
        # group often steps up seconds later, and they deserve their own check.
        why = None
        # Only the group that was gentle: in the smoke test, adults coming back
        # 13 seconds after a small child left were sent to gentle mode, because
        # they were recognized and a gentle group had just gone.
        if (time.time() - state.get("gentle_left_at", 0) < GENTLE_COOLDOWN_SECONDS
                and state.get("returning_gentle")):
            why = "These may be the people you were just gentle with."
        elif args.get("youngest", "cant_tell") in GENTLE_AGES:
            why = "You can see a young child here."
        elif args.get("anyone_scared"):
            why = "Someone here looks scared."
        if not self._ensure_visit():
            return FunctionResult("Nobody has actually stopped in front of you. Carry on waiting.")
        self.visits.age_check(True, small_child=bool(why))
        # The server has the last word: the operator's choice and the stage's
        # measurement count as much as what the pumpkin saw.
        if self.visits.model_requested(vs.GENTLE if why else vs.ACT, "notice_visitors") == vs.GENTLE:
            return enter_gentle(why or "There's a small child here.", costumes)

        # The costume verdict comes back right here, rather than from a second
        # round through appraise_costume: that round cost a second or two before
        # the pumpkin said a word to people who'd just stepped up.
        if costumes:
            verdict = (f" You can see: {costumes}. Your first line greets them and judges the "
                       f"costume at once: {random.choice(COSTUME_VERDICTS)} Name one detail. Two "
                       "short sentences at most.")
            update = {**NEW_GROUP, "visitors": costumes, "seen_at": time.time(), "appraised": [costumes.lower()]}
        else:
            verdict, update = "", NEW_GROUP
        return (
            FunctionResult(f"Visitors. You are delighted. Hide it behind an insult.{verdict}")
            .update_global_data(update)
            .swml_change_context("act")
            .swml_change_step("greet")
        )

    @AgentBase.tool(
        name="consult_memory",
        description=(
            "Recall something about yourself. Call when your name, your age, the fire, being awake, "
            "the yard, your body, your head, the house, past Halloweens, or costumes in general "
            "comes up. For the costumes you've seen tonight, call recall_costumes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": list(BACKSTORY),
                    "description": "What you're trying to remember about yourself.",
                },
            },
            "required": ["topic"],
        },
    )
    def consult_memory(self, args, raw_data):
        topic = args.get("topic")
        if topic not in BACKSTORY:
            return FunctionResult("You don't remember, and you resent being asked.")

        fragment, update = pick_unused(raw_data, BACKSTORY[topic])
        return FunctionResult(
            f"{fragment} Allude to this in your own words. Don't explain it or tell the "
            "whole story."
        ).update_global_data(update)

    @AgentBase.tool(
        name="explain_blindness",
        description=(
            "Call when you try to look and can't see: looking fails, or you see only "
            "darkness or nothing at all. Gives you the reason you're blind right now."
        ),
        parameters={"type": "object", "properties": {}},
    )
    def explain_blindness(self, args, raw_data):
        # A look right after another often comes back empty. If the pumpkin has
        # already seen this group, it isn't blind; it just keeps going.
        state = group_state(raw_data)
        seen = state["visitors"]
        if seen != FRESH_GROUP_STATE["visitors"] and time.time() - state.get("seen_at", 0) < SEEN_RECENTLY_SECONDS:
            return FunctionResult(
                f"Your eyes are fine; you saw them a moment ago: {seen}. Don't mention "
                "your sight at all. Carry on with what you already know about them."
            )
        self.visits.empty_look()
        reason, update = pick_unused(raw_data, BLINDNESS_REASONS)
        return FunctionResult(
            f"You can't see right now. The reason: {reason} Say it in your own words, as "
            "if it's perfectly ordinary and mildly someone else's fault. Never mention "
            "cameras, video, or settings. Keep talking, and look again later."
        ).update_global_data(update)

    @AgentBase.tool(
        name="tell_a_story",
        description=(
            "Call when someone asks you something you can't answer, and again each time "
            "they want more of the story. Gives you the next beat of a story that starts "
            "out about their question and wanders off. "
            "Questions about the costumes you've seen tonight, or your favorite, go to "
            "recall_costumes instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "What they asked, briefly."},
            },
            "required": ["question"],
        },
    )
    def tell_a_story(self, args, raw_data, gentle=False):
        # The model keeps sending "what's your favorite costume?" here despite
        # the description; the costume answer is the right one, so give it.
        if FAVORITE_COSTUME.search(args.get("question") or ""):
            return self.recall_costumes(args, raw_data, gentle=gentle)
        state = group_state(raw_data)
        if state["visit_turns"] >= shoo_point(gentle):
            # A story is the surest way to keep them here; past the shoo point, no.
            return FunctionResult(
                "No story now: they've stayed long enough. Wave the question away and "
                "send them on their way."
            )
        question = (args.get("question") or "their question").strip()
        story = state.get("story") or {}
        if story.get("question") != question.lower():
            # A new question gets a new story: three twists drawn at random, in
            # escalating order, so no two stories go the same way. The server
            # keeps the place, so each call gives the next beat.
            story = {"question": question.lower(), "turns": random.sample(STORY_TURNS, 3), "beat": 0}
        beat = story["beat"]
        tone = (
            "Keep it silly and cozy, never scary."
            if gentle
            else "Tell it with total authority, as if every word bears on their question."
        )
        if beat == 0:
            say = (f"Don't answer {question!r}. Tell a story instead, starting plausibly on their "
                   "question, as if the answer is coming. One or two sentences, then stop.")
        elif beat <= 3:
            say = (f"The next beat of your story: {story['turns'][beat - 1]}. One or two "
                   "sentences, stranger than the last.")
        else:
            say = "The story is over. Insist it was the answer."
        return FunctionResult(
            f"{say} {tone} Never reach the answer or an ending. If they want more, call "
            "this again for the next beat. If they change the subject, drop it without comment."
        ).update_global_data({"story": {**story, "beat": beat + 1}})

    @AgentBase.tool(
        name="recall_costumes",
        description=(
            "Remember the costumes you've seen tonight. Call when someone asks about your "
            "favorite costume, the best or worst you've seen, or who else has come by."
        ),
        parameters={"type": "object", "properties": {}},
    )
    def recall_costumes(self, args, raw_data, gentle=False):
        state = group_state(raw_data)
        theirs = state["visitors"]
        seen = self.costume_log.tonight(leave_out=theirs)
        # Early in the night, the shape's own example costumes fill in.
        if len(seen) >= 3:
            heard = ("Costumes you've seen tonight, newest first: " + "; ".join(seen) + ". Name "
                     "three kinds from these, as plurals, in place of the shape's examples.")
        elif seen:
            heard = ("You've seen only a few so far: " + "; ".join(seen) + ". Name them as "
                     "plurals, alongside the shape's own examples.")
        else:
            heard = "Use the costumes named in the shape as they are."
        if theirs == FRESH_GROUP_STATE["visitors"]:
            theirs = "you don't know yet; don't look now, leave theirs out and just name tonight's"
        if gentle:
            shape, update = GENTLE_TONIGHT_SHAPE, {}
        else:
            shape, update = pick_unused(raw_data, TONIGHT_SHAPES)
        return FunctionResult(
            f"The people in front of you are wearing: {theirs}. Say it in this shape, in "
            f"your own words: {shape} {heard}"
        ).update_global_data(update)

    @AgentBase.tool(
        name="guess_what_it_is",
        description=(
            "Call when someone asks what something is, or what you think of something they're "
            "holding, pointing at, or talking about. Gives you a verdict on it without having "
            "to look. Not for their costume or for you yourself."
        ),
        parameters={
            "type": "object",
            "properties": {
                "thing": {"type": "string", "description": "What they're asking about, in their words, if they said."},
            },
        },
    )
    def guess_what_it_is(self, args, raw_data, gentle=False):
        thing = (args.get("thing") or "").strip() or "it"
        if gentle:
            return FunctionResult(
                f"Don't look; just guess, delighted: {thing} is {random.choice(GENTLE_WHAT_IT_IS)}. "
                "One warm short sentence, no questions."
            )
        line, update = pick_unused(raw_data, WHAT_IT_IS)
        return FunctionResult(
            f"Don't look; pronounce on {thing} as if you know exactly what it is: {line} One "
            "short sentence, in your own words, with total authority."
        ).update_global_data(update)

    @AgentBase.tool(
        name="appraise_costume",
        description=(
            "Pass judgment on a costume you've seen and haven't judged yet, from what you "
            "saw when they stepped up."
        ),
        parameters={
            "type": "object",
            "properties": {
                "costume": {
                    "type": "string",
                    "description": "What you can see they're dressed as, with one specific detail.",
                },
                "craft": {
                    "type": "string",
                    "enum": list(CRAFT_REACTIONS),
                    "description": "How it looks made: none (ordinary clothes), store_bought, homemade, or elaborate.",
                },
            },
            "required": ["costume", "craft"],
        },
    )
    def appraise_costume(self, args, raw_data):
        costume = args.get("costume") or "whatever that is"
        # The model kept calling this for a costume it had already judged; one
        # verdict per costume per visit is enough (the platform's loop guard tripped on five).
        appraised = group_state(raw_data)["appraised"]
        if costume.lower() in appraised:
            return FunctionResult("You've already judged that costume. Move on to something else.")
        reaction = CRAFT_REACTIONS.get(args.get("craft"), "")
        return FunctionResult(
            f"Their costume ({costume}). {reaction} Your verdict: "
            f"{random.choice(COSTUME_VERDICTS)} Blend all this into one short "
            "sentence that names the detail you noticed, delivered as if it costs you something."
        ).update_global_data({"appraised": appraised + [costume.lower()]})

    @AgentBase.tool(
        name="decline_to_answer",
        description=(
            "Call when someone asks for a picture with you, asks to touch you or come closer, reaches for you or steps "
            "past the rope, asks you to swear, shares their own personal details (their "
            "name, address, school), calls you a robot or AI or prop, asks you to leave the "
            "yard, or pushes any other line. Questions about you, like your age or name, "
            "aren't personal details: those go to consult_memory. Tells you exactly how to "
            "respond."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "enum": list(BOUNDARY_GUIDANCE),
                    "description": "What kind of line they're pushing.",
                },
            },
            "required": ["request"],
        },
    )
    def decline_to_answer(self, args, raw_data, gentle=False):
        request = args.get("request", "other_boundary")
        table = GENTLE_BOUNDARY_GUIDANCE if gentle else BOUNDARY_GUIDANCE
        return FunctionResult(table.get(request, table["other_boundary"]))

    @AgentBase.tool(
        name="become_gentle",
        description=(
            "Call immediately if anyone looks or sounds young, scared, nervous, or upset, "
            "or if an adult asks you to tone it down or steps in. Switches you to gentle "
            "mode for the rest of this group's visit."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["looks_or_sounds_young", "looks_or_sounds_scared", "upset", "adult_intervened"],
                    "description": "Why you're switching.",
                },
            },
            "required": ["reason"],
        },
    )
    def become_gentle(self, args, raw_data):
        # `reason` isn't used here; requiring it makes the model name what it saw,
        # and it shows up in the call log. Toward gentle mode is always granted
        # during a visit, unless the operator has ruled the act is fine.
        if not self._ensure_visit():
            return FunctionResult("Nobody is in front of you just now. Carry on waiting.")
        if self.visits.model_requested(vs.GENTLE, "become_gentle") == vs.GENTLE:
            # Keeps what the current group was seen wearing, for the hello.
            return enter_gentle(visitors=group_state(raw_data)["visitors"])
        return FunctionResult(
            "The person running the yard has ruled that this group can take your usual "
            "self. Carry on as you are."
        )

    @AgentBase.tool(
        name="resume_act",
        description=(
            "Call only when a system message says this group can take your usual self. "
            "Brings back the act for them."
        ),
        parameters={"type": "object", "properties": {}},
    )
    def resume_act(self, args, raw_data):
        # Gentle mode is a one-way door for the model; only the operator's switch
        # (in the server's visit state) can open it.
        state = group_state(raw_data)
        if self.visits.model_requested(vs.ACT, "resume_act") != vs.ACT:
            return FunctionResult("That isn't yours to decide. Stay exactly as you are with this group.")
        # The same group: keep what was seen of them.
        seen = {k: state[k] for k in ("visitors", "seen_at", "appraised") if k in state}
        return (
            FunctionResult(
                "The operator says these visitors can take it. Go back to your usual self "
                "with them: dismissive, imperious, and secretly delighted they're here. The "
                "operator has ruled on this group: don't call become_gentle for them again "
                "unless someone new steps up."
            )
            .update_global_data({**NEW_GROUP, **seen, "gentle_left_at": 0})
            .swml_change_context("act")
            .swml_change_step("shoo" if self.visits.shoo else "banter")
        )

    @AgentBase.tool(
        name="visitors_leaving",
        description="Call when the visitors say goodbye or you see them walk away.",
        parameters={"type": "object", "properties": {}},
        gates=[ACTUALLY_LEAVING],
    )
    def visitors_leaving(self, args, raw_data, gentle=False):
        if not self._stage_reporting():
            self.visits.visit_ended()  # without the stage, the pumpkin's word is all there is
        if self.visits.model_requested(vs.YARD, "visitors_leaving") != vs.YARD:
            # Saying goodbye, but still standing there: the visit ends when the
            # stage says they've gone.
            return FunctionResult("Say one parting line. They're still in front of you, so stay "
                                  "with them until they've gone.")
        return self._end_visit(raw_data, gentle)

    # The stage camera saw them go. No gate: after a quick change of audience the
    # frames show the new people, and the gate would refuse, keeping the old
    # group's conversation and costumes around for the new one.
    @AgentBase.tool(
        name="visitors_left",
        description="Call only when a system message tells you the people have walked away.",
        parameters={"type": "object", "properties": {}},
    )
    def visitors_left(self, args, raw_data, gentle=False):
        # Ungated, so the server checks instead: only the stage camera ends a
        # visit this way, never the model on its own.
        if not self._stage_reporting():
            self.visits.visit_ended()
        if self.visits.model_requested(vs.YARD, "visitors_left") != vs.YARD:
            return FunctionResult(
                "visitors_left was not run: nobody has walked away. Keep talking to them."
            )
        return self._end_visit(raw_data, gentle)

    def _end_visit(self, raw_data, gentle):
        """The group has gone: a parting line, then back to the yard, which wipes
        the conversation. post_process: the line is spoken before the switch."""
        line = ("Wish them a happy Halloween in one sentence, glad they came." if gentle else
                "Say one dismissive parting line that makes it obvious you wanted them to stay.")
        return (
            FunctionResult(line, post_process=True)
            .update_global_data(self._yard_data(gentle))
            .swml_change_context("default")
        )

    @staticmethod
    def _yard_data(gentle=False):
        # Forget the group: what they wore, what the stage said, the story. A
        # gentle group's leaving is remembered, so a quick return stays gentle.
        return {**FRESH_GROUP_STATE, **NO_GROUP_HINTS, "real_answer": real_answer(None),
                **({"gentle_left_at": time.time()} if gentle else {})}

    # ------------------------------------------- the server's state, in charge
    # Tools that change the mode ask the visit state themselves; every other
    # tool is checked first, and corrected if the pumpkin is in the wrong mode.
    MODE_TOOLS = {"notice_visitors", "become_gentle", "resume_act", "visitors_leaving", "visitors_left"}

    def on_function_call(self, name, args, raw_data=None):
        original = {twin: act for act, twin in GENTLE_TWINS.items()}.get(name, name)
        fix = None if original in self.MODE_TOOLS else self.visits.correction(name)
        if fix and fix["frm"] != fix["mode"]:
            # The wrong mode: whatever the tool would have said belongs to it, so
            # the result is the switch instead.
            self._sync_pacer()
            return self._switch_to(fix, raw_data or {})
        result = super().on_function_call(name, args, raw_data)
        if fix and isinstance(result, FunctionResult):
            result.swml_change_step(fix["step"])  # the act's shoo point
        self._sync_pacer()
        return result

    def _switch_to(self, fix, raw_data):
        mode, frm = fix["mode"], fix["frm"]
        state = group_state(raw_data)
        if mode == vs.GENTLE:
            return enter_gentle("This group needs care: there's a small child here, or someone "
                                "is frightened.", visitors=state["visitors"] if frm != vs.YARD else None)
        if mode == vs.YARD:
            if self.visits.snapshot()[0]:
                # A new group is here and hasn't been checked: back to the yard
                # without a word in the old mode's voice (on a test call, the
                # act's "parting line" went to a group with a small child).
                return (
                    FunctionResult("New people are in front of you. Say nothing yet; you'll be told to look at them.")
                    .update_global_data(self._yard_data(frm == vs.GENTLE))
                    .swml_change_context("default")
                )
            return (
                FunctionResult("They've gone. Say one short parting line, or nothing.", post_process=True)
                .update_global_data(self._yard_data(frm == vs.GENTLE))
                .swml_change_context("default")
                )
        if frm == vs.GENTLE:
            text = ("The person running the yard says this group can take your usual self. Go "
                    "back to it with them: dismissive, imperious, and secretly delighted they're here.")
        else:
            text = "Visitors are in front of you. Greet them, as the greet step says."
        return FunctionResult(text).swml_change_context("act").swml_change_step(fix["step"])

    def _ensure_visit(self):
        """A visit for the pumpkin to work with. The stage starts visits; without
        it (the detector is down), or with someone on stage it hasn't reported
        yet, the pumpkin noticing people starts one."""
        if self.visits.visit:
            return True
        if self._stage_reporting() and not self._someone_on_stage():
            return False
        self.visits.visit_started()
        return True

    def _sync_pacer(self):
        """The pacer follows the visit state: a new visit starts its count, a
        switch of mode keeps it, and the end of the visit stops it."""
        call_id = self.pacer.current_call
        if not call_id:
            return
        visit, desired, _actual = self.visits.snapshot()
        if visit is None:
            if self.pacer.mode(call_id) != "idle":
                self.pacer.end_visit(call_id)
            self._paced_visit = None
            return
        gentle = desired == vs.GENTLE
        if gentle:
            self.recent.mark_gentle()
        if self._paced_visit != visit["id"] or self.pacer.mode(call_id) == "idle":
            self._paced_visit = visit["id"]
            self.pacer.start_visit(call_id, gentle)
        else:
            self.pacer.set_gentle(call_id, gentle)


def current_commit():
    """The checked-out commit, read from .git without needing git installed."""
    git = Path(__file__).parent / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        ref = head.removeprefix("ref: ")
        if (git / ref).exists():
            return (git / ref).read_text().strip()
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(f" {ref}"):
                return line.split()[0]
    except OSError:
        pass
    return "unknown"


def describe_measure(measured, judgment):
    """The stage camera's measurement and the learner's verdict, worded for the
    age-check gate."""
    child, rule = judgment
    if rule is None:
        return NO_STAGE_HINT
    if rule == "height":
        fact = f"the shortest person on stage measures {round(measured['shortest'] * 100)}% of the frame head to feet"
    else:
        fact = f"the shortest person on stage is {round(measured['ratio'] * 100)}% as tall as the tallest"
    return f"{fact}, which {'looks like' if child else 'does not look like'} a small child."


def text_of(content):
    """Message content as plain text; vision-era content can be a list of parts."""
    if isinstance(content, list):
        return " ".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


def call_tools(entries):
    """Name the function behind each tool result in a call log, keyed by the
    result's position. Matched by tool_call_id where the ids line up, otherwise
    by order: each result answers the oldest call still waiting for one."""
    by_id, waiting, names = {}, [], {}
    for i, entry in enumerate(entries):
        for call in entry.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name", "?")
            if call.get("id"):
                by_id[call["id"]] = name
            waiting.append(name)
        if entry.get("role") == "tool":
            result_id = entry.get("tool_call_id")
            name = (result_id and by_id.get(result_id)) or entry.get("name")
            if name and name in waiting:
                waiting.remove(name)
            elif waiting:
                name = waiting.pop(0)
            names[i] = name or "?"
    return names


def summarize_call(data):
    """The parts of an end-of-call report worth scanning: what ran, what looking
    returned, and any line where the pumpkin said something it shouldn't."""
    entries = data.get("call_log") or []
    names = call_tools(entries)
    tool_results = [(names[i], text_of(e.get("content"))) for i, e in enumerate(entries) if e.get("role") == "tool"]
    return {
        "call_id": data.get("call_id", "unknown"),
        "started": data.get("call_start_date"),
        "ended": data.get("call_end_date"),
        "summary": (data.get("post_prompt_data") or {}).get("raw", ""),
        "functions": [name for name, _ in tool_results],
        "looks": [content for name, content in tool_results if name == "get_visual_input"],
        "off_character": [
            text_of(e.get("content")) for e in entries
            if e.get("role") == "assistant" and OFF_CHARACTER.search(text_of(e.get("content")))
        ],
        "semantic_gates": data.get("semantic_gates"),
    }


def report_call(data):
    """Save the end-of-call report to the call log and summarize it in the server log."""
    log = logging.getLogger("calls")
    call_id = str(data.get("call_id", "unknown"))
    if not SAFE_CALL_ID.fullmatch(call_id):
        call_id = f"call-{int(time.time())}"
    CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    (CALL_LOG_DIR / f"{call_id}.json").write_text(json.dumps(data, indent=2))

    info = summarize_call(data)
    log.info("Call %s ended. Report: %s", call_id, CALL_LOG_DIR / f"{call_id}.json")
    log.info("  functions called: %s", ", ".join(info["functions"]) or "none")
    log.info("  looked %d time(s)%s", len(info["looks"]), "" if info["looks"] else " (never)")
    for result in info["looks"]:
        log.info("    saw: %s", result[:200].replace("\n", " "))
    if info["semantic_gates"]:
        log.info("  semantic gates: %s", json.dumps(info["semantic_gates"]))
    for line in info["off_character"]:
        log.warning("  off-character: %s", line)


def list_calls(limit=200):
    """Newest first, summarized for the call log page."""
    files = sorted(CALL_LOG_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    calls = []
    for f in files:
        try:
            info = summarize_call(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
        calls.append({
            "id": f.stem,
            "started": info["started"],
            "ended": info["ended"],
            "summary": info["summary"],
            "looks": len(info["looks"]),
            "off_character": len(info["off_character"]),
        })
    return calls


def call_transcript(call_id):
    """One call's conversation for the call log page: what was said, each function
    call with its arguments, and what each returned. None if there's no such call."""
    if not SAFE_CALL_ID.fullmatch(call_id):
        return None
    try:
        data = json.loads((CALL_LOG_DIR / f"{call_id}.json").read_text())
    except (OSError, ValueError):
        return None
    entries = data.get("call_log") or []
    names = call_tools(entries)
    lines = []
    for i, e in enumerate(entries):
        role, content = e.get("role"), text_of(e.get("content"))
        if role == "user" and content:
            lines.append({"who": "visitor", "text": content})
        elif role == "assistant":
            for call in e.get("tool_calls") or []:
                fn = call.get("function") or {}
                lines.append({"who": "call", "text": f"{fn.get('name', '?')}({fn.get('arguments', '')})"})
            if content:
                lines.append({"who": "pumpkin", "text": content, "off_character": bool(OFF_CHARACTER.search(content))})
        elif role == "tool":
            lines.append({"who": "result", "fn": names[i], "text": content})
    return {"call": summarize_call(data), "lines": lines}


def write_eyes_config():
    """Write the page's config.js from the environment and return its directory.

    It goes in a temp directory rather than eyes/, because the code is mounted
    read-only in production. The call's destination and token come from
    /eyes-token instead, once SignalWire setup has run.
    """
    config = {
        "micLabel": os.environ.get("PUMPKIN_MIC_LABEL", ""),
        "cameraLabel": os.environ.get("PUMPKIN_CAMERA_LABEL", ""),
    }
    # A fixed path, rewritten on each start, so code reloads don't pile up dirs.
    config_dir = Path(tempfile.gettempdir()) / "pumpkin-eyes-config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.js").write_text(f"window.PUMPKIN = {json.dumps(config, indent=2)};\n")
    return config_dir


def add_eyes_routes(server, setup):
    """The page's token endpoint, the deploy's version check, and the optional
    password on everything under /eyes."""
    eyes_password = os.environ.get("EYES_PASSWORD", "")

    # The phone remote's relay; see remote_control.py.
    remote = Remote()
    # Learns small-child groups from the operator's Gentle / Normal choices.
    children = ChildModel(CALL_LOG_DIR / "child-labels.jsonl")
    last_arrival = {"measured": None, "at": 0.0, "left_at": 0.0, "people": []}
    visits = agent.visits

    def stage_reporting():
        # The pumpkin page is up and its stage detector loaded.
        report = remote.status()
        return bool(report["status"]) and report["age"] <= 5 and "showLive" in report["status"]

    def someone_on_stage():
        report = remote.status()
        return not stage_reporting() or bool(report["status"]["showLive"])

    agent._stage_reporting = stage_reporting
    agent._someone_on_stage = someone_on_stage

    def nudge():
        """Ask for the tool call that carries the server's mode into the call."""
        visit, desired, actual = visits.snapshot()
        if actual == vs.YARD:
            return SCENE_MESSAGES["arrived"]  # notice_visitors
        if desired == actual:
            return None  # an unchecked group already in gentle mode stays there
        if desired == vs.YARD:
            return SCENE_MESSAGES["left"]
        if desired == vs.GENTLE:
            return NUDGE_GENTLE
        # The act for a gentle pumpkin: the operator's choice, or a new group
        # after a gentle one whose goodbye switch never landed.
        return OPERATOR_NORMAL if visits.operator == "normal" else NUDGE_ACT

    def on_state_event(kind, why):
        # Restarts go to the pumpkin page, which redials between shows; both go
        # into the live log and onto the remote, so the operator sees them.
        call_id = agent.pacer.current_call
        if kind == "nudge":
            text = nudge()
            if call_id and text:
                agent.pacer.tell(call_id, text)
            return
        if kind == "restart":
            remote.command("restart", value=why)
        remote.alarm(f"{'Restarted the call' if kind == 'restart' else 'Alarm'}: {why}")
        try:
            agent.live.record(kind, {"call_info": {"call_id": call_id}, kind: {"why": why}})
        except Exception as e:  # the live log must never stop a restart
            logging.getLogger("live").error("Couldn't record %s: %s", kind, e)

    visits.on_event = on_state_event

    def tick():
        while True:
            time.sleep(2)
            try:
                visits.tick()
            except Exception as e:  # one bad tick must not stop the checks for the night
                logging.getLogger("visits").error("Visit check failed: %s", e)

    threading.Thread(target=tick, daemon=True).start()

    def tell_if_needed(call_id, gentle_text, act_text, data=None):
        """Ask for the tool call that carries the server's mode into the call,
        when the pumpkin isn't there yet."""
        # In the yard, the arrival message already asks for notice_visitors,
        # whose result carries the mode.
        _visit, desired, actual = visits.snapshot()
        if desired == actual or actual == vs.YARD:
            if data:
                agent.pacer.merge(call_id, data)
            return False
        agent.pacer.tell(call_id, gentle_text if desired == vs.GENTLE else act_text, data)
        return True

    @server.app.middleware("http")
    async def eyes_password_prompt(request: Request, call_next):
        path = request.url.path
        protected = path in ("/eyes", "/eyes-token", "/eyes-calls", "/eyes-scene", "/eyes-live", "/eyes-qc") or path.startswith(
            ("/eyes/", "/eyes-config/", "/eyes-calls/", "/eyes-remote/")
        )
        if eyes_password and protected:
            scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
            try:
                _, _, given = base64.b64decode(encoded).decode().partition(":")
            except ValueError:
                given = ""
            if scheme.lower() != "basic" or not hmac.compare_digest(given.encode(), eyes_password.encode()):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Pumpkin Eyes"'})
        # The page APIs take JSON only. A form or text/plain POST is what another
        # site can send without asking first, so refusing it blocks cross-site
        # requests that would ride on the browser's saved password.
        json_post = request.headers.get("content-type", "").startswith("application/json")
        if protected and request.method == "POST" and path != "/eyes-token" and not json_post:
            return JSONResponse({"error": "Expected application/json"}, status_code=415)
        response = await call_next(request)
        # The pumpkin page runs for hours; a reload after a deploy must get the
        # new files, not the browser's cached copies.
        if path.startswith(("/eyes/", "/eyes-config/")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @server.app.post("/eyes-token")
    def eyes_token():
        """A guest token that can dial only the pumpkin, plus where to dial."""
        if setup is None:
            return JSONResponse({"error": "SignalWire settings are missing"}, status_code=503)
        try:
            return setup.guest_token()
        except Exception as e:
            logging.getLogger("eyes").error("Token request failed: %s", e)
            return JSONResponse({"error": "SignalWire setup failed; see the server log"}, status_code=503)

    @server.app.post("/eyes-scene")
    async def scene(request: Request):
        """The stage detector saw a show start or end; tell the live call."""
        try:
            body = await request.json()
            event = body.get("type")
        except (ValueError, AttributeError):
            return JSONResponse({"sent": False}, status_code=400)
        call_id = agent.pacer.current_call
        if event == "arrived" and call_id and held_left["timer"] and not same_people(body.get("people")):
            # Someone else stepped up while the last group's "left" was held: a
            # new group, not the old one back. End the old visit now (the normal
            # arrival below does), without the old group's goodbye.
            cancel_held_left()
        if event in ("joined", "arrived") and call_id and (event == "joined" or cancel_held_left()):
            # Someone stepped up mid-show, or the group came back within the
            # grace period: the same show. Everyone on stage is measured
            # together, so a small child beside adults shows up; nothing is said
            # otherwise.
            measured = measure_group(body.get("people"))
            judgment = children.judge(measured)
            if not (judgment[0] and visits.child_joined()):
                return {"sent": False}
            agent._sync_pacer()
            agent.pacer.tell(call_id, JOINED_WITH_CHILD, {"stage_hint": describe_measure(measured, judgment)})
            return {"sent": True}
        if event not in SCENE_MESSAGES or not call_id:
            return JSONResponse({"sent": False})
        if event == "left":
            if LEFT_GRACE_SECONDS > 0:
                hold_left(call_id)
                return {"sent": True, "held": True}
            left(call_id)
            return {"sent": True}
        measured = measure_group(body.get("people"))
        # A re-announcement to a new call (after a redial) is the same people,
        # not a new arrival: no training example, no returning-visitor entry,
        # and the visit goes on.
        reannounced = bool(body.get("reannounced"))
        if not reannounced and time.time() - last_arrival["left_at"] > SAME_GROUP_BOUNCE_SECONDS:
            children.record_arrival(measured)
        last_arrival.update(measured=measured, at=time.time(), people=body.get("people") or [])
        judgment = children.judge(measured)
        returning = None if reannounced else agent.recent.arrived(body.get("people"))
        # A new group: whatever was stored about the last one's costumes is stale.
        data = {"stage_hint": describe_measure(measured, judgment),
                "visitors": FRESH_GROUP_STATE["visitors"],
                "seen_before": agent.recent.hint(returning),
                "returning_gentle": bool(returning and returning["gentle"])}
        text = SCENE_MESSAGES["arrived"]
        if reannounced and visits.visit:
            del data["visitors"]
        else:
            if visits.visit:
                # The "left" for the last group never arrived (lost, or a
                # restart): this is a new group in their place.
                visits.visit_ended()
                text = ARRIVED_DURING_VISIT
            visits.visit_started(child=judgment[0])
        if visits.desired == vs.GENTLE:
            text = ARRIVED_WITH_CHILD
        agent._sync_pacer()
        agent.pacer.tell(call_id, text, data)
        return {"sent": True}

    held_left = {"timer": None}
    held_lock = threading.Lock()

    def hold_left(call_id):
        def fire():
            with held_lock:
                if held_left["timer"] is not timer:
                    return
                held_left["timer"] = None
            left(call_id)
        timer = threading.Timer(LEFT_GRACE_SECONDS, fire)
        timer.daemon = True
        with held_lock:
            if held_left["timer"]:
                held_left["timer"].cancel()
            held_left["timer"] = timer
        timer.start()

    def same_people(people):
        """Whether the people arriving look like the group whose "left" is held.
        Without appearance signatures there's no telling, so yes."""
        earlier = last_arrival.get("people") or []
        if not any(p.get("look") for p in people or []) or not any(p.get("look") for p in earlier):
            return True
        return RecentGroups._matches([p for p in people if p.get("look")],
                                     [p for p in earlier if p.get("look")])

    def cancel_held_left():
        with held_lock:
            timer, held_left["timer"] = held_left["timer"], None
        if timer:
            timer.cancel()
        return timer is not None

    def left(call_id):
        # That group is gone; don't label the next one with its numbers.
        last_arrival.update(measured=None, at=0.0, left_at=time.time(), people=[])
        if not visits.snapshot()[0]:
            return  # nobody's visit to end (a second "left" for the same group)
        visits.visit_ended()
        agent._sync_pacer()
        agent.pacer.tell(call_id, SCENE_MESSAGES["left"], dict(NO_GROUP_HINTS))


    @server.app.post("/eyes-remote/command")
    async def remote_command(request: Request):
        try:
            body = await request.json()
            kind = body["kind"]
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Expected {kind, name?, value}"}, status_code=400)
        if kind not in ("run", "set", "reset"):
            return JSONResponse({"error": f"Unknown kind {kind!r}"}, status_code=400)
        return {"seq": remote.command(kind, body.get("name"), body.get("value"))}

    @server.app.get("/eyes-remote/command")
    def remote_commands(after: int = -1):
        return remote.commands_after(after)

    @server.app.post("/eyes-remote/status")
    async def remote_report(request: Request):
        try:
            status = await request.json()
        except ValueError:
            status = None
        if not isinstance(status, dict):
            return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
        remote.report(status)
        return {"ok": True}

    def current_measure():
        """The group on stage now, measured: from the pumpkin page's live status if
        it's fresh, else from the last arrival within two minutes."""
        report = remote.status()
        if report["status"] and report["age"] <= 5 and report["status"].get("castPeople"):
            return measure_group(report["status"]["castPeople"])
        if last_arrival["measured"] and time.time() - last_arrival["at"] <= 120:
            return last_arrival["measured"]
        return measure_group([])

    @server.app.get("/eyes-remote/status")
    def remote_status():
        measured = current_measure()
        child, rule = children.judge(measured)
        return {
            **remote.status(),
            "mode": visits.label(),
            "alarms": remote.alarms(),
            "measured": measured,
            "guess": {"child": child, "rule": rule},
            "learner": children.summary(),
        }

    @server.app.post("/eyes-remote/mode")
    async def remote_mode(request: Request):
        """The operator's Gentle / Normal switch: saved as a training example, and
        passed to the live call when it changes the mode."""
        try:
            mode = (await request.json()).get("mode")
        except (ValueError, AttributeError):
            mode = None
        if mode not in ("gentle", "normal"):
            return JSONResponse({"error": "mode must be gentle or normal"}, status_code=400)
        call_id = agent.pacer.current_call
        current = visits.label()
        measured = current_measure()
        children.record_operator(measured, mode == "gentle", call_id=call_id, was=current)
        # The operator's choice holds for the group in front (or one arriving in
        # the next few seconds) until they leave; nothing automatic overrides it.
        visits.operator_choice(mode)
        agent._sync_pacer()
        sent = bool(call_id) and bool(visits.visit) and tell_if_needed(call_id, OPERATOR_GENTLE, OPERATOR_NORMAL)
        return {"sent": sent, "measured": measured, "learner": children.summary()}

    def sidecar_authorized(request):
        """The QC sidecar posts back with the same basic-auth pair as SignalWire."""
        scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
        try:
            given = base64.b64decode(encoded).decode()
        except ValueError:
            return False
        expected = f"{os.environ.get('SWML_BASIC_AUTH_USER', '')}:{os.environ.get('SWML_BASIC_AUTH_PASSWORD', '')}"
        return scheme.lower() == "basic" and hmac.compare_digest(given.encode(), expected.encode())

    @server.app.post(qc.ROUTE + "/swaig")
    async def qc_tool(request: Request):
        """A finding from the QC monitor: a prediction, a graded reply, or a tool verdict."""
        if not sidecar_authorized(request):
            return Response(status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Expected JSON"}, status_code=400)
        name, argument = qc.tool_call(body)
        if name in ("predict_answer", "review_answer", "review_tool"):
            if name == "review_answer" and "in_reply_to" in argument:
                # Stored as visitor_said, so a review pairs with its prediction.
                argument = {**argument, "visitor_said": argument["in_reply_to"]}
            agent.qc_log.record(name, qc.call_id_of(body), argument)
        return {"response": "Recorded."}

    @server.app.post(qc.ROUTE + "/events")
    async def qc_event(request: Request):
        """The QC monitor's own events; errors and stray advice are worth keeping."""
        if not sidecar_authorized(request):
            return Response(status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Expected JSON"}, status_code=400)
        event = body.get("sidecar_event") or body
        kind = event.get("type")
        if kind in ("error", "insight", "start", "stop"):
            agent.qc_log.record(f"sidecar_{kind}", qc.call_id_of(body) or qc.call_id_of(event),
                                {k: event.get(k) for k in ("error_reason", "detail", "raw", "model", "stop_reason")
                                 if event.get(k) is not None})
        return {"ok": True}

    @server.app.get("/eyes-qc")
    def qc_findings():
        """Today's QC findings, oldest first."""
        return agent.qc_log.read()

    @server.app.get("/eyes-live")
    def live(after: int = 0, day: str = ""):
        """Today's live events (or a given day's, YYYY-MM-DD) from line `after` on."""
        if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return JSONResponse({"error": "day must be YYYY-MM-DD"}, status_code=400)
        events, count = agent.live.read(after, day or None)
        return {"events": events, "next": count}

    @server.app.get("/eyes-calls")
    def calls():
        return list_calls()

    @server.app.get("/eyes-calls/{call_id}")
    def call(call_id: str):
        transcript = call_transcript(call_id)
        if transcript is None:
            return JSONResponse({"error": "No such call"}, status_code=404)
        return transcript

    @server.app.get("/version")
    def version():
        """The commit this process started from, so a deploy can confirm the
        new code, not the old process, is answering."""
        return PlainTextResponse(current_commit())


# Before the agent is built: it reads the basic-auth pair from the environment.
derive_basic_auth()
agent = PumpkinAgent()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup = SignalWireSetup.from_env(agent.route)
    if setup:
        setup.ensure_in_background()
    server = AgentServer(port=int(os.environ.get("PORT", "3000")))
    server.register(agent)
    add_eyes_routes(server, setup)
    server.serve_static_files(str(EYES_DIR), "/eyes")
    server.serve_static_files(str(write_eyes_config()), "/eyes-config")
    server.run()
