// Keeps one video call to the pumpkin agent open all night: camera and mic out,
// the pumpkin's voice in (through the effects chain), no video back. Redials
// whenever the call ends.
// Must load before the SDK: it records the peer connections the SDK makes.
import { refreshVideo, videoStats } from "./video-sender.js";
import { SignalWire } from "https://cdn.jsdelivr.net/npm/@signalwire/js@4.0.0-rc.4/dist/browser.mjs";
import { Tone, DEFAULT_EFFECTS, buildVoiceChain, loadWorklet } from "./voice.js";
import { Scene } from "./scene.js";
import { DEFAULT_STAGE, STAGE_ZONE } from "./stage.js";
import { CONTROLS, meterPercent, readout, renderSliders } from "./controls.js";

const cfg = window.PUMPKIN;
const voice = document.getElementById("voice");
const statusLine = document.getElementById("status");
const FINAL = new Set(["disconnected", "failed", "destroyed"]);
const MIN_BACKOFF = 5;
const MAX_BACKOFF = 60;
const HEALTHY_CALL_MS = 30000;
const REANNOUNCE_DELAY_MS = 4000;
// A client whose startup auth failed never becomes ready. Past this, the
// client is rebuilt.
const DIAL_TIMEOUT = 30;

const log = (...args) => console.log("[pumpkin]", ...args);
// Some browser errors (DOMExceptions) carry only a name, so fall back to it.
const describe = (e) => e?.message || e?.name || String(e);
const sleep = (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));
const status = (text) => {
  statusLine.textContent = text;
  log(text);
};

// ---------------------------------------------------------------- voice

// Storage can throw (private window, blocked site data); the defaults still work.
const store = (key) => ({
  load: () => { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  save: (v) => { try { localStorage.setItem(key, JSON.stringify(v)); } catch {} },
  clear: () => { try { localStorage.removeItem(key); } catch {} },
});

const effectsStore = store("pumpkin-effects");
const effects = { ...DEFAULT_EFFECTS, ...effectsStore.load() };
let chain;

// Audio can only start after a click, so the chain is built on the first one
// (Start, or picking a preview clip) and reused for the rest of the night.
async function ensureAudio() {
  if (chain) return chain;
  await Tone.start();
  chain = await buildVoiceChain(effects);
  chain.output.connect(Tone.getDestination());
  chain.setEnabled(document.getElementById("effects").checked);
  return chain;
}

// A group of sliders (voice, gate, stage): rendered from controls.js into the
// page's [data-sliders] box, saved per computer, and applied live. A slider
// move, a reset, and a remote command all go through set() or reset().
const groups = {};

function addGroup(group, settings, defaults, storage, apply) {
  const box = document.querySelector(`[data-sliders="${group}"]`);
  const sliders = renderSliders(box, CONTROLS[group]);
  const sync = () => {
    for (const [name, { input, output }] of Object.entries(sliders)) {
      input.value = settings[name];
      output.textContent = readout(name, settings[name]);
    }
  };
  const set = (name, value) => {
    settings[name] = value;
    sync();
    apply(name, value);
    storage.save(settings);
  };
  for (const [name, { input }] of Object.entries(sliders)) input.oninput = () => set(name, Number(input.value));
  groups[group] = {
    settings,
    set,
    reset() {
      Object.assign(settings, defaults);
      storage.clear();
      sync();
      for (const [name, value] of Object.entries(settings)) apply(name, value);
    },
  };
  sync();
  return groups[group];
}

const effectsToggle = document.getElementById("effects");
function setEffectsOn(on) {
  effectsToggle.checked = on;
  chain?.setEnabled(on);
}
effectsToggle.onchange = () => setEffectsOn(effectsToggle.checked);
addGroup("effects", effects, DEFAULT_EFFECTS, effectsStore, (name, value) => chain?.set(name, value));
document.getElementById("reset").onclick = () => groups.effects.reset();

let preview;
let previewUrl;
const stopPreview = document.getElementById("stop-preview");

function endPreview() {
  preview?.dispose();
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  preview = previewUrl = undefined;
  stopPreview.disabled = true;
}

document.getElementById("preview").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  await ensureAudio();
  endPreview();
  previewUrl = URL.createObjectURL(file);
  preview = new Tone.Player({ url: previewUrl, loop: true });
  await Tone.loaded();
  preview.connect(chain.input).start();
  stopPreview.disabled = false;
};
stopPreview.onclick = endPreview;

