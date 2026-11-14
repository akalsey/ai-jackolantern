// The pumpkin's voice: pitched down for size, a little chorus, a lowpass for
// the hollow-gourd sound, and a short bright reverb. "Inside a gourd," not
// "inside a cathedral."
import * as Tone from "https://cdn.jsdelivr.net/npm/tone@15.1.22/+esm";

export { Tone };

const PITCH_SHIFTER_URL = new URL("./pitch-shifter.worklet.js", import.meta.url).href;

// Tone's addAudioWorkletModule loads only the first module it's ever given and
// silently ignores later URLs, so each worklet file is loaded here instead.
const loadedWorklets = new WeakMap();
export function loadWorklet(context, url) {
  const raw = context.rawContext;
  if (!loadedWorklets.has(raw)) loadedWorklets.set(raw, new Map());
  const modules = loadedWorklets.get(raw);
  if (!modules.has(url)) modules.set(url, raw.audioWorklet.addModule(url));
  return modules.get(url);
}

export const DEFAULT_EFFECTS = {
  // Past -4 it goes muddy; intelligibility over crowd noise matters more than depth.
  pitchSemitones: -3,
  chorusMix: 0.3,
  // Shaves TTS sibilance and suggests something hollow.
  lowpassHz: 6000,
  // Small room. Long tails tip into full spooky.
  reverbSeconds: 1.0,
  reverbMix: 0.2,
};

export async function buildVoiceChain(settings = {}) {
  const s = { ...DEFAULT_EFFECTS, ...settings };
  const context = Tone.getContext();
  await loadWorklet(context, PITCH_SHIFTER_URL);

  const input = new Tone.Gain();
  const pitch = context.createAudioWorkletNode("pitch-shifter", { outputChannelCount: [1] });
  pitch.parameters.get("semitones").value = s.pitchSemitones;
  const chorus = new Tone.Chorus({ frequency: 0.6, delayTime: 20, depth: 0.5, wet: s.chorusMix }).start();
  const filter = new Tone.Filter(s.lowpassHz, "lowpass");
  const reverb = new Tone.Reverb({ decay: s.reverbSeconds, wet: s.reverbMix });
  await reverb.ready;
  const output = new Tone.Gain();

  Tone.connect(pitch, chorus);
  chorus.chain(filter, reverb, output);

  const setters = {
    pitchSemitones: (v) => { pitch.parameters.get("semitones").value = v; },
    chorusMix: (v) => { chorus.wet.value = v; },
    lowpassHz: (v) => { filter.frequency.value = v; },
    reverbSeconds: (v) => { reverb.decay = v; },
    reverbMix: (v) => { reverb.wet.value = v; },
  };

  const chain = {
    input,
    output,
    // Live adjustment from the tuning sliders.
    set(name, value) {
      setters[name]?.(value);
    },
    // Bypass for A/B listening.
    setEnabled(on) {
      input.disconnect();
      if (on) Tone.connect(input, pitch);
      else input.connect(output);
    },
  };
  chain.setEnabled(true);
  return chain;
}
