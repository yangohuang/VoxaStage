import test from 'node:test';
import assert from 'node:assert/strict';
import { AvatarPlayer, decodeMedia, decode2DMedia } from '../audio.mjs';

class AudioContextFake {
  constructor() { this.currentTime = 0; this.state = 'running'; this.destination = {}; this.created = []; }
  async resume() {}
  createBuffer(_channels, samples, rate) { return { samples, rate, copyToChannel() {} }; }
  createBufferSource() {
    const source = { stopped: false, disconnected: false, connect() {}, disconnect() { this.disconnected = true; },
      start(when) { this.when = when; }, stop() { this.stopped = true; } };
    this.created.push(source); return source;
  }
}
globalThis.AudioContext = AudioContextFake;
const meta = { type: 'avatar_meta', generation: 0, clip_id: 'a', sample_rate: 24000, fps: 30, vertex_count: 3, faces: [[0, 1, 2]] };
function media(index = 0) {
  return { type: 'media', generation: 0, clip_id: 'a', start_sample: index * 800, frame_index: index, pts: index / 30,
    audio: Buffer.alloc(1600).toString('base64'), vertices: Buffer.alloc(36).toString('base64') };
}

test('PCM decoding uses signed little endian samples and preserves float32 mesh coordinates', () => {
  const pcm = Buffer.from([0, 128, 255, 127]);
  const vertices = Buffer.alloc(12); vertices.writeFloatLE(1.5, 0); vertices.writeFloatLE(-2, 4);
  const decoded = decodeMedia({ ...media(), audio: pcm.toString('base64'), vertices: vertices.toString('base64') });
  assert.deepEqual([...decoded.pcm], [-1, 32767 / 32768]);
  assert.deepEqual([...decoded.vertices], [1.5, -2, 0]);
});

test('local stop immediately stops scheduled AudioBufferSources and waits for server generation', async t => {
  const states = [], frames = [];
  const p = new AvatarPlayer({ onState: s => states.push(s), onFrame: v => frames.push(v), onError: e => { throw e; } });
  t.after(() => clearInterval(p.timer));
  await p.unlock(); p.reset(0); p.handle(meta);
  for (let i = 0; i < 4; i++) p.handle(media(i));
  assert.equal(p.sources.size, 4);
  p.context.currentTime = 0.04; p.tick();
  assert.equal(p.metrics.playing, true);
  assert.deepEqual(states, ['started']);
  assert.equal(frames.length, 1);
  const sources = [...p.sources]; p.stop();
  assert.ok(sources.every(s => s.stopped && s.disconnected));
  assert.equal(p.metrics.playing, false);
  assert.equal(p.metrics.generation, 0);
  p.handle({ ...media(4), audio: 'invalid base64!' });
  assert.equal(p.sources.size, 0);
  assert.equal(p.metrics.droppedStale, 1);
  p.reset(1);
  assert.equal(p.metrics.pendingFrames, 0);
});

test('short clip reports ended only when audio sources finish and the clock reaches the end', async t => {
  const states = [];
  const p = new AvatarPlayer({ onState: s => states.push(s), onFrame() {}, onError: e => { throw e; } });
  t.after(() => clearInterval(p.timer));
  await p.unlock(); p.reset(0); p.handle(meta); p.handle(media());
  p.handle({ type: 'clip_end', generation: 0, clip_id: 'a', total_samples: 800 });
  p.context.currentTime = 0.03; p.tick();
  p.context.currentTime = 1; p.tick();
  assert.deepEqual(states, ['started']);
  p.context.created[0].onended(); p.tick();
  assert.deepEqual(states, ['started', 'ended']);
});

test('2D JPEG media decodes before it enters the audio timeline and rejects malformed images', async () => {
  const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64');
  const bitmap = { width: 320, height: 240, close() {} };
  const decoded = await decode2DMedia({
    type: 'media', generation: 0, clip_id: 'portrait', start_sample: 0, frame_index: 0, pts: 0,
    audio: Buffer.alloc(1600).toString('base64'), image: jpeg,
  }, async () => bitmap);
  assert.equal(decoded.image, bitmap);
  await assert.rejects(() => decode2DMedia({ audio: Buffer.alloc(2).toString('base64'), image: Buffer.from('not jpeg').toString('base64') }, async () => bitmap), /JPEG/);
});

