import { PCMResampler } from './resampler.mjs';

class AvatarMicrophone extends AudioWorkletProcessor {
  constructor() { super(); this.resampler = new PCMResampler(sampleRate); }
  process(inputs) {
    const mono = inputs[0]?.[0];
    if (mono) for (const packet of this.resampler.push(mono)) this.port.postMessage(packet, [packet]);
    return true;
  }
}
registerProcessor('avatar-microphone', AvatarMicrophone);
