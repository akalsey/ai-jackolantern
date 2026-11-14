# The Pumpkin

A burning jack-o'-lantern on a scarecrow's body that talks to trick-or-treaters
in a front yard. It's a SignalWire voice AI agent that can see. A computer hidden
in the prop (a Chromebook works well), with a webcam, a microphone, and a
speaker, keeps one video call open to the agent all night. The character is dismissive and imperious and
wants visitors gone, while secretly wishing they'd stay. It never breaks
character and never admits to being a machine. With a small or scared child in
the group it goes into gentle mode: the same grand pumpkin, but nobody in the
group (child or grown-up) is ever the joke, and nothing it says is scary.

This repo is published as an example for SignalWire customers. `README.md`
covers setup for a person; this file covers how the parts fit and what to watch
out for when changing them.

## Layout

| Path | What it is |
|---|---|
| `pumpkin_agent.py` | The agent (SignalWire Python SDK): prompt, contexts and steps, SWAIG functions, semantic gates, AI params, and the HTTP routes for the pumpkin page and call log. Also the entry point (`AgentServer`). |
| `surroundings.example.txt` | A sample of `surroundings.txt`: facts about one yard and prop (the neighborhood, nearby attractions, what the scarecrow wears). The real file sits next to `pumpkin_agent.py` on the host and stays out of git. |
| `signalwire_setup.py` | On startup, creates or corrects the SWML webhook resource in the space and mints guest tokens for the page. |
| `child_model.py` | Learns when a group includes a small child from the operator's Gentle / Normal choices. |
| `costume_log.py` | Every group's costumes tonight, in the call-log volume, for `recall_costumes`. |
| `returning_visitors.py` | Remembers recent groups' looks and costumes, so the pumpkin can tell when one comes back. |
| `live_log.py` | A compact line per debug event (what visitors and the pumpkin said, function calls, step changes) for reading the night while the call is still going, at `/eyes-live`. |
| `qc_monitor.py`, `qc_report.py` | The quality-control monitor: an `ai_sidecar` that listens to the call, predicts in-character answers, grades the real ones and the tools used, and logs findings for `/eyes/qc.html`; and a Markdown report of them for tuning. |
| `reload_filter.py` | Tells `watchfiles` to restart the agent only for `.py` files and `surroundings.txt`. |
| `reset_training.py` | Removes the learner's examples (`child-labels.jsonl`) and the night's costumes (`costumes.jsonl`) recorded in a time window, or all of them, backing each file up first. For clearing a test's entries before a real night; the README shows the `docker compose exec` form. The agent reads the learner's examples at startup, so restart it afterwards. |
| `remote_control.py` | In-memory relay between the phone remote and the pumpkin page: numbered commands one way, status the other. |
| `visit_state.py` | The server's own state for each visit (yard, act, gentle), which the model's contexts are made to match: corrections on every tool result, refusals of switches the server doesn't allow, and restarts when the pumpkin is stuck or repeating itself. |
| `sim/replay_agent.py` | Replays a recorded night through the real server: stage events into `/eyes-scene`, the operator's presses into `/eyes-remote/mode`, the model's recorded tool calls through `on_function_call`, with a small model of the platform applying each result's context and step changes and offering only each step's tools. Reports wrong-mode minutes by kind, tools not offered, nudges, restarts, and alarms. About 25 seconds for a 4-hour night. |
| `sim/replay.py` | Replays a recorded night (the live log) through `visit_state.py` with the page's new visit rules, and reports visits, wrong-mode minutes, corrections, restarts, alarms, and conversation size. Recorded nights go in `sim/data/`, which git ignores: they hold what visitors said. |
| `visit_pacing.py` | Counts exchanges per visit from the platform's debug events and pushes an escalating `visit_mood` into the live call. |
| `eyes/` | The page the pumpkin's computer opens (the call client, voice effects, mic noise gate, stage detector), the phone remote, and the call log viewer. `controls.js` defines every slider for both pages. |
| `DECISIONS.md` | Why the design is what it is and how each choice evolved, with the business-agent pattern each one illustrates. |
| `tests/` | `node tests/stage.test.mjs` (stage logic), `node tests/picture.test.mjs` (cuts and clothing colors, day and night), `uv run tests/test_visit_pacing.py` (pacing), `uv run tests/test_child_model.py` (the learner), `uv run tests/test_returning_visitors.py` (recognizing a group that comes back), `uv run tests/test_costume_log.py` (tonight's costumes), `uv run tests/test_live_log.py` (the live log, and that level-2 events leave pacing alone), and `uv run tests/test_qc.py` (the QC monitor), `uv run tests/test_agent.py` (who goes gentle, who can leave it, corrections on every tool call, restarts, and what the stage camera and the Normal switch send), `uv run tests/test_visit_state.py` (the visit state and the replay), `uv run tests/test_reset_training.py` (clearing what it has learned). Run them all before updating a live pumpkin. |
| `Dockerfile`, `docker-compose.yml` | One container behind nginx-proxy, with the checkout mounted read-only. |

Python files use `uv` with inline script metadata (`# /// script`). Dependencies
live in `pumpkin_agent.py`'s header; the Dockerfile installs from it with
`uv export --script`.

## How a call works

1. The pumpkin's computer opens `/eyes/` and the operator clicks Start (Chrome won't
   play audio without a click). The page gets a guest token from `/eyes-token`
   and dials the agent's address over video.
2. SignalWire fetches the agent's SWML from `/pumpkin` (basic auth) and runs
   the `ai` verb. The call stays up all night; the page redials if it drops.
3. The pumpkin looks with the platform's built-in `get_visual_input`
   (`enable_vision`) and talks. SWAIG functions run on this server at
   `/pumpkin/swaig`.
4. On the page, the stage detector watches the camera and posts "arrived" and
   "left" to `/eyes-scene`. The server relays them into the call as system
   messages, so the pumpkin greets and dismisses people without having to
   notice them itself.
5. Debug events (level 2, which includes the dialogue) stream to
   `/pumpkin/debug_events`, which feeds visit pacing and the live log.
6. When the call ends, the post-prompt report goes to `/pumpkin/post_prompt` and
   is saved for the call log.

## The conversation

### Prompt layers

