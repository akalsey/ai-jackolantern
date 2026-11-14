// Every adjustable setting, shared by the pumpkin page and the phone remote
// so both show the same sliders with the same ranges.

export const CONTROLS = {
  effects: [
    { name: "pitchSemitones", label: "Pitch", min: -6, max: 0, step: 0.5 },
    { name: "chorusMix", label: "Chorus mix", min: 0, max: 0.6, step: 0.05 },
    { name: "lowpassHz", label: "Lowpass", min: 2000, max: 12000, step: 250 },
    { name: "reverbSeconds", label: "Reverb length", min: 0.4, max: 2.5, step: 0.1 },
    { name: "reverbMix", label: "Reverb mix", min: 0, max: 0.5, step: 0.05 },
  ],
  // The stage area (where visitors' feet are when they're on stage) and a floor
  // on how small a person can look and still count.
  stage: [
    { name: "zoneTop", label: "Stage top", min: 0, max: 0.95, step: 0.05 },
    { name: "zoneBottom", label: "Stage bottom", min: 0.05, max: 1, step: 0.05 },
    { name: "zoneLeft", label: "Stage left", min: 0, max: 0.5, step: 0.05 },
    { name: "zoneRight", label: "Stage right", min: 0.5, max: 1, step: 0.05 },
    { name: "minHeight", label: "Min size", min: 0.05, max: 0.5, step: 0.05 },
  ],
  gate: [
    { name: "thresholdDb", label: "Threshold", min: -80, max: -10, step: 1 },
    { name: "holdSeconds", label: "Hold", min: 0.1, max: 1.5, step: 0.05 },
  ],
};

const READOUTS = {
  pitchSemitones: (v) => `${v} st`,
  lowpassHz: (v) => `${(v / 1000).toFixed(2)} kHz`,
  reverbSeconds: (v) => `${v} s`,
  thresholdDb: (v) => `${v} dB`,
  minHeight: (v) => `${Math.round(v * 100)}% tall`,
  zoneTop: (v) => `${Math.round(v * 100)}% down`,
  zoneBottom: (v) => `${Math.round(v * 100)}% down`,
  zoneLeft: (v) => `${Math.round(v * 100)}% in`,
  zoneRight: (v) => `${Math.round(v * 100)}% in`,
  holdSeconds: (v) => `${v} s`,
};

export const readout = (name, value) => (READOUTS[name] ?? ((v) => `${Math.round(v * 100)}%`))(value);

// Builds one labeled slider per spec into `container`. Returns { name: { input, output } }.
export function renderSliders(container, specs) {
  const sliders = {};
  for (const spec of specs) {
    const label = document.createElement("label");
    label.className = "slider";
    const input = document.createElement("input");
    Object.assign(input, { type: "range", min: spec.min, max: spec.max, step: spec.step });
    const output = document.createElement("output");
    label.append(spec.label, input, output);
    container.append(label);
    sliders[spec.name] = { input, output };
  }
  return sliders;
}

// The meter's scale, shared so both pages draw the mic level the same way.
export const METER_FLOOR_DB = -80;
export const meterPercent = (db) =>
  `${Math.round(Math.max(0, Math.min(1, (db - METER_FLOOR_DB) / -METER_FLOOR_DB)) * 100)}%`;