// ---------------------------------------------------------------- mic gate

// Mutes the mic below a threshold so background noise never reaches the
// recognizer and interrupts the pumpkin. The threshold goes slightly above the
// level of the background noise.
const DEFAULT_GATE = { thresholdDb: -45, holdSeconds: 0.4 };
const GATE_URL = new URL("./noise-gate.worklet.js", import.meta.url).href;
const gateStore = store("pumpkin-gate");
const gate = { ...DEFAULT_GATE, ...gateStore.load() };
let gateNode;

const meterLevel = document.getElementById("mic-level");
const meterThreshold = document.getElementById("mic-threshold");
let micLevel = null; // the latest gate report, for the remote's meter
let echoCancellation = null; // whether the browser actually turned it on for the mic

function showLevel(report) {
  micLevel = report;
  meterLevel.style.width = report ? meterPercent(report.levelDb) : "0%";
  meterLevel.classList.toggle("open", Boolean(report?.open));
  meterLevel.classList.toggle("ducked", Boolean(report?.ducked));
}

function applyGate(name, value) {
  if (gateNode) gateNode.parameters.get(name).value = value;
  if (name === "thresholdDb") meterThreshold.style.left = meterPercent(value);
}
addGroup("gate", gate, DEFAULT_GATE, gateStore, applyGate);
document.getElementById("reset-gate").onclick = () => groups.gate.reset();
applyGate("thresholdDb", gate.thresholdDb);

// Opens the mic through the gate and returns the gated stream for the call.
async function openGatedMic(deviceId) {
  const context = Tone.getContext();
  await loadWorklet(context, GATE_URL);
  const raw = await navigator.mediaDevices.getUserMedia({
    audio: {
      ...(deviceId && { deviceId: { exact: deviceId } }),
      // The speaker sits next to the mic; echo cancellation keeps the pumpkin
      // from hearing itself and interrupting its own lines.
      echoCancellation: true,
      noiseSuppression: true,
      // Off: in a quiet room it would turn the mic up, raising the background
      // toward the gate's threshold.
      autoGainControl: false,
    },
  });
  // Not every mic or system honors echo cancellation; say whether this one did.
  echoCancellation = Boolean(raw.getAudioTracks()[0]?.getSettings().echoCancellation);
  log(`mic echo cancellation: ${echoCancellation ? "on" : "off (the gate still mutes the mic while the pumpkin talks)"}`);
  const source = context.createMediaStreamSource(raw);
  // Input 0 is the mic; input 1 is the pumpkin's own voice, which mutes the mic
  // while it plays (see noise-gate.worklet.js).
  const node = context.createAudioWorkletNode("noise-gate", { numberOfInputs: 2, outputChannelCount: [1] });
  for (const [name, value] of Object.entries(gate)) node.parameters.get(name).value = value;
  node.port.onmessage = ({ data }) => showLevel(data);
  const voiceTap = context.createGain();
  chain.output.connect(voiceTap);
  voiceTap.connect(node, 0, 1);
  const destination = context.createMediaStreamDestination();
  source.connect(node);
  node.connect(destination);
  gateNode = node;

  return {
    stream: destination.stream,
    close() {
      raw.getTracks().forEach((t) => t.stop());
      source.disconnect();
      chain.output.disconnect(voiceTap);
      voiceTap.disconnect();
      node.disconnect();
      if (gateNode === node) gateNode = undefined;
      showLevel(null);
    },
  };
}

// ---------------------------------------------------------------- stage