- **Base prompt**, applied everywhere: Who You Are, Your Surroundings (only
  when `surroundings.txt` exists, one bullet per line of it), Your Eyes, Your Past, How
  You Talk, The Staff (the people handing out candy, who handle that and anything
  else beneath the pumpkin), What You Don't Know, Lines You Hold.
- **Context prompt**: The Act (in `default`) or Gentle Mode (in `gentle`).
- **Step text**, filled in once when the step is entered (its `${...}` values
  don't update mid-step). The mood used to live here and never changed within
  a step, so the pumpkin never shooed; it's now the base prompt's first
  section, "Right Now" (`${global_data.visit_mood}`), and at the shoo point the
  pacer also says `SHOO_NOW` into the call once, so the switch happens on that
  turn.
- **`conscience`** (in `params`), re-applied after every function call. It
  exists because the platform's looking function says things like "I cannot see
  you without your video enabled", and the model would repeat them.

### Voice

A Cartesia `sonic-2` voice set by ID (`add_language` in `pumpkin_agent.py`),
chosen for low latency. Non-word sounds are spelled out ("Hm.", "Ahem.")
rather than relying on an engine's sound tags. The
looking filler is one of these: it plays the instant a look starts, which is
how a show opens once the stage detector triggers the first look.

### Contexts and steps

The server's visit state (`visit_state.py`, below) decides which mode the
pumpkin should be in; each mode is a context, and every tool result switches the
pumpkin's context to match.

- `default` (the yard: nobody here): one step, `idle`. It looks at new arrivals
  and calls `notice_visitors`, whose result moves to the act or gentle mode.
- `act`: `greet` → `banter` → `shoo`. The model moves from greet to banter
  itself; the server moves banter to shoo at the pacer's shoo point. There is
  no farewell step: a leaving tool's result says one parting line
  (`post_process`) and switches back to the yard.
- `gentle`: one step, `reassure`, with no `valid_steps` or `valid_contexts`.

All three are isolated (`set_isolated(True)`): entering one wipes the
conversation. The call lasts all night; with `set_history("hide")`, which this
replaced, each show hid the last from the model but not from the conversation's
size, and the call hit the platform's 128,000-token limit after 38 minutes on
Halloween night. Without either, every show saw every earlier one, so the
pumpkin repeated its old lines and kept the name it had given an earlier group.
What a step needs from before is carried in `global_data`: `notice_visitors`
saves what it saw in `visitors` for the greet and reassure steps; `enter_gentle`
keeps the current group's for `become_gentle`; and leaving resets it, so the
next group is never greeted as the last. Going gentle mid-visit forgets that
visit's conversation, which also means a child never inherits the act's insults.

Gentle mode is a one-way door for the model: `resume_act` only works when the
operator chose Normal for this group (`visit_state` refuses it otherwise).
A group arriving within 90 seconds of a gentle group leaving starts gentle only
if the stage camera recognizes them as that gentle group (`returning_gentle`,
from `RecentGroups.mark_gentle`). On a real call, adults returning 13 seconds
after a small child left got gentle mode, because any recognized group counted.

Every step calls `set_functions` explicitly. Without it, a step inherits the
previous step's function list.

### Functions

| Function | What it does |
|---|---|
| `notice_visitors` | Reports what the model saw (youngest apparent age, anyone scared, costumes) and asks for the act or gentle mode; the visit state has the last word (the operator's choice and the stage's measurement count too). Starts a visit only when the stage isn't reporting or someone is on stage. Gated, see below. |
| `consult_memory` | Returns one contradictory backstory fragment per topic (name, age, fire, …), never repeated within a visit. |
| `explain_blindness` | An absurd reason it can't see, for when looking fails. If the group has already been seen (`global_data.visitors`), it says the eyes are fine and to carry on: a second look a second or two after the first often comes back empty ("You cannot currently see anything."), and the pumpkin kept claiming blindness right after describing costumes. "Your Eyes" also asks for one look per arrival, not one per remark. |
| `tell_a_story` | Instead of "I don't know": a story that starts on the question and escalates through three random absurd turns. Each call returns the next beat, with the place kept in `global_data.story` (the model used to be handed all three and asked to pace them). Refuses once a visit is past the shoo point. |
| `recall_costumes` | Tonight's costumes (from `costume_log.py`, leaving out the group in front) and a shape to say them in, ending on the group's own costume: wonderful except theirs, theirs the best, or hated them all, yours no better. Gentle mode always puts theirs above every other costume tonight. |
| `guess_what_it_is` | "What is that?" about something a visitor holds or points at: a confidently wrong verdict without looking (warm guesses in gentle mode). Added on the night: visitors kept asking, dark looks came back empty, and the pumpkin answered with blindness excuses. |
| `appraise_costume` | A costume verdict seeded by how the costume looks made. |
| `decline_to_answer` | Exact wording for boundaries (a photo, allowed only from their side of the rope; touch, profanity, personal info, "you're an AI", leave the yard): cold in the act, warm in gentle mode. |
| `become_gentle` | Asks for gentle mode; granted during a visit unless the operator chose Normal for this group. |
| `resume_act` | Back to the act, gentle mode only, and only when the operator chose Normal for this group. |
| `visitors_leaving` | A goodbye. Ends the visit only once the stage says they've gone (or with no stage detector); otherwise it asks for one parting line and stays with them. Gated, see below. |
| `visitors_left` | Ends a visit when the stage camera says so (the "left" message). Not gated: after a quick change of audience the frames show the new people, and the gate refused. Refused while the stage still has the group. |

Gentle mode is a context, never a flag. Tools whose wording differs by mode
have a gentle copy (`GENTLE_TWINS`: `tell_a_gentle_story`,
`recall_costumes_gently`, `decline_gently`, `visitors_leaving_gently`,
`visitors_left_gently`), registered by `_add_gentle_twins` with the original's
description, parameters, and gates, and a handler fixed to the gentle wording.
Only the copies are in the `reassure` step's list, so in gentle mode the act's
wording isn't reachable whatever any data says. (A `global_data.gentle` flag
used to choose the wording; it drifted after a lost context switch and a child
got the act.) A tool can't read the mode instead: SignalWire's tool requests
carry the call, `global_data`, and session details, but not the current
context or step, so being listed only in a context is how a tool knows it's
there.

