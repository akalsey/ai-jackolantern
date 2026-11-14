// Scripted scenes for eyes/stage.js. Run with: node tests/stage.test.mjs
import { Stage } from "../eyes/stage.js";
let fails = 0;
const check = (name, cond) => { fails += !cond; console.log((cond ? "ok   " : "FAIL ") + name); };
// A person standing with their feet at feetY (default: well inside the stage area).
const person = (cx, h = 0.5, feetY = 0.9) => ({ x: cx - 0.1, y: feetY - h, w: 0.2, h });

// Run a scene: frames(t) -> boxes, from t0 to t1 every 250 ms. Collect events.
// Real timers wander, so each tick lands 250 ms +/- 60 ms after the last.
let seed = 7;
const jitter = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5) * 120;
function run(stage, t0, t1, frames, every = 250) {
  const events = [];
  for (let t = t0; t <= t1; t += every + jitter()) {
    for (const e of stage.update(t, frames(t)).events) events.push({ t, ...e });
  }
  return events;
}

// 1. A passerby walks quickly across the frame: never a show.
let s = new Stage();
let ev = run(s, 0, 4000, (t) => [person(0.05 + 0.9 * (t / 4000))]);
check("passerby walking across starts nothing", ev.length === 0);

// 2. Someone standing still down the street: small, feet high in the frame,
// outside the stage area. No show.
s = new Stage();
ev = run(s, 0, 5000, () => [person(0.5, 0.2, 0.45)]);
check("distant still person (feet above the stage) starts nothing", ev.length === 0);

// 2b. Someone close but standing off to the side of the stage: no show.
s = new Stage();
ev = run(s, 0, 5000, () => [person(0.97)]);
check("feet outside the stage's sides start nothing", ev.length === 0);

// 3. A full scene.
s = new Stage();
// Two people step up together and stay.
ev = run(s, 0, 4000, () => [person(0.35), person(0.65)]);
check("two people standing close start a show with a cast of 2",
  ev.length === 1 && ev[0].type === "arrived" && ev[0].cast === 2);
check("the arrival carries each cast member's measurements",
  ev[0].people.length === 2 && ev[0].people.every((p) => p.height === 0.5 && p.feetY === 0.9 && p.whole) &&
  s.castPeople().length === 2);
// A third wanders over and joins.
ev = run(s, 4250, 8000, () => [person(0.35), person(0.65), person(0.85, 0.45)]);
const sum = s.summary(8000);
check("latecomer joins the audience, reported once as joined, measured with the cast",
  ev.length === 1 && ev[0].type === "joined" && ev[0].people.length === 3 && sum.cast === 2 && sum.audience === 1);
// Brief occlusion: the first cast member vanishes for 750 ms.
ev = run(s, 8250, 9000, (t) => (t < 9000 ? [person(0.65), person(0.85, 0.45)] : [person(0.35), person(0.65), person(0.85, 0.45)]));
ev = ev.concat(run(s, 9250, 11000, () => [person(0.35), person(0.65), person(0.85, 0.45)]));
check("brief occlusion: same show, cast intact", ev.length === 0 && s.summary(11000).present === 2);
// One of the cast leaves: half remain, the show goes on.
ev = run(s, 11250, 20000, () => [person(0.65), person(0.85, 0.45)]);
check("half the cast still here: show continues", ev.length === 0 && s.summary(20000).live);
// The rest of the cast leaves; the latecomer stays.
ev = run(s, 20250, 32000, () => [person(0.85, 0.45)]);
const left = ev.find((e) => e.type === "left");
const next = ev.find((e) => e.type === "arrived");
check("most of the cast gone: show ends", Boolean(left));
check("ended between 6.5 and 8.5 s after they left (gone 3 s + 4 s below half)", left && left.t - 20250 >= 6500 && left.t - 20250 <= 8500);
check("the latecomer becomes the next show's cast", next && next.cast === 1 && next.t >= left.t);

// 4. Empty yard after a show: just "left", nothing more.
s = new Stage();
run(s, 0, 3000, () => [person(0.5)]);
ev = run(s, 3250, 12000, () => []);
check("everyone walks away: one 'left', no new show", ev.length === 1 && ev[0].type === "left" && !s.summary(12000).live);