// Who's on stage; tells the server when a show starts and ends. See stage.js.
const stageStore = store("pumpkin-stage");
// The operator-adjustable stage settings: the stage area and Min size.
const STAGE_DEFAULTS = Object.fromEntries([...STAGE_ZONE, "minHeight"].map((k) => [k, DEFAULT_STAGE[k]]));
const stageSettings = { ...STAGE_DEFAULTS, ...stageStore.load() };
const scene = new Scene({
  video: document.getElementById("stage-video"),
  canvas: document.getElementById("stage-canvas"),
  summary: document.getElementById("stage-summary"),
  settings: stageSettings,
  log,
  onPictureChange: refreshVideo,
  // The mic gate mutes while the pumpkin's voice plays: the best signal of it talking.
  isTalking: () => Boolean(micLevel?.ducked),
  // Dragging a stage area on the camera view goes through the same path as the sliders.
  onZone: (zone) => Object.entries(zone).forEach(([name, value]) => groups.stage.set(name, value)),
});
// For poking at from the browser console while tuning on site.
window.pumpkinScene = scene;
addGroup("stage", stageSettings, STAGE_DEFAULTS, stageStore, (name, value) => scene.set(name, value));
document.getElementById("reset-stage").onclick = () => groups.stage.reset();

// ---------------------------------------------------------------- call

// Guest tokens from our own server, each able to dial only the pumpkin. The SDK
// calls refresh() before one expires, so the call outlives the two-hour token.
class PumpkinTokens {
  async authenticate() {
    const response = await fetch("/eyes-token", { method: "POST" });
    if (!response.ok) throw new Error(`token request failed: ${response.status}`);
    const { token, expiresAt, destination } = await response.json();
    this.destination = destination;
    return { token, expiry_at: expiresAt };
  }

  refresh() {
    return this.authenticate();
  }
}

let tokens;
function makeClient() {
  tokens = new PumpkinTokens();
  const c = new SignalWire(tokens);
  const errors = c.errors$.subscribe((e) => log("sdk error:", describe(e)));
  c.destroyed$.subscribe(() => errors.unsubscribe());
  return c;
}

// Resolves once the client has a token, which is also when we learn where to
// dial. cancel() drops the wait when the client is abandoned instead.
function whenReady(c) {
  let sub;
  const promise = new Promise((resolve) => {
    sub = c.ready$.subscribe((ready) => {
      if (!ready) return;
      resolve();
      queueMicrotask(() => sub.unsubscribe());
    });
  });
  return { promise, cancel: () => sub?.unsubscribe() };
}

let client = makeClient();

async function findDevice(devices, kind, label) {
  if (!label) return undefined;
  const match = devices.find(
    (d) => d.kind === kind && d.label.toLowerCase().includes(label.toLowerCase()),
  );
  if (!match) log(`no ${kind} matching "${label}"; using the default`);
  return match?.deviceId;
}

let mic;
let camera;
let currentCall;

// Start and Stop, from the page or the remote. Stop hangs up and holds the
// call loop until Start; Start can only follow one click on this page, since
// Chrome won't play audio before that.
let armed = false;
let running = false;
let wake = () => {};
const nextChange = () => new Promise((resolve) => (wake = resolve));
// Rejects when Stop is pressed, so a dial still waiting to start gives up.
let onStop = () => {};
const whenStopped = () => new Promise((_, reject) => (onStop = () => reject(new Error("stopped"))));

function setRunning(on) {
  if (on && !armed) return;
  running = on;
  if (!on) {
    status("Stopped. Press Start on the remote to begin again.");
    currentCall?.hangup().catch(() => {});
    onStop();
  }
  wake();
}
// Reloading or closing the page hangs up, so the old call doesn't linger for a
// minute beside the next one. Best effort; the server also follows the newest call.
window.addEventListener("pagehide", () => currentCall?.hangup().catch(() => {}));