Per-visit wording state lives in `global_data` (`used_fragments`, `visitors`,
`seen_at`, `appraised`, `story`, `real_answer`, `stage_hint`, `seen_before`,
`visit_turns`, `visit_mood`, `gentle_left_at`). Which mode the pumpkin should be
in, and who decided, lives on the server.

### The server owns the state

`visit_state.py` is the server's record of the group in front: the mode the
pumpkin should be in (`yard`, `act`, `gentle`), the mode the platform last
reported, the operator's choice, and whether the visit has reached its shoo
point. The platform's contexts are the pumpkin's state machine, but only the
model can move it (a tool result changes the context; a tool runs only when the
model calls one), and on Halloween night it ignored prose requests to switch for
50 minutes. So:

- **What changes it, in code:** the stage camera's arrivals and departures
  (`/eyes-scene`), the operator's switch (`/eyes-remote/mode`), the age check
  failing (a blocked `notice_visitors` in the debug events), a small child
  measured joining, and the pacer's shoo point.
- **Every tool request is checked** (`PumpkinAgent.on_function_call`). If the
  pumpkin is in the wrong mode, the result is the switch instead of the tool's
  answer (`_switch_to`); at the shoo point the answer goes out with a step change
  to `shoo`. The mode tools (`notice_visitors`, `become_gentle`, `resume_act`,
  the leaving tools) ask `model_requested` instead, which grants a switch toward
  gentle during a visit (unless the operator chose Normal) and refuses anything
  else the server didn't decide.
- **Messages still go into the call** at an arrival, a departure, a small child
  joining, or an operator press, because a correction rides on the next tool
  call and the message asks for one. If the pumpkin is in the yard, the arrival
  message's `notice_visitors` carries it, so the operator's message is skipped.
- **The platform's reports are the truth** about where the pumpkin is:
  `context_change` and `step_change` events update it, and `session_start`
  puts it back in the yard (every call starts there). A switch a tool result
  carries counts only once the platform reports it (`wait_for_reports`): on a
  real call, a goodbye's switch to the yard (sent with `post_process`) was lost
  when the next arrival interrupted it, and the server had already counted it.
  Until the report comes, every tool call sends the switch again: skipping
  it for a few seconds once let an act tool run during a switch to gentle
  mode, and the pumpkin mocked a child's costume.
- **No act before the age check.** A new group waits in the yard (its greeting
  suits anyone) until `notice_visitors` passes the age check; only then does it
  get the act. If the pumpkin is in gentle mode when a group is reported, it
  stays gentle until that group passes its own check. On a test call the
  server wanted the act the moment a group arrived, and twice a small child got
  it: once before any check, once when the camera re-reported the same child as
  a "new" group and the reset threw away the age check's verdict. A group that
  hasn't been checked after 8 seconds gets the arrival message again.
- **Nudges:** a mismatch that lasts 8 seconds with no tool call sends the
  message that asks for the tool carrying the fix (`notice_visitors` from the
  yard, `visitors_left` with nobody there, `become_gentle`, or `resume_act`).
  The server replay found the case: the age check failing for someone the
  stage never counted as a visit switches the platform to gentle mode with
  nobody there, and nothing else asks for a tool.
- **Restarts:** a mismatch that lasts 30 seconds, or three near-repeats of the
  pumpkin's own lines within two minutes, sends a `restart` command to the page,
  which waits for the pumpkin to finish speaking and (up to 20 seconds) for the
  stage to empty, then hangs up and redials at once. At most one every 5
  minutes; past that, an alarm instead. The page's re-announcement after the
  redial brings the tool call that sets the new call straight.
- **Alarms:** ten empty looks in ten minutes, a visit over ten minutes, or a
  restart held back. Each goes into the live log and onto the remote (`alarms`
  in `/eyes-remote/status`).
- **Replay:** `sim/replay.py` runs a recorded live log through the visit state
  alone; `sim/replay_agent.py` runs it through the real server (the agent's
  tool handling, the routes, the pacer) with a small model of the platform in
  between (contexts, steps, which tools each step offers, restarts).



### Safety is layered on purpose

Small or scared children must get gentle mode, so several independent paths lead
there, and none of them depends on the others:

- **A semantic gate on `notice_visitors`.** The platform's decision model sees
  3 camera frames. If it can't confirm (at 0.7) that everyone is past about 6
  (kids at an adult's shoulder or taller count as old enough) and
  nobody is scared, the gate's `on_fail` switches to gentle mode before the
  handler runs.
- **The handler itself** checks the model's `youngest` and `anyone_scared`
  answers, and the 90-second cooldown.
- **The server's visit state**, which corrects the pumpkin into gentle mode on
  its next tool call whatever the model does, and restarts the call if no tool
  call comes.
- **`become_gentle`**, which the prompt tells the model to call at once.
- **The stage camera's height ratio**: the shortest cast member's height over
  the tallest's. A learned cutoff (below) can send an arriving group straight to
  gentle mode, and the measurement goes into `global_data.stage_hint`, which the
  `notice_visitors` gate's question reads.
- **The operator**, with the Gentle switch on the phone remote.

Young children are judged by relative height: people all about the same height
are peers, because young children don't trick-or-treat alone. A gate on
`visitors_leaving` checks 2 frames so the pumpkin doesn't say goodbye to people
still standing there.

The act is dismissive and imperious but doesn't send visitors away on its own,
and it speaks mostly in statements: questions keep a conversation going.
Shooing and questions are both left to the pacing's `visit_mood` (below),
because the act alone tends to shoo from the first line, and a mood that only
says "don't shoo yet" makes it ask a question every turn.

### Visit pacing

The model can't be trusted to count turns, and the character wants company, so
it would talk forever. `visit_pacing.py` does the counting instead:

- A visit starts, goes gentle, and ends when the server's visit state says so
  (`PumpkinAgent._sync_pacer` after every change); the pacer no longer infers
  visits from the pumpkin's steps. Speech starts a visit only when the stage
  detector isn't reporting, so visits the pumpkin noticed by ear are still paced.
- One exchange is a `speech_detect` followed by an `ai_completion`. Muttering to
  an empty yard doesn't count.
- The mood arc: in the first exchange (`QUESTIONS_UNTIL`) a question of the
  pumpkin's own is allowed; then statements only; from exchange 3
  (`SHOO_AFTER`), each exchange pushes a sterner shoo mood. Each
  change goes out through `calling.ai_message(global_data=...)`. That merges into the call's
  data silently, without prompting a reply. Updates go through one queue so
  they arrive in order.
- At the shoo point the pacer says `SHOO_NOW` into the call once and tells the
  server (`on_shoo`), which moves the act to its shoo step on the next tool
  call and pokes the QC monitor with a `[PACE]` note.
- Gentle mode never asks questions (a talkative child needs no encouragement)
  and starts its send-offs sooner, from exchange 2 (`GENTLE_SHOO_AFTER`),
  ending in short goodbyes however much they say. The gentle moods are about
  timing only; the voice comes from the gentle context. `tell_a_story` refuses
  past `shoo_point(gentle)`.
- Each mood sent is also written to the live log (event `pacing`), so
  `/eyes-live` shows when it landed.

## Stage detection

On a busy street heads change constantly, so a headcount means little. What
matters is who's on stage: standing close in front of the pumpkin, interacting.

- **Detection (`eyes/scene.js`):** MediaPipe's person detector (EfficientDet-Lite0,
  in the browser) runs on the camera, on the GPU (WebGL) when it can and the CPU
  otherwise. It uses whole bodies rather than faces, because masks defeat face
  detection. The rate adapts: every 250 ms on a fast machine, backing off to
  about 2.5 times the measured detection time, up to 1 second, on a slow one.
  The first 3 frames are left out of the timing average, because the first GPU
  frame includes shader compilation (seconds, not milliseconds). The Stage
  summary shows the detection time, the delegate, and the current rate, so any
  machine's headroom can be checked by opening the page on it.
