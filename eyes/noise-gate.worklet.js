// Noise gate for the mic: passes audio while its level is above the threshold
// and for `holdSeconds` after, and mutes it otherwise, so background noise
// never reaches the speech recognizer.
//
// A second input carries the pumpkin's own voice. While it's playing (and for
// `duckHoldSeconds` after, for the effects' tail and the room), the mic stays
// muted, so the speaker's sound in the mic never reaches the recognizer even
// when the browser's echo cancellation can't remove it. The cost: visitors
// can't interrupt the pumpkin mid-sentence.
//
// Posts the input level ~20 times a second for the page's meter.
const ENVELOPE_SECONDS = 0.01; // level smoothing
const ATTACK_SECONDS = 0.005; // fast open, so first syllables aren't clipped
const RELEASE_SECONDS = 0.15; // gentle close, so it doesn't click
const REPORT_SECONDS = 0.05;

const coefficient = (seconds) => 1 - Math.exp(-1 / (seconds * sampleRate));
const toDb = (level) => 20 * Math.log10(level + 1e-9);

class NoiseGate extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      { name: "thresholdDb", defaultValue: -45, minValue: -90, maxValue: 0, automationRate: "k-rate" },
      // Long enough to ride through pauses between words.
      { name: "holdSeconds", defaultValue: 0.4, minValue: 0, maxValue: 2, automationRate: "k-rate" },
      // The pumpkin counts as talking while its voice is above this level.
      { name: "duckDb", defaultValue: -50, minValue: -90, maxValue: 0, automationRate: "k-rate" },
      { name: "duckHoldSeconds", defaultValue: 0.3, minValue: 0, maxValue: 2, automationRate: "k-rate" },
    ];
  }

  constructor() {
    super();
    this.envelope = 0;
    this.voiceEnvelope = 0;
    this.gain = 0;
    this.holdLeft = 0;
    this.duckLeft = 0;
    this.peak = 0;
    this.sinceReport = 0;
    this.envelopeCoef = coefficient(ENVELOPE_SECONDS);
    this.attackCoef = coefficient(ATTACK_SECONDS);
    this.releaseCoef = coefficient(RELEASE_SECONDS);
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0][0];
    const voice = inputs[1]?.[0]; // absent until the pumpkin's voice is connected
    const output = outputs[0];
    if (!input) {
      for (const channel of output) channel.fill(0);
      return true;
    }
    const threshold = Math.pow(10, parameters.thresholdDb[0] / 20);
    const holdSamples = parameters.holdSeconds[0] * sampleRate;
    const duckThreshold = Math.pow(10, parameters.duckDb[0] / 20);
    const duckHoldSamples = parameters.duckHoldSeconds[0] * sampleRate;

    for (let n = 0; n < input.length; n++) {
      const sample = input[n];
      this.envelope += (Math.abs(sample) - this.envelope) * this.envelopeCoef;

      this.voiceEnvelope += (Math.abs(voice ? voice[n] : 0) - this.voiceEnvelope) * this.envelopeCoef;
      if (this.voiceEnvelope >= duckThreshold) this.duckLeft = duckHoldSamples;
      else if (this.duckLeft > 0) this.duckLeft--;

      if (this.duckLeft > 0) {
        // The pumpkin is talking: whatever the mic hears now is mostly its own
        // voice, so it neither opens the gate nor holds it open afterward.
        this.holdLeft = 0;
      } else if (this.envelope >= threshold) {
        this.holdLeft = holdSamples;
      } else if (this.holdLeft > 0) {
        this.holdLeft--;
      }

      const target = this.holdLeft > 0 ? 1 : 0;
      this.gain += (target - this.gain) * (target > this.gain ? this.attackCoef : this.releaseCoef);
      for (const channel of output) channel[n] = sample * this.gain;
      if (this.envelope > this.peak) this.peak = this.envelope;
    }

    this.sinceReport += input.length;
    if (this.sinceReport >= REPORT_SECONDS * sampleRate) {
      this.port.postMessage({ levelDb: toDb(this.peak), open: this.gain > 0.5, ducked: this.duckLeft > 0 });
      this.peak = 0;
      this.sinceReport = 0;
    }
    return true;
  }
}

registerProcessor("noise-gate", NoiseGate);
