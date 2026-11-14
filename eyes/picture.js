// Comparing whole pictures and the people in them, in a way that works at night.
//
// A front yard after dark is mostly near-black, so raw pixel differences are
// tiny even when the picture changes completely, and everyone's clothes look the
// same dark gray. Both measures here first even out the picture's brightness
// (and, for whole-picture changes, its contrast), the way a camera's auto
// exposure would.

// Mean and spread of all color values in an RGBA pixel array.
function stats(pixels) {
  let sum = 0;
  let squares = 0;
  let n = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    for (let c = 0; c < 3; c++) {
      const v = pixels[i + c] / 255;
      sum += v;
      squares += v * v;
      n++;
    }
  }
  const mean = n ? sum / n : 0;
  return { mean, std: Math.sqrt(Math.max(0, squares / (n || 1) - mean * mean)) };
}

// Average BLOCK x BLOCK squares of an RGBA frame `width` pixels wide into a
// smaller RGB-only grid (as RGBA, alpha unused). Sensor noise is independent
// pixel to pixel and averages away; a real change of picture doesn't.
const BLOCK = 4;
function blocks(pixels, width) {
  const height = pixels.length / 4 / width;
  const bw = Math.floor(width / BLOCK);
  const bh = Math.floor(height / BLOCK);
  const out = new Float32Array(bw * bh * 4);
  for (let by = 0; by < bh; by++) {
    for (let bx = 0; bx < bw; bx++) {
      const o = (by * bw + bx) * 4;
      for (let y = by * BLOCK; y < (by + 1) * BLOCK; y++) {
        for (let x = bx * BLOCK; x < (bx + 1) * BLOCK; x++) {
          const i = (y * width + x) * 4;
          for (let c = 0; c < 3; c++) out[o + c] += pixels[i + c] / (BLOCK * BLOCK);
        }
      }
    }
  }
  return out;
}

// How different two same-sized frames are once each is normalized to its own
// brightness and contrast: about 1 for two unrelated pictures, a few tenths at
// most for the same scene, in daylight or dark.
export function frameChange(a, b, width = 96) {
  if (!a || !b || a.length !== b.length) return 0;
  const ga = blocks(a, width);
  const gb = blocks(b, width);
  const sa = stats(ga);
  const sb = stats(gb);
  // A floor on contrast, so a nearly flat night picture's noise isn't blown up.
  const da = Math.max(sa.std, 0.02);
  const db = Math.max(sb.std, 0.02);
  let sum = 0;
  let n = 0;
  for (let i = 0; i < ga.length; i += 4) {
    for (let c = 0; c < 3; c++) {
      sum += Math.abs((ga[i + c] / 255 - sa.mean) / da - (gb[i + c] / 255 - sb.mean) / db);
      n++;
    }
  }
  return n ? sum / n : 0;
}

// The brightness gain that makes a frame average mid-gray; see appearance().
export function frameGain(pixels) {
  return 0.5 / Math.max(stats(pixels).mean, 0.02);
}

// The average color of the top and bottom halves of a box: six numbers 0..1,
// enough to tell a witch from a skeleton standing in the same spot. Scaled so
// the whole picture averages mid-gray, so dark scenes still tell clothes apart.
export function appearance(pixels, width, height, box, gain = frameGain(pixels)) {
  const x0 = Math.max(0, Math.floor(box.x * width));
  const x1 = Math.min(width, Math.ceil((box.x + box.w) * width));
  const y0 = Math.max(0, Math.floor(box.y * height));
  const y1 = Math.min(height, Math.ceil((box.y + box.h) * height));
  const mid = Math.floor((y0 + y1) / 2);
  const half = (top, bottom) => {
    const sum = [0, 0, 0];
    let n = 0;
    for (let y = top; y < bottom; y++) {
      for (let x = x0; x < x1; x++) {
        const i = (y * width + x) * 4;
        sum[0] += pixels[i];
        sum[1] += pixels[i + 1];
        sum[2] += pixels[i + 2];
        n++;
      }
    }
    return sum.map((v) => (n ? Math.min(1, (v / n / 255) * gain) : 0));
  };
  return [...half(y0, mid), ...half(mid, y1)];
}