- **On stage:** the person's *feet* (the bottom center of their box) are inside
  the stage area, their box is at least `minHeight` of the frame, and they've
  stopped: moving slower than `maxDrift` over the last half second, measured
  from the newest position at least that old (`MOVEMENT_WINDOW_MS`), so
  detection timing can't hide a walker. A person walking past moves faster.
  The camera is a wide 720p webcam about 6 feet from the audience and sees well
  past the stage, so size alone can't say who's on stage; where they stand can.
  The stage area (`zoneTop`, `zoneBottom`, `zoneLeft`, `zoneRight`: the floor
  where feet go) is drawn by dragging a rectangle on the pumpkin page's camera
  view, or set with sliders on either page. Crossed edges are read either way
  round, so sliders can't empty the stage.
- **Identity:** a detection continues a known person only if it overlaps them
  or is near them *and* looks the same. `Scene` gives each box an appearance
  signature (the average color of its top and bottom halves, from a 96-pixel
  copy of the frame), and `maxLookChange` caps how different it can be. Without
  this, a new group stepping into the old group's spots inherited their
  identities and the show never ended. The colors are scaled so the whole
  picture averages mid-gray (`eyes/picture.js`): at night everything is near
  black, and unscaled, everyone looked alike.
- **Cuts:** when the picture changes by `CUT_CHANGE` (0.6) between detections,
  measured after evening out each frame's brightness and contrast
  (`frameChange` in `eyes/picture.js`: about 1 for two unrelated pictures, a few
  hundredths for the same scene, day or night; frames are averaged in 4x4
  blocks first so night sensor noise can't fake a cut), the whole picture changed at once, as when a virtual webcam
  switches images or the camera is bumped. `Stage.update(..., { cut: true })`
  forgets everyone and ends the show immediately; the people now on stage start
  the next one. Real people walking in and out never change that much of the
  frame at once. The raw pixel difference this replaced never fired on dark
  scenes: a full swap between two night pictures measured 0.04 to 0.09.
- **Measurements:** `measure(track)` gives each person's feet-to-head height (box
  height), feet position, and `whole`: whether the head and feet are both in
  frame. A box touching the top or bottom edge has no real height.
- **Shows (`eyes/stage.js`):** someone on stage starts a show at once
  (`startMs` 0; on the night it went from 2.5 seconds to about half a second,
  since people pass by fleetingly). The look's filler is a looking sound
  (`LOOKING_SOUNDS`: "Hm.", "Let me see."), so nobody stands in silence while it
  works out who's there; the hello is the pumpkin's own first line, with three
  of `HELLOS` ("What do we have here?", "Welcome, traveler.") as examples. The
  filler used to be a hello too, and every look played one, so an arrival got
  two or three greetings in a row.
  Everyone on stage at that moment is the *cast*; latecomers join the audience
  without restarting anything. A cast member only needs to stay close and in
  front, so someone hopping or gesturing still counts, and a cast member's
  track is kept for `castGoneMs`, so someone hidden behind another person for a
  couple of seconds returns as themselves rather than as a newcomer. The show
  ends when less
  than half the cast has been on stage for 4 seconds (a member counts as gone
  after 3 seconds off stage). Anyone left on stage then becomes the next show's
  cast.
- **Relay:** each show start or end posts to `/eyes-scene`. The server sends a
  system message into the live call (`SCENE_MESSAGES`), using the call ID the
  pacer follows: the call whose `session_start` came last (a reloaded page's
  old call can linger for a minute, still sending events, and the page also
  hangs up on `pagehide`). An "arrived" also resets `global_data.visitors`, so a
  new group is never greeted in the last one's costumes. It goes through the pacer's one ordered
  queue (`VisitPacer.tell`), like the moods and the operator's switch, so an
  "arrived" right after a "left" can't land first and lose its `seen_before`.
  Posts are retried up to 3 times, go out one at a time in order, and each
  waits (up to 10 s) for the pumpkin to stop speaking, read from the mic gate's
  `ducked` flag: a message into the call cuts off whatever it's saying, so a
  show used to start mid-word. More cases:
  - **"left"** ends the visit in the visit state and clears `stage_hint` and
    `seen_before` (`NO_GROUP_HINTS`). It's held for
    `LEFT_GRACE_SECONDS` (8) first, and an "arrived" in that time cancels it
    and is treated as the same show (only a small child among them sends
    anything, `JOINED_WITH_CHILD`). In the dark the detector loses a person for
    a few seconds or takes them for someone new, and a lone visitor standing
    still got a new show every 15 seconds. Someone who *doesn't* look like the
    group whose "left" is held (`same_people`, the appearance signatures
    `returning_visitors.py` matches with) is a new group: the old visit ends at
    once and the new one starts. A second "left" with no visit running says
    nothing.
  - **"arrived"** starts a visit in the visit state (gentle if the learner
    judges a small child or the operator chose Gentle just before). A
    re-announcement after a redial is the same visit.
  - **"arrived" during a visit** means the last "left" was lost (say, during a
    restart): the server ends the old visit and starts the new one
    (`ARRIVED_DURING_VISIT`). On Halloween night, before the server owned the
    state, one missed `visitors_left_gently` kept every later group in gentle
    mode for 50 minutes.
  - **"joined"** comes from `stage.js` when someone steps up mid-show, with
    everyone on stage measured, and only if most of the cast has been seen
    since the newcomer stepped up (so it works at any detection rate): after a swap, the new group used to be
    reported as joining the old show, sending "go gentle", then "left", then
    "arrived" within seconds, each interrupting the last. Only if the learner judges a small child is
    anything sent (`JOINED_WITH_CHILD`); otherwise newcomers stay audience.
