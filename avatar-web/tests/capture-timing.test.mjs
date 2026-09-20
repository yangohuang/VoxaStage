import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { PCMResampler } from '../resampler.mjs';

const code = fs.readFileSync(new URL('../mic-worklet.mjs', import.meta.url), 'utf8')
  .replace("import { PCMResampler } from './resampler.mjs';", '');
function processor(rate, timing) {
  const sent = [];
  let Processor;
  const scope = { PCMResampler, sampleRate: rate, currentFrame: 4096,
    AudioWorkletProcessor: class { constructor() { this.port = {postMessage: (data) => sent.push(data)}; } },
    registerProcessor(_name, value) { Processor = value; },
  };
  vm.runInNewContext(code, scope);
  return {scope, sent, instance: new Processor({processorOptions: {captureTiming: timing}})};
}

for (const rate of [16000, 44100, 48000]) {
  test(`diagnostic packets preserve PCM and input-frame mapping at ${rate}Hz`, () => {
    const plain = processor(rate, false), timed = processor(rate, true);
    for (let block = 0; block < 90; block++) {
      const input = Float32Array.from({length: 128}, (_, i) => Math.sin((block * 128 + i) / 31));
      for (const p of [plain, timed]) { p.instance.process([[input]]); p.scope.currentFrame += 128; }
    }
    assert.ok(timed.sent.length > 5);
    assert.equal(timed.sent.length, plain.sent.length);
    for (let index = 0; index < timed.sent.length; index++) {
      const {pcm, timing} = timed.sent[index];
      assert.ok(pcm instanceof ArrayBuffer, 'timed mode wraps the unchanged PCM buffer');
      assert.deepEqual(new Uint8Array(pcm), new Uint8Array(plain.sent[index]));
      assert.equal(timing.valid, true);
      assert.equal(timing.inputRate, rate);
      assert.equal(timing.outputRate, 16000);
      assert.equal(timing.outputStartSample, index * 320);
      assert.equal(timing.samples, 320);
      assert.equal(timing.firstInputFrame, 4096 + index * 320 * rate / 16000);
      assert.equal(timing.lastInputFrame, 4096 + (index * 320 + 319) * rate / 16000);
      assert.ok(Math.floor(timing.lastInputFrame) + 1 < timing.processedThroughFrame);
      assert.equal(timing.processedThroughFrame - timing.blockStartFrame, 128);
    }
  });
}

test('only a packet crossing missing input has invalid mapping; later packets recover without changing audio', () => {
  const timed = processor(16000, true), plain = processor(16000, false);
  for (const p of [timed, plain]) {
    p.instance.process([[new Float32Array(128).fill(0.2)]]); p.scope.currentFrame += 128;
    p.instance.process([]); p.scope.currentFrame += 128;
    for (let i = 0; i < 8; i++) { p.instance.process([[new Float32Array(128).fill(0.7)]]); p.scope.currentFrame += 128; }
  }
  assert.ok(timed.sent.length >= 3);
  timed.sent.forEach((packet, i) => {
    assert.equal(packet.timing.valid, i > 0);
    assert.equal(packet.timing.segment, 1);
    assert.equal(packet.timing.firstInputFrame, i === 0 ? null : 4096 + 128 + i * 320);
    assert.equal(packet.timing.lastInputFrame, i === 0 ? null : 4096 + 128 + i * 320 + 319);
    assert.deepEqual(new Uint8Array(packet.pcm), new Uint8Array(plain.sent[i]));
  });
});

test('Chromium startup frame jump invalidates the crossing packet and starts a new timing segment', () => {
  const p = processor(48000, true);
  p.instance.process([[new Float32Array(128)]]); p.scope.currentFrame += 384;
  for (let i = 0; i < 20; i++) { p.instance.process([[new Float32Array(128)]]); p.scope.currentFrame += 128; }
  assert.ok(p.sent.length > 0);
  assert.equal(p.sent[0].timing.valid, false);
  assert.equal(p.sent[0].timing.firstInputFrame, null);
  assert.equal(p.sent[1].timing.valid, true);
  assert.equal(p.sent[1].timing.firstInputFrame, 4096 + 256 + 960);
  assert.equal(p.sent[1].timing.segment, 1);
});

test('empty callbacks before first input do not invalidate the first capture epoch', () => {
  const p = processor(16000, true);
  p.instance.process([]); p.scope.currentFrame += 128;
  for (let i = 0; i < 4; i++) { p.instance.process([[new Float32Array(128)]]); p.scope.currentFrame += 128; }
  assert.equal(p.sent[0].timing.valid, true);
  assert.equal(p.sent[0].timing.firstInputFrame, 4224);
});

test('44.1kHz ramp values independently agree with the advertised fractional input positions', () => {
  const p = processor(44100, true);
  for (let block = 0; block < 100; block++) {
    const ramp = Float32Array.from({length:128}, (_, i) => (block * 128 + i) / 20000);
    p.instance.process([[ramp]]); p.scope.currentFrame += 128;
  }
  for (const {pcm, timing} of p.sent) {
    const view = new DataView(pcm);
    for (const sample of [0, 31, 319]) {
      const coordinate = timing.firstInputFrame + sample * timing.inputRate / timing.outputRate;
      const expected = Math.round((coordinate - 4096) / 20000 * 32767);
      assert.ok(Math.abs(view.getInt16(sample * 2, true) - expected) <= 1);
    }
  }
});

test('44.1kHz multiple gaps preserve resampling phase while frame mapping follows each real segment', () => {
  const p = processor(44100, true), frames = [], segments = [];
  let segment = 0;
  for (let block = 0; block < 300; block++) {
    if (block > 0 && (block % 29 === 0 || block === 30)) {
      p.scope.currentFrame += 256; segment++;
    }
    const ramp = Float32Array.from({length:128}, (_, i) => (frames.length + i) / 50000);
    for (let i = 0; i < 128; i++) { frames.push(p.scope.currentFrame + i); segments.push(segment); }
    p.instance.process([[ramp]]); p.scope.currentFrame += 128;
  }
  let valid = 0, invalid = 0;
  for (const {pcm, timing} of p.sent) {
    const start = timing.outputStartSample * 44100 / 16000;
    const end = (timing.outputStartSample + 319) * 44100 / 16000;
    const expectedValid = segments[Math.floor(start)] === segments[Math.floor(end) + 1];
    assert.equal(timing.valid, expectedValid);
    const view = new DataView(pcm);
    for (const sample of [0, 31, 319]) {
      const position = start + sample * 44100 / 16000;
      assert.ok(Math.abs(view.getInt16(sample * 2, true) - Math.round(position / 50000 * 32767)) <= 1);
    }
    if (expectedValid) {
      valid++;
      const coordinate = position => frames[Math.floor(position)] + position - Math.floor(position);
      assert.ok(Math.abs(timing.firstInputFrame - coordinate(start)) < 1e-8);
      assert.ok(Math.abs(timing.lastInputFrame - coordinate(end)) < 1e-8);
      assert.equal(timing.segment, segments[Math.floor(start)]);
    } else { invalid++; assert.equal(timing.firstInputFrame, null); assert.equal(timing.lastInputFrame, null); }
  }
  assert.ok(valid > 20 && invalid > 5);
});
