// Watches the camera for who's on stage and tells the server when a show starts
// (people step up) or ends (most of them walk off). The server passes that into
// the live call, so arrivals and departures don't depend on the model choosing
// to look.
import { FilesetResolver, ObjectDetector } from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.1.1/vision_bundle.mjs";
import { Stage, measure } from "./stage.js";
import { appearance, frameChange, frameGain } from "./picture.js";

const WASM = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.1.1/wasm";
const MODEL = "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite";
// Detection runs every FASTEST_MS on a quick machine. A slower one (a Raspberry
// Pi, an old laptop) backs off to about 2.5x its detection time, up to
// SLOWEST_MS, so it never falls behind; the stage logic measures stillness in
// time, not frames, so it just reacts a little later.
const FASTEST_MS = 250;
const SLOWEST_MS = 1000;
const COLORS = { cast: "#e8730c", audience: "#f5c542", passerby: "#666" };
// Appearance signatures come from a small copy of each frame.
const LOOK_WIDTH = 96;
// When the picture changes this much between detections (see picture.js; about
// 1 for two unrelated pictures), the whole picture changed at once (the camera
// cut to a new scene, or was bumped), not people walking: everyone in view is new.
const CUT_CHANGE = 0.6;
// A smaller change still means the picture is worth sending afresh (someone
// stepped up, a photo swapped); see video-sender.js.
const PICTURE_CHANGE = 0.1;
// A re-announcement after a redial is skipped if a real "arrived" went out this recently.
const REANNOUNCE_SKIP_MS = 10000;
// The longest a stage report waits for the pumpkin to finish speaking.
const QUIET_WAIT_MS = 10000;

export class Scene {
  // onZone(zone) is called with { zoneTop, zoneBottom, zoneLeft, zoneRight }
  // when someone drags a new stage area on the camera view.
  constructor({ video, canvas, summary, settings, log, onZone, onPictureChange = () => {}, isTalking = () => false }) {
    this.video = video;
    this.onPictureChange = onPictureChange;
    this.lastArrivedAt = -Infinity;
    this.isTalking = isTalking;
    this.reports = Promise.resolve();
    this.canvas = canvas;
    this.summary = summary;
    this.log = log;
    this.stage = new Stage(settings);
    this.detector = null;
    this.timer = null;
    this.thumb = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
    this.detectMs = null; // moving average, shown so a machine's headroom can be checked
    this.warmup = 3; // the first frames include one-time setup (GPU shader compiles)
    this.#enableZoneDrawing(onZone);
  }