- **Page API hygiene:** POSTs to the page APIs must be `application/json`
  (another site can't send that without asking first), and call IDs are
  matched with `fullmatch`.
- **The page panel:** a camera view with the shaded stage area, boxes (orange:
  cast, yellow: audience, grey: passersby), a dot at each person's feet, their
  height when whole, a show summary, and the stage sliders.
  `window.pumpkinScene` is exposed for poking at from the console on site.
- **Fallback:** if the detector fails to load, the call still works; the
  pumpkin notices people when they speak.
- **Greeting first:** the "arrived" message and the idle step tell the pumpkin
  to acknowledge new people at once with a short line that suits anyone, a
  small child included (`FIRST_LINE`: never a sneer, overriding the act's
  "every line has teeth", since the age check hasn't run yet), and to look and
  call `notice_visitors` in the same reply, so nobody stands in silence while it looks. Costume remarks come after.
- **Quiet time:** the quiet-time prompt (`attention_timeout`, 30 seconds) only
  mutters. It no longer goes looking, because arrivals come from the detector.

## Returning visitors

Each show starts with a clean conversation, so the pumpkin can't remember a
group on its own. `returning_visitors.py` does it from the stage camera:

- **What's kept:** for each arriving group, every cast member's appearance
  signature and feet-to-head height (`measure()` in `stage.js` includes the
  signature as `look`), plus the costumes the pumpkin reported in
  `notice_visitors`. In memory only, for 20 minutes.
- **A match:** a person matches when their signatures differ by at most
  `SAME_LOOK` (0.06) and, when both are whole, their heights by at most 20%. A
  group is back when at least one person and at least half of the new group
  match an earlier group. The newest matching group wins.
- **Telling the pumpkin:** `/eyes-scene` sets `global_data.seen_before` on
  every arrival (and resets it on "left"). The `greet` and `reassure` steps
  read it and open with "You again?" or "You came back!"
- **Limits:** color signatures can't tell two people in the same plain
  clothes apart, so a stranger in a gray shirt and jeans may be "remembered".
  The pumpkin playing along with that is in character.

## Tonight's costumes

Each show's conversation is hidden from the next, so the pumpkin can't say what
it has seen tonight from memory. `notice_visitors` appends each group's
costumes to `costumes.jsonl` in the call-log volume (so a reload mid-evening
keeps them), and `recall_costumes` reads back the last 12 hours, newest first,
without repeats. The `consult_memory` topic `costumes` is the pumpkin's
opinions of costumes in general; tonight's go to `recall_costumes`.

## Quality-control monitor

With `QC_MONITOR=1`, the agent's SWML attaches SignalWire's `ai_sidecar` after
`answer` and before `ai` (`add_post_answer_verb`). The sidecar is a second AI
on the call that never speaks and never sees the pumpkin's prompt: its own
prompt (`qc_monitor.SPEC`) is the character's spec as a reviewer would know it.

- **Per visitor turn** (the sidecar's tick fires when the visitor stops
  talking), it calls `predict_answer` with what it expects the pumpkin to say,
  before the reply exists, then `review_answer` once the reply is in the
  transcript, with a verdict (`as_expected` … `broke_character`, `too_long`).
- **Tools:** the sidecar hears only speech, so `_debug_event` tells it about
  each tool the pumpkin uses (not looks) with `calling.ai_sidecar.poke`; it
  answers with `review_tool` (`expected` … `wrong_tool`). Pokes go out one at a
  time, 4 seconds apart (`qc_monitor.Poker`): in the first live test, tools used
  close together were never reviewed. A failed poke is logged as `poke_failed`,
  and one more than 20 seconds old is dropped (`poke_dropped`).
- **Pairing:** the sidecar's tick fires when a visitor stops talking, before
  the pumpkin answers, so the latest reply answers the visitor's *previous*
  line. `review_answer` takes `in_reply_to` (stored as `visitor_said`) so a
  review pairs with its prediction; reviews used to land one line late.
- **Mode and pace:** on every real `context_change`, `_debug_event` pokes a
  `[MODE]` note (and logs `mode`): the act, gentle mode (nobody in the group is a
  target, nothing scary, the pumpkin's own voice stays), or an empty yard. At the
  shoo point it pokes a `[PACE]` note, so the monitor knows when shooing is
  expected. Its prompt also says the server decides arrivals, departures, and
  mode, so a switch arriving on a tool isn't graded as the model's choice.
- **Verified on a live call:** the sidecar hears the pumpkin's own voice (it
  quoted replies in `review_answer`). Its prompt tells it which voice applies
  after `become_gentle` and `resume_act`, and to review the latest reply on
  every turn, successes included.
- **Report-only:** `act_on_channel: false`, so nothing it does can change the
  call. Its tools post to `/qc/swaig` and its events to `/qc/events`, both with
  the same basic-auth pair SignalWire uses for the agent.
- **Findings** go to `qc/qc-YYYY-MM-DD.jsonl` in the call-log volume, served at
  `/eyes-qc` and shown at `/eyes/qc.html` (problems first). `qc_report.py`
  turns them into Markdown grouped by problem, each with the visitor's words,
  the expectation, and the pumpkin's reply.
- **Unverified on a live call:** whether the sidecar's `local-caller` leg
  carries the pumpkin's voice (the reviews need it), and the exact shape of its
  tool requests (`qc_monitor.tool_call` accepts both the `ai` verb's
  `argument.parsed` and plain `arguments`). Check `/eyes/qc.html` after a test
  call. Also unverified: two tools used
  in one reply send two pokes at once; watch for missing or doubled tool
  reviews.
- **Cost:** billed per minute while attached, which is the whole evening.

## Phone remote

`/eyes/remote.html` is an iPhone-sized page to start and stop the show and
adjust every setting from anywhere. The pumpkin page holds the call and the
settings, so the server only relays, through `remote_control.py`, in memory:

- **Commands:** the remote posts commands to `/eyes-remote/command`: `run`
  (start or stop), `set` (a setting), or `reset` (a slider group). The server
  queues one more kind itself, `restart` (see "The server owns the state"). Each gets
  a sequence number. The pumpkin page polls for commands newer than the last
  one it applied, every second. A freshly loaded page gets the current number
  but none of the backlog, so a reload never replays old changes over its saved
  settings.
- **Status:** the pumpkin page posts its status to `/eyes-remote/status`
  every second: armed, running, call status, stage summary, mic level, and
  every setting. The remote shows it and treats more than 5 seconds of silence
  as the pumpkin's computer being gone.
- **Start and Stop:** Stop hangs up, interrupts a dial still connecting, and
  holds the call loop until Start. Start needs the page to be *armed*: someone
  must click Start on the pumpkin's computer once per page load, because Chrome won't
  play audio before a click on that page. Until then the remote's button is
  disabled with a hint.
- **Sliders:** both pages render sliders from `eyes/controls.js`. On the
  pumpkin page, `addGroup()` registers each group (effects, gate, stage), and a
  slider move, a reset, or a remote command all go through the group's `set()`
  or `reset()`. The remote leaves a slider alone for 2.5 seconds after it's
  touched, so incoming status doesn't jump it back.

### Gentle / Normal switch and the small-child learner

The remote's "This group" card shows the mode the server wants for the current
group (from the visit state) and has a Gentle / Normal switch.

- **A press sets the visit state's operator choice**, and if the pumpkin isn't
  in that mode yet, sends "the operator says … call become_gentle" or "… call
  resume_act" to bring the tool call that carries the switch.
- **The operator's choice holds for the group in front.** A person's judgment
  overrides the automatic checks at any time during a show and for the rest of
  that group's visit: after Normal, the age gate, a small child measured
  joining, and the model's own `become_gentle` all leave the act in place;
  after Gentle, only another press brings the act back. The choice is
  forgotten when the visit ends. A press with nobody there applies to a group
  arriving within 20 seconds (`visit_state.OPERATOR_AHEAD_SECONDS`). The remote
  keeps showing the operator's choice ("Switching to…") until the mode catches
  up, for up to 20 seconds.
- **Two measurements** (`child_model.measure_group`), whole bodies only: the
  *ratio* (shortest over tallest) and the shortest person's feet-to-head
  *height* as a fraction of the frame. Everyone on stage stands at about the
  same distance, so a learned height applies anywhere on the stage, and to a
  lone visitor; the ratio needs a group.
- **Learning the height:**
  - **Automatic examples:** arriving groups with a large height difference
    (ratio ≤ 0.5) give their shortest as a small child and their tallest as
    not; near-equal groups (ratio ≥ 0.9) give their shortest as not. An
    arrival within 10 seconds of a "left" (`SAME_GROUP_BOUNCE_SECONDS`) is
    taken as the same group drifting at the stage's edge and adds none, so
    one family can't train the height by itself.
  - **Operator examples:** every switch press saves the current group's
    measurements (the pumpkin page's live cast if fresh, otherwise the last
    arrival within two minutes; "left" clears it) at weight 3.
  - **Storage:** examples go to `child-labels.jsonl` in the call-log volume.
    Only numbers are kept, never images.
- **The cutoffs:** `fit_cutoff` minimizes weighted errors, with a missed child
  costing 3 times a needless gentle show; ties go to the higher (safer) cutoff.
  The height rule takes over once there are 6 height examples, with at least
  one of each kind.
  Until then the ratio rule (0.6, or learned from operator presses) decides.
- **On arrival:** if the judged rule says small child, the "arrived" message
  tells the pumpkin to go gentle at once, and `stage_hint` carries the
  measurement and verdict to the `notice_visitors` gate.

The learner treats "Gentle" as "has a small child." Choosing Gentle for some
other reason, like a scared adult, teaches it something slightly wrong.

## Vision

- **Turning it on:** `enable_vision` and `vision_model` in `params`, plus an
  internal filler for `get_visual_input`. The function is the platform's own;
  don't register one with that name.
- **Step function lists:** the platform switches off any function a step's list
  doesn't name, and the SDK refuses names outside its fixed built-in set.
  `pumpkin_agent.py` adds `get_visual_input` to
  `sdk_contexts.RESERVED_NATIVE_TOOL_NAMES` so steps can list it. Without that,
  the pumpkin can never look. Leaving it *out* of a list doesn't stop it,
  though, and neither does `toggle_functions`: on test calls the pumpkin
  looked 20 times in the act, whose steps don't list it, with the toggle
  switched off. What keeps looks harmless is `explain_blindness`, which says
  the eyes are fine when the group was seen in the last few minutes.
- **What to look for:** `get_visual_input` takes a `request` string that the
  model writes, and that string is the vision model's prompt. "Your Eyes" tells
  the model to ask about the people standing close and facing it: how many,
  their relative heights, what each is wearing, and whether anyone looks
  scared. The server doesn't see those frames. It could see some: a SWAIG
  function with `visual_input` receives the caller's newest frames in its
  request, which would let a tool look for itself. Nothing uses that yet.
- **Character:** prompt, conscience, and `explain_blindness` all frame looking
  as the pumpkin's own carved eyes. It never mentions video, cameras, or
  settings.

## The pumpkin page (`eyes/`)

- `client.js`: the call loop. A custom credential provider fetches
  `/eyes-token` (guest token plus destination) and refreshes it. The page waits
  for `client.ready$` before dialing. If dialing doesn't start within 30
  seconds, it rebuilds the SignalWire client in place; a page reload would lose
  the audio unlock. Backoff runs 5 to 60 seconds.
- **Media:** the page opens the mic itself through a noise gate, and the camera
  itself too.
- `noise-gate.worklet.js`: a mic gate with threshold and hold, plus a level
  meter. Auto gain control is off, so a quiet room doesn't raise the background
  toward the threshold. Its second input is the pumpkin's own voice (tapped
  from the effects chain's output): while that is above `duckDb` (−50 dB), plus
  `duckHoldSeconds` (0.3 s), the mic stays muted and can't arm the gate's hold.
  That keeps the speaker's sound out of the recognizer even where the
  browser's echo cancellation can't remove it, at the cost of visitors not
  being able to interrupt mid-sentence. The meter turns blue while muted, and
  the page reports whether the browser actually enabled echo cancellation on
  the mic (`echoCancellation` in the status, shown on the remote). A preview
  clip goes through the same chain, so it mutes the mic during a call too;
  that's deliberate, since the mic would hear it.
- `voice.js` and `pitch-shifter.worklet.js`: the pumpkin's voice. The custom
  shifter (−3 semitones) feeds Tone.js chorus (30% mix), a lowpass (6 kHz), and
  reverb (1.0 s, 20% wet). Sliders adjust these live and save to
  `localStorage`.
- `calls.html`: the call log viewer.

### Browser-side gotchas, each found the hard way

- **Supply both media streams.** Once the Browser SDK (`@signalwire/js`
  4.0.0-rc.4) is given `inputAudioStream`, it sends only the streams it's given
  and ignores device constraints. The camera must be passed as
  `inputVideoStream` too. Getting this wrong left every call silently without
  video.
- **Tone.js `PitchShift` is broken for downward shifts.** It computes the sweep
  rate as `2^((n-1)/12)+1` instead of `1-2^(n/12)`. Use
  `pitch-shifter.worklet.js`.
- **Load worklets with `loadWorklet()` in `voice.js`.** Tone's
  `addAudioWorkletModule` only ever loads the first module URL it's given.
- **Don't rely on browser echo cancellation alone.** The pumpkin's voice plays
  through Web Audio (the effects chain), and depending on the browser version
  and audio setup that output isn't always part of what echo cancellation
  subtracts; some USB mics don't honor the constraint at all. Hence the gate's
  mute-while-talking input.
- **Force keyframes for the pumpkin's vision.** Chrome sends one keyframe
  when the call starts and then only changes, unless the far end asks for
  another. With a still or slow-changing picture the platform's vision was left
  describing an old frame, or nothing. `video-sender.js` records the SDK's peer
  connections (it must be imported before the SDK) and nudges the video
  sender's resolution by 1%, which makes Chrome encode a fresh keyframe: when
  `Scene` sees the picture change by 3% or more (at most once a second), and
  every 1.5 seconds while running. Each sender's original scale is remembered, so
  a failed undo can't shrink the picture over time. Its `videoStats()` (fps, keyframes sent, PLI/FIR requests from the
  far end) goes out in the remote status as `video`.
- **Keep the remote stream on a muted `<audio>` element.** Chrome needs that
  before Web Audio receives remote WebRTC audio.
- **A call that ends after working redials at once.** The model can end the
  call with the platform's built-in hangup (it did after a child said "Good
  night", and again on a test call after "Bye bye"; no documented setting
  removes it); the prompt says never to, and `runForever` skips the backoff when the
  call ran at least 30 seconds (`HEALTHY_CALL_MS`), so an agent that drops calls
  right after connecting still backs off.
