import test from 'node:test';
import assert from 'node:assert/strict';
import { PCMResampler } from '../resampler.mjs';

test('48k microphone input becomes bounded little endian 16k PCM packets across arbitrary blocks', () => {
  const r = new PCMResampler(48000);
  const packets = [];
  for (let i = 0; i < 40; i++) packets.push(...r.push(new Float32Array(128).fill(0.5)));
  assert.equal(packets.length, 5);
  for (const packet of packets) {
    assert.equal(packet.byteLength, 640);
    const view = new DataView(packet);
    assert.equal(view.getInt16(0, true), 16384);
  }
});

test('16k samples retain continuity between worklet blocks and clip amplitude', () => {
  const r = new PCMResampler(16000);
  assert.equal(r.push(new Float32Array(128).fill(-2)).length, 0);
  assert.equal(r.push(new Float32Array(128).fill(2)).length, 0);
  const [packet] = r.push(new Float32Array(128).fill(0));
  const view = new DataView(packet);
  assert.equal(view.getInt16(127 * 2, true), -32768);
  assert.equal(view.getInt16(128 * 2, true), 32767);
  assert.equal(view.getInt16(256 * 2, true), 0);
});
