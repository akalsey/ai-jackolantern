# The Pumpkin

A burning jack-o'-lantern on a scarecrow's body that talks to trick-or-treaters.
It's a SignalWire voice AI agent that can see: a computer hidden in the prop
keeps a video call open all night, sending the yard's camera and mic to the
agent and playing the pumpkin's voice through a speaker, with effects that make
it sound like it's coming from inside a gourd.

It's built as an example of what a SignalWire AI agent can do beyond a single
prompt:

- **Contexts and steps** that move a conversation through stages (greet,
  banter, shoo), with each step limiting which functions the model can call.
- **SWAIG functions** that hold the character's backstory and boundaries in
  code, return guidance the model follows, and change steps and contexts.
- **Vision**: the agent looks with `get_visual_input`, and semantic gates check
  camera frames before the most important decisions.
- **A safety mode for small children** that the model can enter but only a
  person can leave, plus a learner that picks up what a small child looks like
  from the operator's choices.
- **Server-side control of a live call**: debug events count conversation turns,
  and `calling.ai_message` pushes instructions and `global_data` into the call.
- **Automatic SignalWire setup**: on startup the agent creates its own SWML
  webhook resource and issues guest tokens for the browser client.
- **A Browser SDK client** with a noise gate, voice effects in Web Audio, and
  in-browser person detection that tells the agent when visitors arrive and
  leave.

One container serves both halves:

- `pumpkin_agent.py`: the agent, at `/pumpkin`. SignalWire fetches it and calls
  its functions there.
- `eyes/`: the page the in-prop computer opens, at `/eyes/`, plus a phone
  remote and a call log viewer.

Throughout this README, `pumpkin.example.com` stands for the public hostname you
deploy to (`PUBLIC_HOST`).

## 1. Deploy

