// Checks for eyes/picture.js: whole-picture changes and clothing colors that
// hold up at night. Run with: node tests/picture.test.mjs
import { appearance, frameChange } from "../eyes/picture.js";

let fails = 0;
const check = (name, cond) => {
  if (!cond) fails++;
  console.log(`${cond ? "ok  " : "FAIL"} ${name}`);
};

// A deterministic "scene": 96x72 RGBA with structure from a seed, scaled by brightness.
const W = 96, H = 72;
function scene(seed, brightness) {
  const px = new Uint8ClampedArray(W * H * 4);
  let s = seed;
  const rand = () => ((s = (s * 1103515245 + 12345) % 2147483648) / 2147483648);
  const blobs = Array.from({ length: 12 }, () => [rand() * W, rand() * H, 8 + rand() * 20, [rand(), rand(), rand()]]);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    let c = [0.2, 0.2, 0.25];
    for (const [bx, by, r, col] of blobs) if ((x - bx) ** 2 + (y - by) ** 2 < r * r) c = col;
    const i = (y * W + x) * 4;
    for (let k = 0; k < 3; k++) px[i + k] = c[k] * 255 * brightness;
    px[i + 3] = 255;
  }
  return px;
}

for (const b of [1, 0.3, 0.1]) {
  check(`brightness ${b}: two different pictures are a cut (${frameChange(scene(1, b), scene(2, b)).toFixed(2)})`,
    frameChange(scene(1, b), scene(2, b)) >= 0.6);
  check(`brightness ${b}: the same picture, slightly brighter, isn't (${frameChange(scene(1, b), scene(1, b * 1.08)).toFixed(3)})`,
    frameChange(scene(1, b), scene(1, b * 1.08)) < 0.1);
}
const box = { x: 0.2, y: 0.2, w: 0.3, h: 0.6 };
const bright = appearance(scene(3, 1), W, H, box);
const dark = appearance(scene(3, 0.1), W, H, box);
check("clothing colors hold steady as the picture darkens",
  bright.every((v, i) => Math.abs(v - dark[i]) < 0.05));
check("...and dark clothes aren't all crushed to black", Math.max(...dark) > 0.3);

// Night camera noise: the same dark scene, two frames with independent sensor
// noise. The thumbnail averages dozens of camera pixels each, so 0.01 (about
// 2.5 levels of 255) is a pessimistic amount at thumbnail scale.
function noisy(px, amount, seed) {
  let s = seed;
  const rand = () => ((s = (s * 1103515245 + 12345) % 2147483648) / 2147483648);
  const out = new Uint8ClampedArray(px);
  for (let i = 0; i < out.length; i++) if (i % 4 !== 3) out[i] = px[i] + (rand() - 0.5) * 2 * amount * 255 * Math.sqrt(3);
  return out;
}
const lookChange = (a, b) => a.reduce((sum, v, i) => sum + Math.abs(v - b[i]), 0) / a.length;
for (const b of [0.3, 0.1, 0.05]) {
  const f1 = noisy(scene(4, b), 0.01, 7), f2 = noisy(scene(4, b), 0.01, 8);
  const change = frameChange(f1, f2);
  check(`brightness ${b}: night noise alone is no cut (${change.toFixed(2)})`, change < 0.6);
  const look = lookChange(appearance(f1, W, H, box), appearance(f2, W, H, box));
  check(`brightness ${b}: the same person through noise stays the same person (${look.toFixed(3)} < 0.06)`, look < 0.06);
}

process.exit(fails);
