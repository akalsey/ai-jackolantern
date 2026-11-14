// Who's on stage, and is it still the same show?
//
// On a busy street heads change constantly, so a headcount means little. What
// matters is the cast: the people standing in front of the pumpkin when a show
// starts. Latecomers join the audience without restarting anything. The show
// ends when most of the cast has walked off, even if the crowd hasn't shrunk.
//
// Pure logic, no browser APIs: feed it person boxes (normalized 0..1) with a
// timestamp, get back events and each tracked person's role.

// Where a person stands is where their feet are: the bottom center of their
// box. The camera sees well past the stage, so being on stage means having your
// feet inside the stage area, not looking big.
export const STAGE_ZONE = ["zoneTop", "zoneBottom", "zoneLeft", "zoneRight"];

export const DEFAULT_STAGE = {
  // On stage: feet inside the stage area, not tiny, and standing still.
  zoneTop: 0.55, // the stage area (the floor where feet go), as fractions of the frame
  zoneBottom: 1,
  zoneLeft: 0.1,
  zoneRight: 0.9,
  minHeight: 0.15, // box height as a fraction of the frame; ignores stray small boxes
  maxDrift: 0.12, // frame widths per second; faster is walking past
  dwellMs: 250, // floor for the stillness check; MOVEMENT_WINDOW_MS (longer) governs today
  // Shows.
  startMs: 0, // someone on stage this long starts a show (people pass fleetingly)
  castGoneMs: 3000, // a cast member off stage this long counts as gone
  retainFraction: 0.5, // the show lasts while at least this share of the cast is here
  endMs: 4000, // ...and ends once it has been below that for this long
  // Tracking.
  trackTtlMs: 1500, // forget a person not seen for this long
  matchIou: 0.3, // overlap to treat a box as the same person
  rejoinDistance: 0.15, // or a nearby box, for a person briefly lost
  // ...but only if they still look the same: the mean difference of their
  // appearance signatures (see Scene) stays under this. Without it, a new group
  // stepping into the same spots would inherit the last group's identities.
  maxLookChange: 0.12,
};

const center = (b) => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 });

// Mean absolute difference between two appearance signatures, or 0 when either
// is missing (then position alone decides).
function lookChange(a, b) {
  if (!a || !b || a.length !== b.length) return 0;
  return a.reduce((sum, v, i) => sum + Math.abs(v - b[i]), 0) / a.length;
}

// Feet-to-head height is only real when the whole person is in frame: a box
// running into the top or bottom edge has lost a head or feet. look is their
// appearance signature, which the server uses to recognize a returning group.
const EDGE = 0.01;
export function measure(track) {
  const b = track.box;
  return {
    height: +b.h.toFixed(3),
    feetY: +(b.y + b.h).toFixed(3),
    whole: b.y > EDGE && b.y + b.h < 1 - EDGE,
    look: track.look?.map((v) => +v.toFixed(3)),
  };
}
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

function iou(a, b) {
  const x1 = Math.max(a.x, b.x);
  const y1 = Math.max(a.y, b.y);
  const x2 = Math.min(a.x + a.w, b.x + b.w);
  const y2 = Math.min(a.y + a.h, b.y + b.h);
  const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
  return inter / (a.w * a.h + b.w * b.h - inter || 1);
}

// The shortest stretch over which movement is measured; see #judge.
const MOVEMENT_WINDOW_MS = 500;

export class Stage {
  constructor(settings = {}) {
    this.s = { ...DEFAULT_STAGE, ...settings };
    this.tracks = [];
    this.nextId = 1;
    this.show = null; // { cast: Map(id -> last time on stage), lowSince }
  }

  set(name, value) {
    this.s[name] = value;
  }