The container listens on port 3000 and expects
[nginx-proxy](https://github.com/nginx-proxy/nginx-proxy) in front of it for
HTTPS, on an external Docker network named `nginx-proxy`. Rename the network in
`docker-compose.yml` if yours is called something else.

**On the Docker host,** in an empty directory to deploy from:

```sh
git clone <this repo> .
cp .env.example .env    # fill it in; see below
docker compose up -d --build
```

Settings go in `.env`, which stays on the host and out of git:

| Variable | What it's for |
|---|---|
| `PUBLIC_HOST` | The public hostname, e.g. `pumpkin.example.com`. nginx-proxy routes it here and gets its certificate, and the agent builds its webhook URLs from it |
| `SIGNALWIRE_SPACE` | Your space's hostname, e.g. `yourspace.signalwire.com` |
| `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN` | The project and an API token with the Voice scope |
| `EYES_PASSWORD` | Optional, recommended. A password the browser asks for before showing the client pages, since they can start a billed call |
| `PUMPKIN_MIC_LABEL`, `PUMPKIN_CAMERA_LABEL` | Optional. Part of a device's name, to pick it over the browser's default |
| `SWML_BASIC_AUTH_USER`, `SWML_BASIC_AUTH_PASSWORD` | Optional. The password SignalWire uses to fetch the agent; derived from the API token when unset |

Check it: `https://pumpkin.example.com/eyes/` should show the page, and
`https://pumpkin.example.com/pumpkin` should ask for a password.

### Updating

The checkout is mounted into the container, so updating is a pull on the host:

```sh
git pull
```

- **Python or `surroundings.txt` changed:** the agent restarts its own process
  (a second or two; a function call during that moment can fail). A call in
  progress stays up.
- **Files in `eyes/` changed:** they're read fresh on every request; reload the
  pumpkin page to pick them up.
- **`Dockerfile`, `docker-compose.yml`, `.dockerignore`, or the agent's Python
  dependencies changed** (the `# /// script` block at the top of
  `pumpkin_agent.py`): rebuild with `docker compose up -d --build`.

`/version` returns the commit the agent process started from, so you can check
the new code is the one answering.

## 2. SignalWire (automatic)

There's nothing to set up by hand. Each time it starts, the agent uses the
project credentials to:

- find the SWML webhook resource named `scarecrow` in your space (or
  `PUMPKIN_RESOURCE_NAME`), creating it if it's missing, and point it at
  `https://pumpkin.example.com/pumpkin` with the basic-auth pair;
- look up the address SignalWire gave that resource;
- hand the client page short-lived guest tokens (from `/eyes-token`) that can
  dial that address and nothing else.

If the credentials are wrong, the server log says so (`SignalWire setup
failed: ...`) and the page keeps retrying until they're fixed.

## 3. Set up the computer in the prop

### What it needs

- **Browser:** a current Google Chrome or Chromium. The page uses WebRTC, Web
  Audio worklets, WebAssembly, and WebGL. It has been run on Chrome on ChromeOS
  and macOS; other browsers aren't tested.
- **Operating system:** anything that runs current Chrome with a desktop:
  ChromeOS, Windows, macOS, or Linux. The page must stay in a visible window in
  front, because browsers slow down timers in background tabs. It asks the
  browser to keep the screen awake; set the system not to sleep anyway.
- **Camera, microphone, speaker:** any USB webcam (720p is plenty; the page
  sends 640×480 at up to 15 frames a second), a microphone (a directional one
  helps on a busy street), and a speaker loud enough for the street.
- **Network:** a steady connection with upload to spare. The call carries
  low-resolution video up and audio both ways; about 1 Mbps each way is
  comfortable.
- **Processing power:** the page runs the call, the voice effects, and a person
  detector at the same time. The detector is the heavy part, and it adapts: it
  runs four times a second on a fast machine and slows toward once a second on
  a slow one, which only makes the pumpkin a little slower to notice arrivals.
  The Stage panel shows how long each detection takes and how often it runs
  ("detection 35 ms on the GPU, every 250 ms"), so open the page on a candidate
  machine and check. Under about 100 ms keeps it at full speed.

**Raspberry Pi:** not tested. A Raspberry Pi 5 (4 GB or more) running Raspberry
Pi OS (64-bit, with the desktop) and Chromium should be able to run it; check
the detection readout. A Pi 4 will likely run detection near the slow end. A
Pi 3 or a Pi Zero won't keep up.

A Chromebook is a good fit: Chrome is built in, it runs on battery through a
power blip, and the lid can stay open with the screen dimmed.

### Setting it up

1. Plug in the webcam, a microphone (a directional one helps on a busy street),
   and the speaker. Choose the mic and speaker in the system's audio settings.
2. Keep it on power with the screen on; the page asks Chrome to keep it awake
   while the call runs. Turn the brightness all the way down if the glow shows
   through the prop.
3. Open `https://pumpkin.example.com/eyes/` and click **Start**. Allow the camera
   and microphone the first time; Chrome remembers the answer for this site.
4. Leave the tab in front. The status line should read
   "Connected. The pumpkin is watching."

If the call drops, the page redials by itself, backing off from 5 to 60 seconds
between tries. The Start click is needed once per page load, because Chrome
won't play audio before a click.

## Describing your yard

Copy `surroundings.example.txt` to `surroundings.txt` in the deploy directory,
next to `pumpkin_agent.py`, and write one fact per line about your yard and your
prop: the neighborhood, what's nearby, where the people with the candy are, what
the scarecrow is wearing. The pumpkin works these into the act, or pretends it
has never noticed them. The file stays out of git. The agent restarts itself
when the file changes; Stop and Start the show on the phone remote to start a
call that uses it.

## Phone remote

Open `https://pumpkin.example.com/eyes/remote.html` on your phone (add it to
your home screen for quick access). It shows whether the in-prop computer is
connected, what the pumpkin is doing, who's on stage, and the mic level. From
it you can start and stop the show and change every voice, stage, and mic
setting. Changes apply on the in-prop computer within a second and are saved
there.

The **This group** card switches the group in front of the pumpkin between
**Normal** (the usual act) and **Gentle** (warm, for small or scared children).
Use it whenever the pumpkin gets it wrong. Gentle mode can't be undone by the
pumpkin itself, only by this switch. Every press also teaches it: the stage
camera measures each visitor head to feet, and groups with an obvious height
difference (a parent and a toddler) teach it automatically what a small child
measures. Your choices count three times as much. Once it has learned that
height, anyone shorter starts in gentle mode, wherever they stand on the stage
and even on their own. The card shows what it has learned so far. Press the
switch to confirm a correct mode too, not just to fix a wrong one; those presses
are examples as well.

After the client page loads, someone has to click **Start** on the in-prop
computer itself once, because Chrome won't play sound until then. From then on,
the remote's Start and Stop work as often as you like.

## Stage

The client page has a Stage panel showing the camera with boxes on the people it
sees. Orange marks the people who started the current show, yellow marks people
who joined since, and grey marks passersby. When someone steps up and stands
still in front of the pumpkin, a show starts and the pumpkin is told to greet
them. When most of the orange people have walked off, it's told they've left.
Newcomers alone don't end a show. If the whole picture changes at once (for
example, testing with a virtual webcam that switches photos), the show ends
immediately and the new people start the next one.

The shaded area on the camera view is the stage: the patch of floor where
visitors stand. Someone counts as on stage when their feet (the dot under their
box) are inside it. Drag a rectangle on the camera view around that floor, from
its far edge to its near edge, or use the Stage sliders here or on the phone
remote. Chrome remembers it on that computer. When someone's whole
body, head to feet, is in view, their height shows next to their box; that's
what the small-child learning uses.

## Returning visitors

When a group walks off and comes back within 20 minutes, the pumpkin recognizes
them ("You again?") and remembers what it said they were wearing. It goes by the
colors of each person's clothes and their height on the stage camera, so two
strangers dressed alike can be mistaken for each other.

## Tonight's costumes