  // Drag across the camera view to mark the floor where visitors stand: their
  // feet are inside it when they're on stage.
  #enableZoneDrawing(onZone) {
    const point = (e) => {
      const r = this.canvas.getBoundingClientRect();
      const clamp = (v) => Math.min(1, Math.max(0, v));
      return { x: clamp((e.clientX - r.left) / r.width), y: clamp((e.clientY - r.top) / r.height) };
    };
    this.canvas.style.touchAction = "none";
    this.canvas.style.cursor = "crosshair";
    this.canvas.onpointerdown = (e) => {
      this.canvas.setPointerCapture(e.pointerId);
      this.drag = { from: point(e), to: point(e) };
    };
    this.canvas.onpointermove = (e) => {
      if (this.drag) this.drag.to = point(e);
    };
    // A touch the browser takes over (a scroll, a second finger) never ends in
    // pointerup; drop the drag rather than leave its box on screen.
    this.canvas.onpointercancel = () => {
      this.drag = null;
    };
    this.canvas.onpointerup = () => {
      const d = this.drag;
      this.drag = null;
      // A tap, or a sliver, isn't a stage.
      if (!d || Math.abs(d.to.x - d.from.x) < 0.05 || Math.abs(d.to.y - d.from.y) < 0.05) return;
      onZone?.({
        zoneTop: +Math.min(d.from.y, d.to.y).toFixed(2),
        zoneBottom: +Math.max(d.from.y, d.to.y).toFixed(2),
        zoneLeft: +Math.min(d.from.x, d.to.x).toFixed(2),
        zoneRight: +Math.max(d.from.x, d.to.x).toFixed(2),
      });
    };
  }

  // The GPU (WebGL) is usually several times faster; the CPU works everywhere.
  async load() {
    const files = await FilesetResolver.forVisionTasks(WASM);
    for (const delegate of ["GPU", "CPU"]) {
      try {
        this.detector = await ObjectDetector.createFromOptions(files, {
          baseOptions: { modelAssetPath: MODEL, delegate },
          runningMode: "VIDEO",
          scoreThreshold: 0.4,
          categoryAllowlist: ["person"],
        });
        this.delegate = delegate;
        this.log(`stage detection running on the ${delegate}`);
        return;
      } catch (e) {
        if (delegate === "CPU") throw e;
      }
    }
  }

  // Start watching a camera stream. Stage state carries over between calls.
  async attach(stream) {
    this.detach();
    const attached = (this.attached = {});
    this.video.srcObject = stream;
    await this.video.play().catch(() => {});
    // A later attach() or detach() while this one waited wins; two detection
    // loops would double the load and the arrival reports.
    if (attached !== this.attached) return;
    this.#schedule(FASTEST_MS);
  }

  #schedule(ms) {
    this.interval = ms;
    this.timer = setTimeout(() => {
      this.#frame();
      if (this.timer) this.#schedule(this.#nextInterval());
    }, ms);
  }

  #nextInterval() {
    if (this.detectMs == null) return FASTEST_MS;
    return Math.round(Math.min(SLOWEST_MS, Math.max(FASTEST_MS, this.detectMs * 2.5)));
  }

  detach() {
    this.attached = null;
    this.lastPixels = null; // the next call's first frame isn't a cut
    clearTimeout(this.timer);
    this.timer = null;
    this.video.srcObject = null;
  }

  set(name, value) {
    this.stage.set(name, value);
  }

  #frame() {
    const { videoWidth: w, videoHeight: h } = this.video;
    if (!this.detector || !w) return;
    const now = performance.now();
    const found = this.detector.detectForVideo(this.video, now).detections;
    const took = performance.now() - now;
    if (this.warmup > 0) this.warmup--;
    else this.detectMs = this.detectMs == null ? took : this.detectMs * 0.9 + took * 0.1;
    // A small copy of the frame for appearance signatures.
    const tw = LOOK_WIDTH;
    const th = Math.round((LOOK_WIDTH * h) / w);
    if (this.thumb.canvas.width !== tw || this.thumb.canvas.height !== th) {
      this.thumb.canvas.width = tw;
      this.thumb.canvas.height = th;
    }
    this.thumb.drawImage(this.video, 0, 0, tw, th);
    const pixels = this.thumb.getImageData(0, 0, tw, th).data;
    const change = frameChange(this.lastPixels, pixels, tw);
    const gain = frameGain(pixels);
    const cut = change >= CUT_CHANGE;
    if (change >= PICTURE_CHANGE) this.onPictureChange();
    this.lastPixels = pixels;
    if (cut) this.log("stage: the whole picture changed; starting over");
    const people = found.map((d) => {
      const box = {
        x: d.boundingBox.originX / w,
        y: d.boundingBox.originY / h,
        w: d.boundingBox.width / w,
        h: d.boundingBox.height / h,
      };
      return { ...box, look: appearance(pixels, tw, th, box, gain) };
    });
    const { events, tracks, show } = this.stage.update(now, people, { cut });
    for (const event of events) this.#report(event);
    this.#draw(tracks, show);
  }

  // Retried a few times: a lost "left" or "arrived" (say, during the agent's
  // restart after a deploy) would leave the pumpkin talking to the wrong group.
  // A new call (after a redial) knows nothing of a show already under way;
  // tell it about the people on stage now, as if they'd just stepped up.
  reannounce() {
    // Not if the stage already reported this group to the new call (a long
    // reconnect lets the old show end and a fresh "arrived" fire on its own).
    if (!this.stage.show || performance.now() - this.lastArrivedAt < REANNOUNCE_SKIP_MS) return;
    const people = this.stage.castPeople();
    this.#report({ type: "arrived", cast: people.length, people, reannounced: true });
  }

  // Reports go out one at a time, in order, and each waits for the pumpkin to
  // finish its sentence: a message into the call cuts off whatever it's
  // saying, and a show starting mid-word sounds broken.
  #report(event) {
    if (event.type === "arrived" && !event.reannounced) this.lastArrivedAt = performance.now();
    this.log(`stage: ${event.type} (cast of ${event.cast})`);
    this.reports = this.reports.then(() => this.#whenQuiet()).then(() => this.#send(event));
  }

  async #whenQuiet() {
    const started = performance.now();
    while (this.isTalking() && performance.now() - started < QUIET_WAIT_MS) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }

  async #send(event) {
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const response = await fetch("/eyes-scene", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(event),
        });
        if (response.ok) return;
      } catch (e) {
        if (attempt === 3) this.log("stage report failed:", e?.message || e);
      }
      await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
    }
  }

  #draw(tracks, show) {
    const { canvas, video } = this;
    if (canvas.width !== video.videoWidth) canvas.width = video.videoWidth;
    if (canvas.height !== video.videoHeight) canvas.height = video.videoHeight;
    const g = canvas.getContext("2d");
    g.clearRect(0, 0, canvas.width, canvas.height);
    // The stage area (where feet go), or the one being dragged.
    const s = this.stage.s;
    const W = canvas.width;
    const H = canvas.height;
    const d = this.drag;
    const zone = d
      ? { left: Math.min(d.from.x, d.to.x), right: Math.max(d.from.x, d.to.x), top: Math.min(d.from.y, d.to.y), bottom: Math.max(d.from.y, d.to.y) }
      : { left: s.zoneLeft, right: s.zoneRight, top: s.zoneTop, bottom: s.zoneBottom };
    const zx = zone.left * W;
    const zy = zone.top * H;
    const zw = (zone.right - zone.left) * W;
    const zh = (zone.bottom - zone.top) * H;
    g.fillStyle = "rgba(232,115,12,0.10)";
    g.fillRect(zx, zy, zw, zh);
    g.strokeStyle = "rgba(255,255,255,0.5)";
    g.setLineDash([6, 6]);
    g.lineWidth = 2;
    g.strokeRect(zx, zy, zw, zh);
    g.setLineDash([]);
    g.lineWidth = 3;
    g.font = "16px system-ui";
    for (const track of tracks) {
      if (track.lastSeen < performance.now() - 500) continue;
      const role = this.stage.role(track);
      const b = track.box;
      g.strokeStyle = g.fillStyle = COLORS[role];
      g.strokeRect(b.x * W, b.y * H, b.w * W, b.h * H);
      // Feet, and the feet-to-head height when the whole person is in frame.
      const m = measure(track);
      g.beginPath();
      g.arc((b.x + b.w / 2) * W, m.feetY * H, 6, 0, 2 * Math.PI);
      g.fill();
      g.fillText(`${role} ${track.id}${m.whole ? ` · ${Math.round(m.height * 100)}%` : ""}`, b.x * W + 4, b.y * H + 18);
    }
    // How long detection takes, and how often it runs as a result.
    const load = this.detectMs == null ? ""
      : ` · detection ${Math.round(this.detectMs)} ms on the ${this.delegate}, every ${this.interval} ms`;
    this.summary.textContent = (show.live
      ? `Show on: ${show.present} of ${show.cast} still here` + (show.audience ? `, ${show.audience} more watching` : "")
      : "Nobody on stage.") + load;
  }
}