async function mediaOptions() {
  // Device labels stay blank until the page has opened the devices once.
  const warmup = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
  warmup.getTracks().forEach((t) => t.stop());

  const devices = await navigator.mediaDevices.enumerateDevices();
  log("devices:", devices.map((d) => `${d.kind}: ${d.label}`).join(" | "));
  const micId = await findDevice(devices, "audioinput", cfg.micLabel);
  const cameraId = await findDevice(devices, "videoinput", cfg.cameraLabel);
  mic = await openGatedMic(micId);
  // Once the SDK is handed one stream it sends only the streams it's handed and
  // ignores device settings, so the camera is opened here alongside the gated
  // mic. Without it the pumpkin is blind; a missing camera fails the dial.
  camera = await navigator.mediaDevices.getUserMedia({
    video: {
      ...(cameraId && { deviceId: { exact: cameraId } }),
      // Plenty for the vision model.
      width: { ideal: 640 },
      height: { ideal: 480 },
      frameRate: { ideal: 10, max: 15 },
    },
  });
  scene.attach(camera);

  return {
    audio: true,
    video: true,
    receiveAudio: true,
    receiveVideo: false,
    fallbackToReceiveOnly: false,
    inputAudioStream: mic.stream,
    inputVideoStream: camera,
  };
}

