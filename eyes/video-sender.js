// Keeps the platform's view of the camera current.
//
// The pumpkin's vision works from frames the platform takes from the call's
// video. A video encoder sends one complete frame (a keyframe) at the start and
// then only changes, and Chrome sends no more keyframes unless the far end asks.
// With a still or slow-changing picture the platform can be left describing the
// first frame it got, or nothing at all. Briefly nudging the sender's resolution
// makes Chrome encode a fresh keyframe, so the page does that whenever the
// picture changes and every few seconds.
//
// The Browser SDK doesn't expose its peer connection, so this module records
// every RTCPeerConnection the page makes. Import it before the SDK.

const peers = new Set();
const Native = window.RTCPeerConnection;
window.RTCPeerConnection = class extends Native {
  constructor(...args) {
    super(...args);
    peers.add(this);
    this.addEventListener("connectionstatechange", () => {
      if (this.connectionState === "closed") peers.delete(this);
    });
  }
};

function videoSenders() {
  return [...peers].flatMap((pc) => pc.getSenders().filter((s) => s.track?.kind === "video"));
}

// At most one nudge a second, however often the picture changes.
const MIN_GAP_MS = 1000;
let nudging = false;
let lastNudge = 0;
// Each sender's own scale, taken before the first nudge, so a nudge whose undo
// fails can't compound into a smaller and smaller picture.
const baseScale = new WeakMap();

async function setScale(sender, scale) {
  const params = sender.getParameters();
  if (!params.encodings?.length) return false;
  params.encodings[0].scaleResolutionDownBy = scale;
  await sender.setParameters(params);
  return true;
}

// A fresh keyframe on every video sender.
export async function refreshVideo() {
  if (nudging || performance.now() - lastNudge < MIN_GAP_MS) return;
  nudging = true;
  lastNudge = performance.now();
  try {
    await nudgeAll();
  } finally {
    nudging = false;
  }
}

async function nudgeAll() {
  for (const sender of videoSenders()) {
    if (!baseScale.has(sender)) {
      baseScale.set(sender, sender.getParameters().encodings?.[0]?.scaleResolutionDownBy || 1);
    }
    const scale = baseScale.get(sender);
    try {
      if (await setScale(sender, scale * 1.01)) await setScale(sender, scale);
    } catch {
      // A sender closing or renegotiating mid-nudge; put it back if it's still there.
      setScale(sender, scale).catch(() => {});
    }
  }
}

// What the encoder is doing, for the remote's status: whether keyframes go out,
// and whether the far end asks for them (PLI/FIR).
export async function videoStats() {
  for (const sender of videoSenders()) {
    for (const s of (await sender.getStats()).values()) {
      if (s.type !== "outbound-rtp") continue;
      return {
        fps: s.framesPerSecond ?? null,
        width: s.frameWidth ?? null,
        keyframes: s.keyFramesEncoded ?? null,
        pli: s.pliCount ?? null,
        fir: s.firCount ?? null,
        limit: s.qualityLimitationReason ?? null,
      };
    }
  }
  return null;
}
