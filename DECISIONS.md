# Decision log

The pumpkin is a SignalWire voice AI agent that plays a burning jack-o'-lantern
on a scarecrow in a front yard on Halloween. A computer hidden in the prop
holds a video call to the agent, so the pumpkin can see and talk to
trick-or-treaters. (README.md covers setup; CLAUDE.md covers how the parts
fit.) This log records why it's built the way it is, and how those choices
changed as it met real and test visitors. The "In a business agent" line in
each entry says what the same pattern does outside a front yard. Keep this
current: when a design choice is made or reversed, add or update its entry in
the same change.

Terms used throughout:

- **The act**: the pumpkin's normal persona, sarcastic and imperious, secretly
  wanting company.
- **Gentle mode**: its warm persona for small or scared children.
- **A show** (or visit): one group's time in front of the pumpkin, from
  stepping up to walking away.
- **The stage**: the patch of ground in front of the prop where visitors stand.
  A person detector in the prop's web page watches it (the "stage camera").
- **The operator**: the person running the prop, with a phone remote that can
  start and stop the show and switch a group between Normal and Gentle.
- **SWAIG functions**: the tools the model calls, which run on the agent's
  server.

Each entry: **Decision**, **Why**, **How it evolved**, **In a business agent**.

---

## 1. A flow, not one big prompt

**Decision.** The character lives in layers: a base prompt (who it is, how it
talks, lines it holds), two contexts (`default`, the act; `gentle`), steps
inside the act (idle → greet → banter → shoo → farewell), SWAIG functions that
return guidance, and data the server pushes in. No single prompt describes the
whole night.

**Why.** One prompt can't hold "be rude, but not to small children, but shoo
after four turns, but never mention cameras" and keep all of it every turn.
Splitting it means each piece is small, each rule lives where it applies, and
the code can enforce what the model can't be trusted with.

**In a business agent.** The same split separates persona, workflow stage, and
policy: a support agent's greeting, verification, and resolution steps each
carry their own instructions and their own tools.

## 2. Step function lists decide what the model can even try

**Decision.** Every step calls `set_functions` with the exact tools it allows.
`notice_visitors` only exists in idle and farewell; the shooing step has no
storytelling or costume tools; gentle mode has no `notice_visitors` and no way
to start the act.

**Why.** A model can't misuse a tool it can't see. Steps also inherit the
previous step's list if they don't set one, which was a source of surprises.

**How it evolved.** The platform's own `get_visual_input` was switched off by
these lists (the SDK refuses names outside its fixed built-in set), so the
pumpkin couldn't look at all until `get_visual_input` was added to
`RESERVED_NATIVE_TOOL_NAMES`.

**In a business agent.** Least privilege per stage: no refund tool before
identity is verified, no account changes in a sales flow.

## 3. Gentle mode is a one-way door (a compliance gate)