// 5. A cast member hidden for 2 s comes back as themselves, not a newcomer.
s = new Stage();
run(s, 0, 3000, () => [person(0.3), person(0.7)]);
ev = run(s, 3250, 5250, () => [person(0.7)]);
ev = ev.concat(run(s, 5500, 9000, () => [person(0.3), person(0.7)]));
const back = s.summary(9000);
check("2 s occlusion: same cast, no newcomer, no event", ev.length === 0 && back.present === 2 && back.audience === 0);

// 6. A cast member who hops around in front stays in the cast.
s = new Stage();
run(s, 0, 3000, () => [person(0.4), person(0.6)]);
ev = run(s, 3250, 15000, (t) => [person(0.4), person(0.6 + 0.12 * Math.sin(t / 150))]);
// A hop can briefly split one person into two tracks; the copy may be reported
// as "joined" (harmless: the server only acts on a joined small child), but the
// show neither ends nor restarts.
check("hopping cast member keeps the show going",
  ev.every((e) => e.type === "joined") && s.summary(15000).present === 2);


// 7. Head or feet out of frame: on stage, but not a usable height.
s = new Stage();
ev = run(s, 0, 3000, () => [person(0.5, 0.8, 1.0)]); // box runs into the bottom edge
check("feet cut off by the frame: on stage, height not whole", ev[0]?.type === "arrived" && ev[0].people[0].whole === false);

// 8. A slow machine detecting once a second still starts and ends shows.
s = new Stage();
ev = run(s, 0, 5000, () => [person(0.4), person(0.6)], 1000);
check("at 1 frame a second, a show starts", ev.length === 1 && ev[0].type === "arrived");
ev = run(s, 5500, 13000, () => [person(0.4), person(0.6), person(0.85, 0.45)], 1000);
check("at 1 frame a second, a latecomer is still reported as joined", ev.some((e) => e.type === "joined"));
ev = run(s, 14000, 24000, () => [], 1000);
check("at 1 frame a second, it ends when they leave", ev.length === 1 && ev[0].type === "left");

// 9. A new group stepping into the same spots (a camera cut) is a new show, not
// the old cast carrying on: they don't look the same.
const witch = [0.1, 0.1, 0.12, 0.15, 0.1, 0.1];
const ghost = [0.9, 0.9, 0.92, 0.85, 0.85, 0.88];
const skeleton = [0.2, 0.2, 0.2, 0.8, 0.8, 0.8];
const pumpkinSuit = [0.9, 0.5, 0.1, 0.2, 0.6, 0.2];
const dressed = (cx, look) => ({ ...person(cx), look });
s = new Stage();
ev = run(s, 0, 3000, () => [dressed(0.4, witch), dressed(0.6, ghost)]);
ev = run(s, 3250, 16000, () => [dressed(0.4, skeleton), dressed(0.6, pumpkinSuit)]);
const types = ev.map((e) => e.type).join(",");
check(`cut to a new group in the same spots: the show ends and a new one starts (${types})`, types === "left,arrived");

// 10. The same person under shifting light stays the same person.
s = new Stage();
run(s, 0, 3000, () => [dressed(0.5, witch)]);
ev = run(s, 3250, 12000, (t) => [dressed(0.5, witch.map((v) => v + 0.04 * Math.sin(t / 900)))]);
check("small appearance changes keep the same cast", ev.length === 0 && s.summary(12000).present === 1);

// 11. Feet below the stage's bottom edge don't count.
s = new Stage({ zoneTop: 0.4, zoneBottom: 0.7 });
ev = run(s, 0, 4000, () => [person(0.5, 0.5, 0.9)]);
check("feet below the stage's bottom edge start nothing", ev.length === 0);
s = new Stage({ zoneTop: 0.7, zoneBottom: 0.4 });
ev = run(s, 0, 4000, () => [person(0.5, 0.4, 0.6)]);
check("crossed top and bottom still make a stage", ev.length === 1);

// 12. A cut (the whole picture changes at once) ends the show immediately and
// the new people start a new one, even if they look like the old ones.
s = new Stage();
run(s, 0, 3000, () => [dressed(0.4, witch), dressed(0.6, ghost)]);
const cutEvents = s.update(3250, [dressed(0.4, witch), dressed(0.6, ghost)], { cut: true }).events;
check("a cut ends the show at once", cutEvents.length === 1 && cutEvents[0].type === "left");
ev = run(s, 3500, 7000, () => [dressed(0.4, witch), dressed(0.6, ghost)]);
check("and the people after the cut start a new show", ev.length === 1 && ev[0].type === "arrived" && ev[0].cast === 2);

process.exit(fails);