  // detections: [{ x, y, w, h, look? }] normalized to the frame, where look is
  // an optional appearance signature (numbers 0..1). cut: true when the whole
  // picture just changed at once (see Scene): everyone in it is new, so the
  // current show ends now and identities start over. Returns
  // { events: [{ type: "arrived" | "left", ... }], tracks, show }.
  update(t, detections, { cut = false } = {}) {
    const events = [];
    if (cut) {
      this.tracks = [];
      if (this.show) events.push({ type: "left", cast: this.show.cast.size });
      this.show = null;
    }
    this.#track(t, detections);
    for (const track of this.tracks) this.#judge(track, t);
    events.push(...this.#direct(t));
    return { events, tracks: this.tracks, show: this.summary(t) };
  }

  summary(t) {
    if (!this.show) return { live: false, cast: 0, present: 0, audience: 0 };
    const present = this.#castPresent(t);
    const audience = this.tracks.filter((tr) => tr.onStage && !this.show.cast.has(tr.id)).length;
    return { live: true, cast: this.show.cast.size, present, audience };
  }

  // The cast members on stage right now, measured for the server's small-child
  // learner. See measure().
  castPeople() {
    if (!this.show) return [];
    return this.tracks.filter((tr) => tr.onStage && this.show.cast.has(tr.id)).map(measure);
  }

  role(track) {
    if (this.show?.cast.has(track.id)) return "cast";
    return track.onStage ? "audience" : "passerby";
  }

  // Match this frame's boxes to known people: best overlap first, then a nearby
  // box for someone recently lost (occlusion, a missed frame).
  #track(t, detections) {
    // Cast members are kept as long as castGoneMs, so one hidden behind someone
    // for a couple of seconds comes back as themselves, not as a newcomer.
    const ttl = (tr) => (this.show?.cast.has(tr.id) ? this.s.castGoneMs : this.s.trackTtlMs);
    const live = this.tracks.filter((tr) => t - tr.lastSeen <= ttl(tr));
    const pairs = [];
    detections.forEach((box, di) => {
      for (const tr of live) {
        const overlap = iou(box, tr.box);
        const near = distance(center(box), center(tr.box)) <= this.s.rejoinDistance;
        const sameLook = lookChange(box.look, tr.look) <= this.s.maxLookChange;
        if ((overlap >= this.s.matchIou || near) && sameLook) pairs.push({ di, tr, score: overlap || 0.01 });
      }
    });
    pairs.sort((a, b) => b.score - a.score);
    const usedDetections = new Set();
    const usedTracks = new Set();
    for (const { di, tr } of pairs) {
      if (usedDetections.has(di) || usedTracks.has(tr)) continue;
      usedDetections.add(di);
      usedTracks.add(tr);
      this.#see(tr, detections[di], t);
    }
    detections.forEach((box, di) => {
      if (usedDetections.has(di)) return;
      const tr = { id: this.nextId++, history: [], onStage: false, onStageSince: null };
      this.#see(tr, box, t);
      live.push(tr);
    });
    this.tracks = live;
  }