- **Rebuild the client after a failed start.** A client whose startup sign-in
  failed never becomes ready, and `dial()` waits forever.
- **Real timers wander.** At 250 ms, a timer fires anywhere from about 190 to
  310 ms apart. Checks like "still for a second" must measure over the
  person's whole history, not from the oldest sample inside the window. The
  stage tests use jittered ticks for this reason.

## SignalWire setup (`signalwire_setup.py`)

`.env` needs only `SIGNALWIRE_SPACE` (the full hostname), `SIGNALWIRE_PROJECT_ID`,
and `SIGNALWIRE_API_TOKEN` (Voice scope). On startup, in the background:

- Find the SWML webhook resource named `scarecrow`, creating it or updating it
  to point at `{SWML_PROXY_URL_BASE}/pumpkin` with basic auth.
- Read the resource's address and dial its video channel.
- `/eyes-token` mints guest tokens limited to that address.

The basic-auth password is derived from the API token with an HMAC, so it stays
the same across restarts and a call in progress survives a code reload.

## Deploying

- **Update with a pull on the host.** The checkout is the code the container
  runs.
- **Only infrastructure changes need a rebuild.** That means changes to
  `Dockerfile`, `docker-compose.yml`, or `.dockerignore`
  (`docker compose up -d --build`). Any other change leaves the container
  running. The checkout is mounted at `/app` (read-only), `watchfiles`
  restarts only the Python process when a `.py` file or `surroundings.txt`
  changes (`reload_filter.py`), and `eyes/` is read on every request. The prompt
  is fetched once per call, so a surroundings change reaches the pumpkin on its
  next call: Stop and Start on the remote.