**Decision.** The model can enter `gentle` (by `become_gentle`, by a gate's
failure, or by the server's "small child" message), but nothing it can call
brings the act back. `resume_act` exists in gentle mode, yet does nothing
unless the operator's Normal switch on the phone remote chose the act for this
group (the server's visit state holds that choice; see 22).

Gentle mode is not a different, sugary character. It's the same grand, vain
pumpkin with two limits: nobody in the group in front is ever the target (not
the child, and not their grown-ups, whose costumes a child hears mocked as an
attack on their family), and nothing it says is frightening. It can still joke
about itself, the staff, the yard, and the costumes it saw earlier tonight:
"Easily the best princess tonight. The last four were, frankly, an
embarrassment to the crown." The model is a children's show that works on two
levels, with jokes for the grown-ups that never land on the child.

**Why.** The point of gentle mode is that the pumpkin never mocks or scares
anyone in a group with a small child. If the model could decide it was "probably fine" to
resume the act, it eventually would. Inside `gentle`, the act's prompt isn't
even present; the model doesn't know that path exists.

**How it evolved.**
- The farewell step could hand a new group straight to gentle mode without
  passing idle, so the gentle context now hides earlier conversation on entry.
- The always-on `conscience` text said "dismissive, imperious" after every
  function call, which leaked the act's voice into gentle mode. It's now
  mode-neutral ("the current mode says which character").
- Gentle mode was a context, but the tools chose their wording from a
  `global_data.gentle` flag. After a lost context switch the flag drifted, and
  a child got the act's costume insults. The flag is gone: gentle mode has its
  own copies of the tools whose wording differs, and only those copies are
  listed in gentle mode, so the act's wording can't be reached there at all.
  Lesson: enforce a mode by what's reachable in it, not by a variable the
  code consults. A single tool reading the context isn't an option: tool
  requests don't carry the current context or step, only the call, its
  `global_data`, and session details. Per-context tool lists are the
  platform's way to know where you are.
- An ungated "they left" function (`visitors_left`, see 9) could have let the
  model exit gentle mode on its own; the server refuses it unless the stage
  camera says the group has gone.
- The first gentle mode was written as "drop the act completely… warm and
  kind", and the QC monitor graded it as "the warm voice". The pumpkin lost its
  character with families, and the monitor flagged kind goodbyes to small
  children as off-character. It's now defined by its limits (who can't be a
  target, nothing scary), not by a change of voice.
- A one-way door needs a reliable way out, or it traps the next person too.
  On Halloween night the pumpkin missed one `visitors_left_gently` (a "left"
  and the next "arrived" landed seconds apart, and it answered only the
  arrival), and every group for half an hour got the warm voice:
  `notice_visitors` isn't reachable in gentle mode, so nothing new could
  start. The first fix had the server re-ask at the next arrival; the lasting
  one is that the server owns the visit and the next tool call of any kind
  corrects the mode (22). Lesson: when the exit depends on the model, it isn't
  an exit; make it one in code.

**In a business agent.** Compliance-sensitive modes (a minor on the line, a
regulated disclosure, a fraud hold) that the model can enter on suspicion but
only a person or a verified system event can leave.

## 4. A human override that always wins

**Decision.** The phone remote's Gentle/Normal switch overrides every automatic
decision, at any time in a show, for the rest of that group's visit. After a
Normal press, anything automatic that sends the group gentle (the age gate in
9, a "small child" message, the model's own call) is undone at once. The choice is
forgotten when the group leaves, so it can't reach the next group.

**Why.** The automatic checks are judgment calls (heights, a vision model's
guess at age). A person standing there has better judgment, and must not have
to fight the system.

**How it evolved.**
- First, Normal only worked if the group was already gentle; pressed a moment
  early (before the age check landed), it was ignored and the pumpkin went
  gentle anyway. A 20-second hold fixed that race.
- A code review found the hold could spill onto the next group, so it was
  narrowed to the start of a show.
- That was the wrong direction: the operator needs to override at any moment.
  It now holds for the whole visit and is cleared by the visit ending, not by a
  timer.

**In a business agent.** Supervisor barge-in and override that the automation
defers to, scoped to the current interaction so it doesn't leak into the next.

## 5. Server-side pacing: a state machine the model can't argue with

**Decision.** The server counts exchanges from the platform's debug events
(one exchange is the visitor speaking, then the pumpkin replying) and pushes a
`visit_mood` into the call with `calling.ai_message(global_data=...)`, which
the base prompt's "Right Now" section reads. At the shoo point it also says a
one-time "they've stayed long enough" message into the call, so the switch
happens on that turn. The arc: a question of its own is allowed only in the first
exchange, statements after that, and from the third each reply shoos harder.
Gentle mode never asks and sends them off from the second.

**Why.** The character secretly wants company; left alone, it never ends a
conversation, and the line of trick-or-treaters backs up. Models also can't
count their own turns reliably. The server can.

**How it evolved.**
- The first act prompt said "shoo them", so it shooed from the first line.
  Shooing moved entirely into the moods ("don't send them away until Right now
  says").
- Then it asked a question every turn, which keeps people talking. Questions
  were limited to the first two exchanges (later, the first one only).
- Gentle mode copied the act's arc and its "be warm and encouraging" wording,
  so it asked a talkative child a question every turn and the visit never
  ended. Gentle mode now never asks questions and sends them off from the
  second exchange; the act asks only in its first and shoos from the third.
- The count started only when the pumpkin entered its greet step. Once it
  chatted with a group from idle for a dozen turns without recording them, so
  no visit started and the "in no hurry" mood never changed. A visit now starts
  the moment a visitor speaks, whichever step the pumpkin is in, and the stage
  camera's "left" ends it directly.
- For days the mood sat in the step text, which the platform fills in once
  when a step is entered. Every mood pushed after that was silently ignored
  until the next step change, so a visitor could talk for dozens of turns and
  never hear a dismissal. The mood moved to the base prompt, and the shoo
  point is now announced into the call. Lesson: a `${global_data}` value in
  step text is a snapshot, not a live binding.
- Mood pushes went out on separate threads and could arrive out of order (a
  sterner mood overtaken by a softer one). Everything sent into the call now
  goes through one ordered queue, including stage events and operator messages.

**In a business agent.** Time-boxing: after N turns on a support line, steer to
resolution or escalation; on a sales call, move to the close. The rule lives in
code, and the prompt just reads its output.

## 6. Policy answers live in code (`decline_to_answer`)

**Decision.** Boundaries (touching, coming closer, swearing, personal details,
"you're an AI", leaving the yard, photos) go through one function with an enum
of request types. Each type returns exact guidance, cold in the act and warm in
gentle mode.

**Why.** Wording for boundaries shouldn't drift with the model's mood. A table
in code is reviewable and testable.

**How it evolved.**
- Photo requests were refused as "coming closer". The rope rule stays absolute,
  but photos are now granted from the visitor's side ("You may capture my
  likeness from there").
- The tool was renamed from `decline` to `decline_to_answer` to make its
  purpose obvious to the model.
- Gentle-mode lines lost leftover snark ("they are boring you").

**In a business agent.** Refusals and disclosures with legal wording: what the
agent says when asked for medical advice, account details of another person, or
anything outside policy.

## 7. Never "I don't know": a tool for the unanswerable

**Decision.** `tell_a_story` answers questions the pumpkin can't with a story
that starts on the question and wanders, one beat per reply, never reaching an
answer. `consult_memory` holds the backstory as deliberately contradictory
fragments, picked without repeats per visit.

**Why.** "I don't know" breaks the character. Keeping facts in a function (not
the prompt) makes them variable on purpose and keeps the prompt small.

**How it evolved.**
- "How old are you?" went to `decline` as "personal info"; the description now
  says questions about the pumpkin itself go to `consult_memory`.
- "What's your favorite costume?" kept going to the story tool even after its
  description pointed at `recall_costumes` (the QC monitor's first test caught
  it again). `tell_a_story` now hands such questions to `recall_costumes`
  itself: when a description isn't enough, the code routes.

**In a business agent.** A designed fallback for out-of-scope questions (a
graceful redirect or handoff) instead of a hallucinated answer, and reference
facts served by a tool rather than pasted into the prompt.

## 8. Referral to humans as part of the persona (the staff)

**Decision.** The people handing out candy are "the staff": candy, directions,
and complaints are referred to them, with a small complaint about their
standards (none in gentle mode).

**Why.** The pumpkin can't hand out candy. Making the referral part of the
character keeps it in voice and makes the handoff natural.

**How it evolved.** The bit faded as other prompt text grew and was brought
back as its own base-prompt section. Gentle mode briefly contradicted it ("the
nice folks inside", with a complaint) until the two were reconciled. Where the
staff stand is configuration (see 16), not code.

**In a business agent.** Escalation to a human queue as a first-class, on-brand
move, not an error path.

## 9. Verify with the camera before consequential actions (semantic gates)

**Decision.** `notice_visitors` has a `SemanticGate` that shows the platform's
decision model 3 camera frames before the act can start; if it can't confirm
everyone is past small-child age and nobody is scared, the gate's `on_fail`
switches to gentle mode before the handler runs. `visitors_leaving` has a gate
on 2 frames so the pumpkin doesn't say goodbye to people still standing there.

**Why.** The model's own read of the scene is a single guess; a gate is an
independent check by the platform at the moment of the decision, and it fails
in the safe direction.

**How it evolved.**
- Swapping test images made "they left" fail every time: the frames showed the
  next group, so the gate concluded nobody had left and the old group's state
  carried over. Departures the stage camera reports now use an ungated
  `visitors_left`, which the server only accepts within 20 seconds of a real
  stage report.
- The age gate asked "older than about eight" at 0.8 confidence and sent a
  group of school-age kids between two adults to gentle mode. It now asks about
  six, says a child at an adult's shoulder counts as old enough, defines a small
  child as about half an adult's height, and passes at 0.7.

**In a business agent.** Independent verification before irreversible steps:
confirm identity before a payout, confirm intent before cancelling, with the
check's failure path landing somewhere safe.

## 10. Fillers that cover the thinking

**Decision.** When the pumpkin looks (`get_visual_input`), an internal filler
plays immediately: a short sound like "Hm." or "Ahem." The voice engine doesn't
do sound tags, so the sounds are spelled out.

**Why.** Looking takes a second or two. Silence reads as broken; a throat-clear
reads as a pumpkin deciding whether you're worth its time. Since the stage
detector triggers the first look of every show, the filler also opens the show.

**How it evolved.** Waiting for the look before saying anything still left new
visitors standing in silence. On arrival (or when someone speaks first) the
pumpkin now acknowledges them at once with a line that would suit a small
child, and looks in the same reply; costume remarks come after. Because that
line comes before the age check, it's explicitly exempt from the act's "every
line has teeth" rule (`FIRST_LINE`): never a sneer, whoever is standing there.

On the night, shows were still slow to start: 4 to 12 seconds from the stage
reporting a group to the first word. The model always runs its tools before it
speaks, so the greet-first instruction never produced an early line. Cuts:
`notice_visitors` returns the costume verdict itself instead of a second round
through `appraise_costume`, gentle mode says a plain hello rather than looking
first when the costume isn't known, and the stage starts a show after 1.7
seconds of standing instead of 2.5. Then, since people pass by fleetingly, a
show starts once someone has stopped for half a second (the shortest span that
still tells a walker from someone standing), and the look's filler is a greeting
("Happy Halloween!") so they hear a hello at once. With three fillers the same
hello came round every few visitors, so `HELLOS` now holds nearly twenty short,
grand ones that suit anyone ("What's this? Humans?", "Welcome, traveler.").
Then a test call showed the cost of a hello as a filler: the filler plays on
every look, and the pumpkin also greets people itself, so each arrival got two
or three welcomes in a row. The filler is now a looking sound ("Hm.", "Let me
see.") and the greeting is the pumpkin's alone. Lesson: a filler is a sound for
waiting, and it plays every time; anything that should happen once belongs
somewhere else.

**In a business agent.** "Let me pull that up" while a lookup runs, in the
brand's voice, so latency feels like attention.

## 11. Push events from the edge instead of making the model watch

**Decision.** The page in the prop runs a person detector (MediaPipe, in the
browser) and decides who's "on stage": standing still with their feet in a
marked floor area. It posts "arrived" and "left" (and "joined" for latecomers)
to the server, which relays them into the call as system messages.

**Why.** On a busy street, heads change constantly, and asking the model to
watch would cost a vision call every few seconds and still miss things. What
matters is whether most of the people who started the show are still there,
which a headcount can't tell.

**How it evolved.**
- Stage by where feet are, not box size, because a wide camera sees far past
  the stage.
- Cast members survive short occlusions; a new group stepping into the old
  group's spots is told apart by appearance signatures (average colors of top
  and bottom halves), and a whole-picture change ends the show at once.
- A child joining mid-show was never measured, so "joined" events now carry
  everyone on stage and the server sends the gentle message if a small child is
  among them.
- Lost stage posts (for instance during a deploy's restart) are retried; an
  arrival during a stale visit tells the pumpkin to end the old one first.
- At night it missed audience changes entirely: in a near-black picture, a full
  swap of groups barely changed the raw pixels, and everyone's clothes measured
  as the same dark gray, so new people inherited the old ones' identities. Both
  measures now even out the picture's brightness first, as a camera's auto
  exposure would.
- Darker still, a lone visitor standing still kept restarting the show: the
  detector lost them for a few seconds, or their colors shifted far enough to
  make them a stranger, so the page reported "left" then "arrived" every 15
  seconds. Adding light wasn't an option mid-evening and the page couldn't be
  reloaded without someone at the prop, so the server now holds each "left"
  for 8 seconds and lets an "arrived" in that time cancel it. Lesson: debounce
  noisy edge signals where you can change them, and say what it costs (a quick
  swap of groups now reads as one show).

**In a business agent.** Deterministic signals from the systems around the
agent (a payment posted, a ticket closed, a customer joining a queue) pushed
into the conversation, instead of the model polling or guessing.

## 12. One call all night, and a clean slate for every visitor

**Decision.** The prop holds one long video call. Each show starts with no
memory of the last: the yard, the act, and gentle mode are each an isolated
context (`set_isolated(True)`), so entering one wipes the conversation. What the next step needs (what the group is
wearing, whether they've been here before) is carried in `global_data` and read
by `${global_data.*}` in step text.

**Why.** Redialing per group would add seconds of setup in front of every
visitor. But with one call, the model saw every earlier show: it repeated its
own lines and kept the name it had given someone an hour ago.

**How it evolved.**
- Hiding history also hid the pumpkin's own memory of repeat visitors, so
  recognition moved to the server (see 13).
- Reloading the page left the old call running beside the new one for a
  minute; the server now follows the newest call, and the page hangs up when
  it's closed.
- It used to be `set_history("hide")` on the idle and greet steps and the
  gentle context. Hiding kept earlier shows from the model, but not from the
  conversation's size: on Halloween night the call hit the platform's
  128,000-token limit after 38 minutes and dropped. Isolated contexts truncate
  the conversation itself; the replay of that night puts the largest
  conversation at about 5,000 tokens of dialogue instead of 44,000. Lesson:
  check what a setting actually removes, not what the model can see.

**In a business agent.** A shared line or kiosk serving many customers in one
session, where one customer's details must never surface for the next, with
explicit, minimal state carried across steps.

## 13. Memory outside the conversation

**Decision.** The server keeps what the conversation no longer can: tonight's
costumes (`costume_log.py`, for "what's your favorite costume?") and recent
groups' appearance and height (`returning_visitors.py`, for "You again?").
Both are plain data, never images.

**Why.** Once each show is a clean slate, anything that spans shows has to live
somewhere the model can ask for it.

**How it evolved.**
- The favorite-costume answer is built on three lines written by the person
  who designed the character;
  early in the night it uses the lines' own example costumes, then real ones.
- Stale costumes leaked into the next group's gentle-mode hello; every arrival
  and departure now clears them.

**In a business agent.** CRM-style memory: the agent recognizes a returning
caller and recalls past orders through a tool, rather than through a
transcript.

## 14. Learning from the operator, with asymmetric costs

**Decision.** The stage camera measures each visitor's feet-to-head height. A
learner (`child_model.py`) fits a "small child" height cutoff from automatic
examples (very uneven groups, very even ones) and from the operator's switch
presses, weighted three times. A missed child costs three times a needlessly
gentle show.

**Why.** Everyone on stage stands at about the same distance, so a learned
height applies anywhere on the stage and to a lone visitor. The costs are
uneven on purpose: being too kind to a teenager is cheap; mocking a toddler
isn't.

**How it evolved.** It began as a ratio of shortest to tallest, which needs a
group. A family drifting at the stage's edge trained it over and over, so
arrivals right after a departure no longer count.

**In a business agent.** Human-in-the-loop labels from supervisors' overrides,
with error costs set by the business, not by symmetry.

## 15. Keep the speaker out of the microphone

**Decision.** The mic runs through a noise gate, and while the pumpkin's own
voice is playing (plus a short tail), the gate keeps the mic muted.

**Why.** The browser's echo cancellation doesn't reliably remove audio played
through Web Audio (the voice effects), and the pumpkin heard itself. A
background TV kept interrupting it until the noise gate went in.

**How it evolved.** The trade-off is explicit: visitors can't interrupt
mid-sentence. A preview clip on the setup page also mutes the mic, which is
correct (the mic would hear it) and now documented.

**In a business agent.** Kiosks and speakerphones: half-duplex by design where
echo would otherwise make the agent answer itself.

## 16. A published example: specifics in configuration

**Decision.** Nothing about one deployment is in the code: hostnames, server
paths, the neighborhood, nearby performers, the scarecrow's outfit. The yard's
facts live in an untracked `surroundings.txt` (with a generic example
committed), added to the prompt as "Your Surroundings", to use in the act or
pretend not to notice. Credentials live in `.env`.

**Why.** The repo is published as an example for SignalWire customers.

**In a business agent.** Per-tenant facts (locations, hours, product names) as
configuration loaded into the prompt, not forks of the code.

## 17. The persona is tuned against real transcripts

**Decision.** Replies are one or two short sentences, each with teeth. The act
gets a few sample lines that show the voice (not lines to say). Scorn may land
on costumes and questions, never on bodies, faces, age, or families.

**How it evolved.**
- It was too wordy; shortening the lines flattened it. "Brief, but every line
  has teeth" restored the sarcasm.
- It kept saying "I am a pumpkin aflame" and repeating itself; besides the
  clean slate per show (12), the prompt says every line is new.
- It mentioned video and cameras ("I cannot see you without your video
  enabled", which the platform's vision function returns), so the `conscience`
  frames looking as its own carved eyes and blindness as an absurd excuse
  (`explain_blindness`).
- Then it claimed blindness constantly, often right after describing costumes:
  it was looking again a second or two after the last look, and those looks
  came back empty. It now looks once per arrival, never looks for the yard's
  fixed features, and `explain_blindness` tells it to carry on when it has
  already seen the group.

**In a business agent.** Brand voice is a measurable spec: length, register,
banned words, and what's off limits, checked against transcripts and flagged in
the call log.

## 18. Setup and deploys that can't half-work

**Decision.**
- On startup, the agent creates its own SWML webhook resource from the project
  credentials and mints guest tokens limited to that one address. The SWML
  password is derived from the API token, so a restart doesn't break a call in
  progress.
- Code is mounted into the container, and only Python or `surroundings.txt`
  changes restart the process. The page is served with `no-cache`.
- `/version` returns the commit the agent started from, so an update can
  confirm the new code is the one answering.

**How it evolved.** A camera-lag test ran on stale page code because the page
had been restarted, not reloaded; `no-cache` and the live video stats came out
of that.

**In a business agent.** Self-provisioning, least-privilege client tokens, and
deploys that fail loudly.

## 19. Watch production, then add tools

**Decision.** Debug events (level 2, which includes the dialogue) are kept as
a compact live log at `/eyes-live`, because the end-of-call report doesn't
arrive until the all-night call ends. The plan for the night is to read it
every 15 minutes, find questions that keep coming up and get poor answers, and
add in-character tools for them.

**How it evolved.** The first tool shipped this way was `guess_what_it_is`.
Three different groups asked the pumpkin what something was ("What is it?",
"What do you think of that?"); each time it looked, the look came back empty in
the dark, and it answered with a blindness excuse. The tool answers without
looking: a grand, confidently wrong verdict in the act, a delighted guess in
gentle mode.

**In a business agent.** Mining live transcripts for unhandled intents, and
closing the loop with new tools rather than longer prompts.

## 20. A second AI grades the first (quality control)

**Decision.** An `ai_sidecar` listens to the call alongside the pumpkin. It
never speaks and never sees the pumpkin's prompt; its prompt is the character's
spec written for a reviewer. For each visitor turn it records the reply it
expects before the pumpkin answers, then grades the actual reply. The server
tells it about each tool the pumpkin calls (the sidecar only hears speech), and
it judges whether the tool fit. Everything is report-only and logged for
tuning.

**Why.** The live log shows what happened; it doesn't say whether that was
right. A reviewer without the pumpkin's instructions catches what the
instructions themselves get wrong, and predicting before hearing the reply
keeps it from grading on hindsight.

**How it evolved.** The first test call showed it works end to end, including
hearing the pumpkin's voice, but reviewed only 2 of 7 replies and missed tools
used close together. Its prompt now asks for a review of the latest reply every
turn, the server spaces out its tool notes, and it knows `resume_act` and which
voice applies after gentle mode (it had flagged the operator's override as an
unknown tool and a gentle reply as "not sarcastic enough"). Its reviews were
also one line late: it's called when a visitor stops talking, before the reply,
so the reply it sees answers the line before; reviews now name the line they
answer, and the server tells it when the pumpkin changes voice.

**In a business agent.** Continuous QA on every call instead of sampled
reviews: an independent model scores answers against the policy and flags
off-script replies and wrong tool use, feeding prompt and tool fixes.

## 21. The agent's own built-ins work against a call that never ends

**Decision.** The prompt says the pumpkin never ends the conversation or hangs
up, and the page redials at once (no backoff) when a call that was working
ends. A costume is judged once per visit, and the reminder after every tool call says not to repeat the
line just spoken.

**Why.** A child's "Good night" made the model use the platform's built-in
hangup, a natural move for a phone agent and wrong for a prop that holds one
call all night; the redial then took about 12 seconds. The same session tripped
the platform's loop guard twice: switching to gentle while already gentle, and
judging the same costume five times because the greet step (re-read every
turn) asked for a verdict. After tool calls, the model often re-spoke the line
it had just said. The first fix for that ("carry on with something new, or say
nothing more") made it go silent for 14 seconds after a goodbye; the reminder
now only forbids repeating the last line word for word. And once, leaving gentle mode
cleared the group's data but the switch back to the act never landed, so the
pumpkin kept talking to a princess who had gone; the act's exit now switches
context and step together, so the next departure recovers from that.

**How it evolved.** Making `become_gentle` a no-op "when already gentle" (by
the call's data) backfired: after a lost mode switch the data said gentle while
the pumpkin was in the act, so a child got the act and an insulted costume.
`become_gentle` now always switches (it's only offered in the act), and the
flag is gone entirely (see 3). Lesson: trust where the conversation actually is
(its context and step) over a flag that can drift from it.

**In a business agent.** Built-in behaviors tuned for one shape of
conversation (hang up after goodbye) need explicit overrides in another (a
kiosk, a long-lived session), and idempotent tools keep a re-read instruction
from turning into a loop.

---

## 22. The server owns the state; the model's machine is made to match

**Decision.** `visit_state.py` keeps the
authoritative mode for the group in front: the yard (nobody here), the act, or
gentle mode. Whatever the server learns first (the stage, the operator, the age
check) changes it in code. Every tool request compares it with the mode the
pumpkin is in and carries the switch when they differ. Switches the model asks
for are granted only when the server agrees: toward gentle mode during a visit,
always; leaving, only once the stage says the group has gone; the act, only for
a group that passed the checks or that the operator chose. A disagreement that
lasts 30 seconds, or a pumpkin repeating itself, restarts the call, at most once
every 5 minutes; beyond that the operator is alerted.

**Why.** The platform's contexts and steps are a state machine in code, but
only the model moves it: a tool result can change the step, and a tool runs
only when the model calls one. Over the first days, the events moved out of the
conversation (turn counting, the stage camera, the operator's switch) while the
transitions stayed inside it, so each one reached the model as a request in
prose. On Halloween night it ignored them for 50 minutes. Nobody chose to leave
the SDK's machine; each change went through the only door the platform offers.

**How it evolved.** Replaying the night's live log through the new state
(`sim/replay.py`, with the page's new rules: 3 seconds of presence for a visit,
8 seconds of empty stage to end one) gave 99 visits instead of 166 arrivals, 30
hellos to passersby, about 7 minutes in the wrong mode instead of about 127
(mostly the seconds between a visit's start or end and the pumpkin's next tool
call), and a largest conversation of about 5,000 tokens of dialogue instead of
44,000. The stuck gentle mode would have been corrected 4 seconds in. Wiring
it in removed the shadow copies it replaced: the pacer's guesses at the visit
from the pumpkin's steps, the operator's flag in `global_data`, the farewell
step, and the "left" timestamp the leaving tool checked. A second replay drives
the real server code (`sim/replay_agent.py`) and found a case the state alone
couldn't show: the age check failing for a passerby the stage never counted
switches the platform to gentle mode with nobody there, and no message asks
for a tool, so only the 30-second restart caught it (five times in the night).
Now the server asks for the tool after 8 seconds; in the replay that left no
stuck restarts and about 15 minutes in the wrong mode over the night, nearly
all of it the few seconds between each visit's start or end and the next tool
call. A first call with a fake camera and generated pictures found three more
things no replay could: the platform keeps its own look tool whatever a step
lists; a switch sent with a goodbye can be lost when the next message arrives
first, so the server now counts a switch only once the platform reports it; and
the prompt's example line ("Easily the best princess tonight…") was said word
for word with no princess there, so examples now describe the shape, not the
words. A third call found the worst: the server wanted the act the moment a
group arrived, before any age check, and twice a small child got it. Now no
group gets the act until its age check passes, and a pumpkin already in gentle
mode stays there until the new group passes its own. Lesson: when the server
owns the state, it must not assume the safe answer it hasn't checked yet. Two
platform requests would make the workaround unnecessary: the current context in
every tool request (signalwire/cloud-product#22092), and letting the server
change it directly (#22114).

**In a business agent.** A payment posting, an identity check finishing, or a
supervisor taking over changes the agent's state because the system that knows
says so, and the agent's own machine is corrected on its next step rather than
asked to notice.

## Open questions

- Small children's costumes never reach the costume log when a group goes
  straight to gentle mode (`notice_visitors` doesn't run). Needs a small
  gentle-mode tool if it matters.
- The learner's ratio rule, trained on early switch presses, is far too strict
  (0.87). The height rule has taken over; if the learner is reset, watch it.
