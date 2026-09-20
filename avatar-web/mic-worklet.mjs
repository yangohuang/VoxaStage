import { PCMResampler } from './resampler.mjs';

class AvatarMicrophone extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.resampler = new PCMResampler(sampleRate);
    this.captureTiming = options?.processorOptions?.captureTiming === true;
    this.originFrame = null;
    this.expectedFrame = null;
    this.outputSamples = 0;
    this.inputSamples = 0;
    this.segmentStartSample = 0;
    this.segment = 0;
  }
  process(inputs) {
    const mono = inputs[0]?.[0];
    if (!mono?.length) return true;
    if (this.captureTiming) {
      if (this.originFrame === null) this.originFrame = currentFrame;
      else if (this.expectedFrame !== currentFrame) {
        this.segment++;
        this.segmentStartSample = this.inputSamples;
        this.originFrame = currentFrame;
      }
      this.expectedFrame = currentFrame + mono.length;
      this.inputSamples += mono.length;
    }
    for (const packet of this.resampler.push(mono)) {
      if (!this.captureTiming) { this.port.postMessage(packet, [packet]); continue; }
      const samples = packet.byteLength / 2;
      const first = this.outputSamples * sampleRate / 16000;
      const last = (this.outputSamples + samples - 1) * sampleRate / 16000;
      const valid = first >= this.segmentStartSample;
      // Frame coordinates describe the Web Audio graph, not physical input time.
      // A packet straddling a discontinuity has no single continuous mapping.
      const timing = {
        valid, segment: this.segment, inputRate: sampleRate, outputRate: 16000,
        outputStartSample: this.outputSamples, samples,
        firstInputFrame: valid ? this.originFrame + first - this.segmentStartSample : null,
        lastInputFrame: valid ? this.originFrame + last - this.segmentStartSample : null,
        blockStartFrame: currentFrame, processedThroughFrame: this.expectedFrame,
      };
      this.outputSamples += samples;
      this.port.postMessage({pcm: packet, timing}, [packet]);
    }
    return true;
  }
}
registerProcessor('avatar-microphone', AvatarMicrophone);