- **Dependency changes need a rebuild too.** The `# /// script` block is
  installed into the image, so changing it means `docker compose up -d --build`.
- **Confirm the new code is running.** `/version` returns the commit the
  process started from; a crash on start leaves the old commit answering.
- **Config:** `.env` lives only on the host. The page's `config.js` is written
  at startup to a temp directory and served at `/eyes-config/config.js`, because
  the code mount is read-only.

## Routes

| Route | Auth | Purpose |
|---|---|---|
| `/pumpkin` (plus `/swaig`, `/post_prompt`, `/debug_events`) | Basic auth (and per-call tokens) | The agent, for SignalWire |
| `/eyes/`, `/eyes/calls.html` | `EYES_PASSWORD` if set | pumpkin page, call log viewer |
| `/eyes/remote.html` | `EYES_PASSWORD` if set | Phone remote |
| `/qc/swaig`, `/qc/events` | Basic auth (the SWML pair) | The QC sidecar's tools and events |
| `/eyes-qc`, `/eyes/qc.html` | `EYES_PASSWORD` if set | QC findings |
| `/eyes-live?after=N&day=YYYY-MM-DD` | `EYES_PASSWORD` if set | Live events from line N, today's or a given day's. Days roll over at UTC midnight (late afternoon in the Americas), so one evening spans two files. With isolated contexts, the end-of-call report only holds the last group's conversation; the live log is the full record. |
| `/eyes-token`, `/eyes-scene`, `/eyes-remote/…` (including `/eyes-remote/mode`), `/eyes-calls`, `/eyes-calls/{id}`, `/eyes-config/…` | `EYES_PASSWORD` if set | Page APIs |
| `/version`, `/health` | none | Checking which code is running, and that it's up |