async function dialOrRebuild() {
  const options = await mediaOptions();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`dial didn't start within ${DIAL_TIMEOUT}s`)), DIAL_TIMEOUT * 1000);
  });
  const ready = whenReady(client);
  const call = ready.promise.then(() => client.dial(tokens.destination, options));
  try {
    const stopped = whenStopped();
    stopped.catch(() => {}); // a Stop after the race is settled rejects it unheard
    return await Promise.race([call, timeout, stopped]);
  } catch (e) {
    // A dial that connects after Stop or the timeout won the race would
    // otherwise be left running.
    call.then((late) => late.hangup().catch(() => {}), () => {});
    // Rebuilding the client in place keeps the page, and its audio, alive.
    ready.cancel();
    client.destroy();
    client = makeClient();
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function oneCall() {
  status("Calling the pumpkin…");
  try {
    const call = await dialOrRebuild();
    currentCall = call;
    let source;
    const streamSub = call.remoteStream$.subscribe((stream) => {
      source?.disconnect();
      source = undefined;
      voice.srcObject = stream;
      if (stream?.getAudioTracks().length) {
        source = Tone.getContext().createMediaStreamSource(stream);
        Tone.connect(source, chain.input);
      }
    });
    let connected = false;
    await new Promise((resolve) => {
      const statusSub = call.status$.subscribe((s) => {
        log("call:", s);
        if (s === "connected") {
          // A show already under way is news to the new call. Wait for the
          // server to see this call start before telling it.
          if (!connected) setTimeout(() => scene.reannounce(), REANNOUNCE_DELAY_MS);
          connected = true;
          status("Connected. The pumpkin is watching.");
          // Stop was pressed while this call was still connecting.
          if (!running) call.hangup().catch(() => {});
        }
        if (FINAL.has(s)) {
          statusSub?.unsubscribe();
          resolve();
        }
      });
    });
    streamSub.unsubscribe();
    source?.disconnect();
    voice.srcObject = null;
    return connected;
  } finally {
    // A fresh mic, gate, and camera each call; the SDK may stop the tracks it was given.
    mic?.close();
    scene.detach();
    camera?.getTracks().forEach((t) => t.stop());
    mic = camera = currentCall = undefined;
  }
}

// Keeps the pumpkin's computer from sleeping while the call runs.
async function holdWakeLock() {
  try {
    await navigator.wakeLock.request("screen");
  } catch (e) {
    log("wake lock unavailable:", describe(e));
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") holdWakeLock();
});

async function runForever() {
  let backoff = MIN_BACKOFF;
  for (;;) {
    if (!running) {
      status("Stopped. Press Start on the remote to begin again.");
      while (!running) await nextChange();
      backoff = MIN_BACKOFF;
    }
    let connected = false;
    const started = Date.now();
    try {
      // A call that connected and later ended (the agent's inactivity
      // timeout, the agent hanging up after a goodbye, a network blip)
      // redials at once; repeated failures back off.
      connected = await oneCall();
      if (connected) backoff = MIN_BACKOFF;
    } catch (e) {
      log("call failed:", describe(e));
    }
    if (!running) continue;
    // A call that ran a while redials at once; one that dropped straight after
    // connecting backs off like a failure, so a broken agent can't spin the loop.
    if (connected && (redialNow || Date.now() - started > HEALTHY_CALL_MS)) {
      redialNow = false;
      status("Call ended. Redialing…");
      continue;
    }
    status(`Call ended. Redialing in ${backoff}s…`);
    await Promise.race([sleep(backoff), nextChange()]);
    backoff = Math.min(backoff * 2, MAX_BACKOFF);
  }
}

const startButton = document.getElementById("start");
startButton.onclick = async () => {
  startButton.disabled = true;
  startButton.hidden = true;
  await ensureAudio();
  await holdWakeLock();
  // The pumpkin still works without stage detection (it notices people when
  // they speak), so a detector that won't load doesn't stop the call.
  scene.load().catch((e) => log("stage detection unavailable:", describe(e)));
  armed = true;
  running = true;
  runForever();
};

// ---------------------------------------------------------------- remote

// The phone remote (remote.html) talks to this page through the server: it
// queues numbered commands, this page applies the new ones, and posts its
// status back. See remote_control.py.
const REMOTE_POLL_MS = 1000;
let remoteSeq = -1;
let polling = false;

// The server restarts the call when the pumpkin is stuck in the wrong mode or
// repeating itself (visit_state.py). It waits for the pumpkin to finish
// speaking and, for a while, for the stage to empty; a stuck pumpkin in front
// of visitors is worse than a short gap, so after RESTART_WAIT_MS it goes anyway.
const RESTART_WAIT_MS = 20000;
let redialNow = false;
let restarting = false;

async function restartCall(why) {
  if (restarting || !currentCall) return;
  restarting = true;
  log("restarting the call:", why);
  const until = Date.now() + RESTART_WAIT_MS;
  while (Date.now() < until && (micLevel?.ducked || scene.stage.show)) await sleep(0.5);
  redialNow = true;
  restarting = false;
  currentCall?.hangup().catch(() => {});
}

function applyCommand({ kind, name, value }) {
  if (kind === "run") return setRunning(Boolean(value));
  if (kind === "restart") return restartCall(value);
  if (kind === "reset") return groups[name]?.reset();
  if (kind !== "set") return;
  if (name === "effectsOn") return setEffectsOn(Boolean(value));
  const group = Object.values(groups).find((g) => Object.hasOwn(g.settings, name));
  group?.set(name, Number(value));
}

async function pollRemote() {
  // One request at a time: a slow reply landing after a newer one would move
  // remoteSeq backwards and replay commands.
  if (polling) return;
  polling = true;
  try {
    const response = await fetch(`/eyes-remote/command?after=${remoteSeq}`);
    const { seq, commands } = await response.json();
    for (const command of commands) applyCommand(command);
    remoteSeq = seq;
  } catch (e) {
    log("remote unavailable:", describe(e));
  } finally {
    polling = false;
  }
}

// A fresh keyframe every few seconds even when nothing seems to change, so
// whatever the pumpkin looks at is never older than this. See video-sender.js.
const KEYFRAME_EVERY_MS = 1500;
setInterval(() => running && refreshVideo(), KEYFRAME_EVERY_MS);
let video = null;
setInterval(async () => (video = running ? await videoStats() : null), REMOTE_POLL_MS);

function reportStatus() {
  const settings = { effectsOn: effectsToggle.checked };
  for (const g of Object.values(groups)) Object.assign(settings, g.settings);
  fetch("/eyes-remote/status", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      armed,
      running,
      status: statusLine.textContent,
      stage: document.getElementById("stage-summary").textContent,
      castPeople: scene.stage.castPeople(),
      showLive: Boolean(scene.stage.show),
      mic: micLevel,
      echoCancellation,
      video,
      settings,
    }),
  }).catch(() => {});
}

setInterval(pollRemote, REMOTE_POLL_MS);
setInterval(reportStatus, REMOTE_POLL_MS);
pollRemote();
reportStatus();
