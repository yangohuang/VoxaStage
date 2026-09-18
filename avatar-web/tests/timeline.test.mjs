import test from 'node:test';
import assert from 'node:assert/strict';
import { Timeline } from '../timeline.mjs';

const meta = (clip_id = 'a', generation = 0) => ({ clip_id, generation, sample_rate: 24000, fps: 30, vertex_count: 3, faces: [[0, 1, 2]] });
const packet = (index, clip_id = 'a', generation = 0) => ({ generation, clip_id, start_sample: index * 800, frame_index: index, pts: index / 30, pcm: new Float32Array(800), vertices: new Float32Array(9).fill(index) });
function setup(options) { const q = new Timeline(options); q.reset(0); q.addMeta(meta()); return q; }

test('buffers 120ms and schedules only inside the 350ms horizon', () => {
  const q = setup();
  q.push(packet(0)); q.push(packet(1)); q.push(packet(2));
  assert.equal(q.advance(10).jobs.length, 0);
  for (let i = 3; i < 30; i++) q.push(packet(i));
  const out = q.advance(10);
  assert.ok(out.jobs.length >= 4 && out.jobs.length <= 11);
  assert.equal(out.frame, null);
  assert.ok(out.jobs.every(j => j.when <= 10.35));
  assert.equal(q.advance(10.04).frame.frame_index, 0);
  assert.equal(q.playing, true);
});

test('reset and local interrupt reject old packets without guessing the next generation', () => {
  const q = setup(); q.push(packet(0)); q.end({ ...meta(), total_samples: 800 }); q.advance(1);
  q.suspend();
  assert.equal(q.generation, 0);
  assert.equal(q.push(packet(1)), false);
  assert.deepEqual(q.advance(2).jobs, []);
  q.reset(2);
  assert.equal(q.push(packet(0)), false);
  assert.equal(q.addMeta(meta('old', 0)), false);
  assert.equal(q.reset(1), false);
  assert.equal(q.generation, 2);
  assert.equal(q.advance(2).frame, null);
});

test('completed short clips play, successive clips remain serial and retired clips never revive', () => {
  const q = setup(); q.push(packet(0)); q.end({ ...meta(), total_samples: 800 });
  q.addMeta(meta('b')); q.push(packet(0, 'b')); q.end({ ...meta('b'), total_samples: 800 });
  const out = q.advance(5);
  assert.equal(out.jobs.length, 2);
  assert.ok(out.jobs[1].when >= out.jobs[0].when + 800 / 24000 - 1e-9);
  assert.equal(q.advance(5.07).frame.clip_id, 'b');
  q.advance(6);
  assert.equal(q.playing, false);
  assert.equal(q.addMeta(meta()), false);
  assert.equal(q.push(packet(1)), false);
});

test('underrun shifts audio and vertices together to the AudioContext clock', () => {
  const q = setup();
  for (let i = 0; i < 4; i++) q.push(packet(i));
  q.advance(1); q.advance(2);
  q.push(packet(4));
  const out = q.advance(3);
  assert.equal(out.jobs.length, 1);
  assert.ok(out.jobs[0].when >= 3);
  assert.equal(out.frame, null);
  assert.equal(q.advance(3.04).frame.frame_index, 4);
});

test('queue limits fail closed before memory can grow without bound', () => {
  const q = setup({ maxFrames: 4 });
  for (let i = 0; i < 4; i++) q.push(packet(i));
  assert.throws(() => q.push(packet(4)), /缓冲/);
  assert.equal(q.pendingFrames, 4);
});

test('out of order PCM is rejected before it can desynchronize the mesh', () => {
  const q = setup();
  assert.throws(() => q.push(packet(1)), /连续/);
  q.push(packet(0));
  assert.throws(() => q.end({ ...meta(), total_samples: 1600 }), /长度/);
});