`EYES_PASSWORD` should be set: `/eyes-token` starts billed calls.

## Call log

`report_call` saves each post-prompt report to `CALL_LOG_DIR` (the `call-logs`
Docker volume, at `/data/call-logs`) and logs a summary: functions called, what
each look returned, the semantic gate tally, and any assistant line matching
`OFF_CHARACTER` (video, camera, "your eyes", …). `/eyes/calls.html` shows the
list and transcripts. A report arrives only when its call ends, and the
pumpkin page's call is long, so reports come in batches.

## Testing

No call needed:

```sh
uvx --from signalwire-sdk --with requests swaig-test pumpkin_agent.py --list-tools
uvx --from signalwire-sdk --with requests swaig-test pumpkin_agent.py --dump-swml --raw | jq .
uvx --from signalwire-sdk --with requests swaig-test pumpkin_agent.py --raw --exec notice_visitors --youngest young_child --anyone_scared false
uvx --from signalwire-sdk --with requests swaig-test pumpkin_agent.py --raw --custom-data '{"global_data":{"gentle":true}}' --exec decline_to_answer --request touch_or_come_closer
```

Running locally: `./pumpkin_agent.py` with `SWML_BASIC_AUTH_*` set (or the three
`SIGNALWIRE_*` variables plus `SWML_PROXY_URL_BASE`) serves on port 3000.

**Stage logic:** `node tests/stage.test.mjs`, which runs scripted scenes with
jittered timing: a passerby, a distant person (feet above the stage), someone
off to the side, a show with a latecomer, an occlusion, the cast leaving, a
cast member hopping, a body cut off by the frame, a new group in the old spots,
a camera cut, the stage's bottom edge, and a machine detecting once a second.

**Scene changes end to end:** build a video that switches photos partway
(`ffmpeg -loop 1 -t 10 -i a.jpg -loop 1 -t 30 -i b.jpg` with a `concat`
filter, written as `.y4m`) and give it to Chrome as its camera. The show
should end at the switch and a new one start about 3 seconds later.

**The visit state and the replay:** `uv run tests/test_visit_state.py` covers
corrections, the ratchet toward gentle mode, leaving only on the stage's word,
stuck and repeating restarts with their rate limit, and alarms, plus the replay
on a small made-up night. To replay a real one, save `/eyes-live?after=0` (or
the day's `live-*.jsonl`) to `sim/data/` and run `sim/replay.py
sim/data/<file> --events`. The model is replayed, not simulated: its recorded
tool calls stand for what it would have tried, so the replay measures the state
logic, not the conversation.

**Pacing:** `uv run tests/test_visit_pacing.py`, covering the mood arc, what
counts as an exchange, gentle mode mid-visit, and the end of a session.

**The learner:** `uv run tests/test_child_model.py`, covering measuring,
weighted fitting, automatic examples learning the height, judging a lone
visitor, and reloading.

**Stage detection end to end:** give Chrome a Motion-JPEG file as its camera
(`--use-file-for-fake-video-capture=people.mjpeg`; a JPEG photo repeated is
enough), click Start, and read `#stage-summary`. A photo of a few people
standing close should start a show within a few seconds.

**Remote end to end:** two Playwright pages against one local server: the
pumpkin page (fake devices) and `remote.html` at iPhone size. Check Start
on the pumpkin page arms the remote, then phone-to-page settings, Stop and
Start, Reset, page-to-phone settings, and no replay after a reload.

**Page testing:** headless Chrome via Playwright with
`--use-fake-ui-for-media-stream --use-fake-device-for-media-stream`, against
`http://localhost:3000/eyes/`. On macOS, a Playwright-driven browser never
finishes loading an audio worklet (even a two-line one), so the page stalls
after Start; test the call itself in a normal Chrome window instead.

**A real call with a fake audience:** open a separate Chrome profile on the
pumpkin page with a camera file and a mic file, press Start, and read the run
back from `/eyes-live`:

```sh
open -na "Google Chrome" --args --user-data-dir=/tmp/pumpkin-test --no-first-run \
  --use-fake-ui-for-media-stream --use-fake-device-for-media-stream \
  --disable-features=AudioServiceSandbox,AudioServiceOutOfProcess \
  --use-file-for-fake-video-capture=camera.mjpeg \
  --use-file-for-fake-audio-capture=visitors.wav https://pumpkin.example.com/eyes/
```

The camera file is an MJPEG built from still pictures (an empty yard, groups,
a small child with a parent) with ffmpeg's concat demuxer; the mic file is
spoken lines (`say -o`) placed on a timeline with `adelay` and mixed, as 44.1
kHz 16-bit WAV. Without turning off the audio service's sandbox, Chrome on
macOS never reads the WAV and the fake mic is silent (−180 dB on the page's
meter). Chrome plays the MJPEG faster than its nominal rate and loops it, so
scenes don't keep to a clock. The page opens a fresh camera and mic for each
call, so a restart starts both files over. It bills AI time for the whole run.

**Audio effects:** render offline with `Tone.Offline` and measure. A 220 Hz
tone should come out near 185 Hz at −3 semitones; ~183 Hz is within the
shifter's sideband spacing.

**End-of-call reports:** the post-prompt endpoint needs the per-call `__token`.
Get it from the `post_prompt_url` in SWML fetched with a `call_id`, then post a
sample payload to it.

## Conventions

- **Keep this file current.** After any big feature change, update CLAUDE.md in
  the same change: layout, how the parts fit, gotchas, and testing.
- **Keep DECISIONS.md current.** When a design choice is made or reversed, add
  or update its entry in the same change: what was decided, why, how it
  evolved, and what the pattern does in a business agent.

- **Keep it generic.** This is a published example: no details of any one
  deployment (hostnames, server paths, accounts, a particular room or device)
  in code, comments, UI text, or docs. Deployment specifics belong in `.env`, with
  neutral placeholders like `pumpkin.example.com` in the docs.
- **Prompt text** is written like instructions to a person: positive and
  specific. Function descriptions and results are prompt text too.
- **Stay in character:** never add anything that makes the pumpkin acknowledge
  being a machine, a camera, or video.