  #see(track, box, t) {
    track.box = box;
    // Appearance drifts slowly (turning, lighting), so it's averaged. The average
    // can wander over a long stay; that's fine, since sudden changes (a different
    // person) are what maxLookChange and cut detection catch.
    if (box.look) track.look = track.look ? track.look.map((v, i) => v * 0.8 + box.look[i] * 0.2) : [...box.look];
    track.lastSeen = t;
    track.history.push({ t, ...center(box) });
    // Keep enough history that a slow machine (about one detection a second,
    // with timers running a little fast) always has a sample at least dwellMs
    // old; with 1.5x, people flipped on and off stage every other detection.
    const keepAfter = t - Math.max(this.s.dwellMs * 2.5, 2500);
    while (track.history.length && track.history[0].t < keepAfter) track.history.shift();
  }

  #judge(track, t) {
    const c = center(track.box);
    // Movement is judged over at least half a second, from the newest position
    // that's at least that old, so a short dwell still tells someone walking past
    // from someone who has stopped, however the detection timing falls.
    const window = Math.max(this.s.dwellMs, MOVEMENT_WINDOW_MS);
    let anchor = -1;
    for (let i = track.history.length - 1; i >= 0; i--) {
      if (track.history[i].t <= t - window) { anchor = i; break; }
    }
    const span = anchor < 0 ? 0 : t - track.history[anchor].t;
    const drift = anchor < 0 ? Infinity : Math.max(...track.history.slice(anchor).map((h) => distance(h, c)));
    // Close and in front. Standing still is what makes a newcomer count as on
    // stage; once in the cast, someone hopping or gesturing is still there.
    // Clamped: rounding can put feet at the very bottom a hair past 1.
    const feet = { x: c.x, y: Math.min(1, track.box.y + track.box.h) };
    // Crossed sliders shouldn't empty the stage: read the edges either way round.
    const { zoneLeft, zoneRight, zoneTop, zoneBottom } = this.s;
    const inFront =
      t - track.lastSeen < 500 &&
      track.box.h >= this.s.minHeight &&
      feet.x >= Math.min(zoneLeft, zoneRight) &&
      feet.x <= Math.max(zoneLeft, zoneRight) &&
      feet.y >= Math.min(zoneTop, zoneBottom) &&
      feet.y <= Math.max(zoneTop, zoneBottom);
    // Slower than maxDrift (frame widths per second) over that span.
    const still = anchor >= 0 && drift <= this.s.maxDrift * (span / 1000);
    const onStage = inFront && still;
    if (onStage && !track.onStage) track.onStageSince = t;
    if (!onStage) track.onStageSince = null;
    track.onStage = onStage;
    if (inFront && this.show?.cast.has(track.id)) this.show.cast.set(track.id, t);
  }

  // Cast members seen since time `since`.
  #castSeenSince(since) {
    let seen = 0;
    for (const last of this.show.cast.values()) if (last >= since) seen++;
    return seen;
  }

  #castPresent(t) {
    let present = 0;
    for (const last of this.show.cast.values()) if (t - last <= this.s.castGoneMs) present++;
    return present;
  }

  #direct(t) {
    const events = [];
    if (this.show) {
      // Someone who steps up during a show joins the audience, but the server
      // still measures them: a small child must not go unnoticed. Only while
      // most of the cast is still here: when they've gone, the newcomers are the
      // next show (an "arrived" after the "left"), not latecomers to this one.
      // A newcomer joins only if most of the cast has been seen since they
      // stepped up. After a swap the old cast hasn't been, so the newcomers are
      // the next show. A cast that's still here keeps being seen, however slowly
      // a machine detects.
      const newcomers = this.tracks.filter(
        (tr) => tr.onStage && !this.show.cast.has(tr.id) && !this.show.joined.has(tr.id) &&
          t - tr.onStageSince >= this.s.startMs &&
          this.#castSeenSince(tr.onStageSince) / this.show.cast.size >= this.s.retainFraction,
      );
      if (newcomers.length) {
        for (const tr of newcomers) this.show.joined.add(tr.id);
        const onStage = this.tracks.filter((tr) => tr.onStage);
        events.push({ type: "joined", cast: this.show.cast.size, people: onStage.map(measure) });
      }
      const share = this.#castPresent(t) / this.show.cast.size;
      if (share >= this.s.retainFraction) {
        this.show.lowSince = null;
      } else {
        this.show.lowSince ??= t;
        if (t - this.show.lowSince >= this.s.endMs) {
          events.push({ type: "left", cast: this.show.cast.size });
          this.show = null;
        }
      }
    }
    if (!this.show) {
      const ready = this.tracks.filter((tr) => tr.onStage && t - tr.onStageSince >= this.s.startMs);
      if (ready.length) {
        const onStage = this.tracks.filter((tr) => tr.onStage);
        this.show = { cast: new Map(onStage.map((tr) => [tr.id, t])), joined: new Set(), lowSince: null };
        events.push({ type: "arrived", cast: onStage.length, people: onStage.map(measure) });
      }
    }
    return events;
  }
}