Ask the pumpkin for its favorite costume and it answers from the costumes it has
actually seen that night ("So many people have passed by dressed as princesses,
firefighters, and ninjas. But your dinosaur has been the best."). It keeps the
descriptions it made of each group, as text, in `costumes.jsonl` in the
`call-logs` volume.

## Resetting what it has learned

The pumpkin learns as it goes: the small-child learner keeps height examples
from arriving groups and from your Gentle / Normal presses, and it keeps the
costumes it has seen for "what's your favorite costume?". Both live in the
`call-logs` volume. Test calls teach it too, and pictures of a toddler next to
adults teach it things that aren't true of your yard, so clear a test's entries
before the real night:

```sh
docker compose exec scarecrow python reset_training.py --since 2026-11-01 --dry-run
docker compose exec scarecrow python reset_training.py --since 2026-11-01
docker compose restart scarecrow
```

`--since` removes everything recorded from then on; add `--until` to remove
only a window (`--since "2026-11-02 15:30" --until "2026-11-02 16:30"`), or use
`--all` to start over. `--only learner` or `--only costumes` limits it to one of
the two. `--dry-run` shows what would go without changing anything. Each file is
backed up next to itself before it changes. Times are in the container's clock,
which is UTC. Restart afterwards: the learner reads its examples when it starts.

## Quality-control monitor

Set `QC_MONITOR=1` in `.env` to attach a second AI to the call that listens and
never speaks. For each thing a visitor says, it writes down the reply it
expects from the character before the pumpkin answers, then grades the real
reply: as expected, a deviation, out of character, unanswered, too long. It
also judges each tool the pumpkin uses. Read the findings at
`https://pumpkin.example.com/eyes/qc.html`, or turn them into a Markdown report
for tuning the prompts:

```sh
./qc_report.py https://pumpkin.example.com > qc-report.md
```

It's billed per minute while attached, which for this prop is the whole
evening.

## Call log

When a call ends, SignalWire sends the agent a full report: the conversation,
every function call with what it returned, and a one-sentence summary. The agent
saves each one to the `call-logs` Docker volume, which survives rebuilds and
redeploys, and logs a short summary to `docker compose logs scarecrow`.

Read them at `https://pumpkin.example.com/eyes/calls.html` (linked from the
client page, and behind `EYES_PASSWORD` when it's set). Each call shows its
summary, how many times the pumpkin looked, and how many lines mentioned video,
cameras, or "your eyes". Click one for the transcript, with those lines
outlined in red.

A report arrives only when its call ends. The client holds one long call, which
ends after an hour of silence or when the page redials, so expect reports in
batches rather than live.

## Echo

The speaker sits next to the mic, so the mic hears the pumpkin. The page asks
the browser for echo cancellation, and the phone remote shows whether the mic
actually has it on. Either way, the mic is muted while the pumpkin is talking
(the meter turns blue) and for a moment after, so the pumpkin never hears
itself. The trade-off: visitors can't interrupt it mid-sentence; they can speak
as soon as it finishes.

## Tuning the voice

The defaults, set in `eyes/voice.js`:

| Effect | Default | Why |
|---|---|---|
| Pitch | −3 semitones | Enough for size. Past −4 it goes muddy, and intelligibility over crowd noise matters more than depth |
| Chorus | 30% mix | A subtle detune |
| Lowpass | 6 kHz | Shaves TTS sibilance and suggests something hollow |
| Reverb | 1.0 s, 20% wet | Small and bright: inside a gourd, not a cathedral |

The sliders on the page change each effect live, and Chrome remembers your
settings on that computer. To hear a change without a call, pick an audio clip
under **Preview a clip**: it loops through the effects until you click Stop.
Untick **Effects on** to compare against the plain voice.

Pitch shifting uses `eyes/pitch-shifter.worklet.js` rather than Tone.js's
`PitchShift`. That one computes the wrong sweep rate for downward shifts, and
−3 semitones comes out as a warbling tone instead of a lower voice.

## Testing without a call

```sh
uvx --from signalwire-sdk --with requests swaig-test pumpkin_agent.py --list-tools
uvx --from signalwire-sdk --with requests swaig-test pumpkin_agent.py --raw --exec notice_visitors --youngest teen --anyone_scared false --costumes "a vampire with a cardboard cape"
uvx --from signalwire-sdk --with requests swaig-test pumpkin_agent.py --raw --exec notice_visitors --youngest young_child --anyone_scared false
node tests/stage.test.mjs
node tests/picture.test.mjs
uv run tests/test_visit_pacing.py
uv run tests/test_child_model.py
uv run tests/test_returning_visitors.py
uv run tests/test_costume_log.py
uv run tests/test_live_log.py
uv run tests/test_agent.py
uv run tests/test_qc.py
```

The first `notice_visitors` call should move to the `greet` step and the second
should switch to the `gentle` context. The rest run the stage-detection, visit-pacing,
small-child learner, returning-visitor, costume log, live log, and agent tests.
Run them all before updating a live pumpkin.