test('late 2D decodes after an interrupt are disposed without scheduling audio', async t => {
  let resolveImage;
  const bitmap = { width: 320, height: 240, closed: false, close() { this.closed = true; } };
  const p = new AvatarPlayer({ onState() {}, onFrame() {}, onError: e => { throw e; }, decodeImage: () => new Promise(resolve => { resolveImage = resolve; }) });
  t.after(() => clearInterval(p.timer));
  await p.unlock(); p.reset(0);
  p.handle({ type: 'avatar_meta', generation: 0, clip_id: 'portrait', kind: '2d', sample_rate: 24000, fps: 30, width: 320, height: 240, codec: 'jpeg' });
  p.handle({ type: 'media', generation: 0, clip_id: 'portrait', start_sample: 0, frame_index: 0, pts: 0, audio: Buffer.alloc(1600).toString('base64'), image: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') });
  p.stop(); resolveImage(bitmap);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(bitmap.closed, true);
  assert.equal(p.sources.size, 0);
});

test('2D frames retain wire order when JPEG decoding completes out of order', async t => {
  const resolvers = [];
  const p = new AvatarPlayer({ onState() {}, onFrame() {}, onError: e => { throw e; }, decodeImage: () => new Promise(resolve => resolvers.push(resolve)) });
  t.after(() => clearInterval(p.timer));
  await p.unlock(); p.reset(0);
  p.handle({ type: 'avatar_meta', generation: 0, clip_id: 'portrait', kind: '2d', sample_rate: 24000, fps: 30, width: 1, height: 1, codec: 'jpeg' });
  for (let index = 0; index < 2; index++) p.handle({ type: 'media', generation: 0, clip_id: 'portrait', start_sample: index * 800, frame_index: index, pts: index / 30, audio: Buffer.alloc(1600).toString('base64'), image: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') });
  p.handle({ type: 'clip_end', generation: 0, clip_id: 'portrait', total_samples: 1600 });
  resolvers[1]({ width: 1, height: 1, close() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(p.timeline.pendingFrames, 0);
  resolvers[0]({ width: 1, height: 1, close() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(p.timeline.clips[0].nextFrame, 2);
  assert.equal(p.timeline.clips[0].ended, true);
  assert.equal(p.context.created.length, 2);
});

test('2D clip_end waits for pending JPEG decodes and reset closes already decoded queued images', async t => {
  const resolvers = [], errors = [];
  const p = new AvatarPlayer({ onState() {}, onFrame() {}, onError: e => errors.push(e), decodeImage: () => new Promise(resolve => resolvers.push(resolve)) });
  t.after(() => clearInterval(p.timer));
  await p.unlock(); p.reset(0);
  p.handle({ type: 'avatar_meta', generation: 0, clip_id: 'portrait', kind: '2d', sample_rate: 24000, fps: 25, width: 1, height: 1, codec: 'jpeg' });
  for (let i = 0; i < 2; i++) p.handle({ type: 'media', generation: 0, clip_id: 'portrait', start_sample: i * 960, frame_index: i, pts: i / 25,
    audio: Buffer.alloc(1920).toString('base64'), image: Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString('base64') });
  assert.doesNotThrow(() => p.handle({ type: 'clip_end', generation: 0, clip_id: 'portrait', total_samples: 1920 }));
  const second = { width: 1, height: 1, closed: false, close() { this.closed = true; } };
  resolvers[1](second); await new Promise(resolve => setImmediate(resolve));
  assert.equal(p.timeline.clips[0].ended, false);
  p.stop();
  assert.equal(second.closed, true);
  resolvers[0]({ width: 1, height: 1, close() {} }); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(errors, []);
});
