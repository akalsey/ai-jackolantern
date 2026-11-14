// Delay-line pitch shifter: two read taps sweep through a short window half a
// cycle apart, and a sin²/cos² crossfade hides each tap's jump back. Replaces
// Tone.PitchShift, whose sweep rate is wrong for downward shifts (it computes
// 2^((n-1)/12) + 1 where 1 - 2^(n/12) is needed), so -3 came out as warble.
const BUFFER_SIZE = 1 << 16; // ~1.4 s at 48 kHz, far longer than any window

class PitchShifter extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      { name: "semitones", defaultValue: 0, minValue: -12, maxValue: 12, automationRate: "k-rate" },
      // Longer windows are smoother on vowels but smear consonants.
      { name: "windowSeconds", defaultValue: 0.06, minValue: 0.02, maxValue: 0.2, automationRate: "k-rate" },
    ];
  }

  constructor() {
    super();
    this.buffer = new Float32Array(BUFFER_SIZE);
    this.write = 0;
    this.phase = 0;
  }

  read(delay) {
    const pos = this.write - delay;
    const i = Math.floor(pos);
    const frac = pos - i;
    const a = this.buffer[i & (BUFFER_SIZE - 1)];
    const b = this.buffer[(i + 1) & (BUFFER_SIZE - 1)];
    return a + (b - a) * frac;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0][0];
    const output = outputs[0];
    const ratio = Math.pow(2, parameters.semitones[0] / 12);
    const windowSamples = parameters.windowSeconds[0] * sampleRate;
    // The delay grows by (1 - ratio) samples per sample, which plays the
    // buffer back at `ratio` speed. Phase is that growth as a fraction of the window.
    const step = (1 - ratio) / windowSamples;

    for (let n = 0; n < output[0].length; n++) {
      this.buffer[this.write & (BUFFER_SIZE - 1)] = input ? input[n] : 0;
      const p1 = this.phase;
      const p2 = (p1 + 0.5) % 1;
      const g1 = Math.sin(Math.PI * p1) ** 2;
      const sample = g1 * this.read(p1 * windowSamples + 1) + (1 - g1) * this.read(p2 * windowSamples + 1);
      for (const channel of output) channel[n] = sample;
      // Kept masked so it can't grow past int32 range over a long night.
      this.write = (this.write + 1) & (BUFFER_SIZE - 1);
      this.phase = (((this.phase + step) % 1) + 1) % 1;
    }
    return true;
  }
}

registerProcessor("pitch-shifter", PitchShifter);
