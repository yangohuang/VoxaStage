import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const moduleBody = readFileSync(new URL('./playback.mjs', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

// Execute the actual lab module with browser boundaries replaced by deterministic
// mocks. Player counters deliberately survive reset, as in AvatarPlayer.
async function harness({ unlock = async () => {}, outcomes = ['passed'] } = {}) {
  let now = 100;
  const sockets = [], renderCompletions = [], nodes = new Map();
  const input = { id: 'sample', audio_duration_s: 0.04, pcm_sha256: 'fixed-pcm', input_sha256: 'fixed-input' };
  const config = { providers: ['mock'], cases: [input], source_sha256: { 'audio.mjs': 'source-hash' } };
  const document = { getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {
      disabled: false, hidden: false, textContent: '', value: '',
      add(option) { this.value ||= option.value; },
    });
    return nodes.get(id);
  } };
  const window = { addEventListener() {} };
  class MockRenderer {
    setMeta() {}
    render() { now += 7; renderCompletions.push(now); }
  }
  class MockPlayer {
    constructor(callbacks) {
      this.callbacks = callbacks;
      this.counters = { renderedFrames: 0, scheduledSamples: 0, droppedStale: 0 };
      this.stopCalls = 0;
      this.sources = new Set();
      this.decodeQueue = [];
      this.timeline = {
        clips: [],
        push: packet => { this.pending = packet; return true; },
        advance: () => ({ frame: this.pending }),
      };
      this.context = {
        currentTime: 1,
        createBufferSource: () => ({ buffer: null, start() { now += 3; } }),
      };
    }
    async unlock() { await unlock(); }
    reset(generation) { this.generation = generation; this.timeline.clips = []; }
    stop() { this.stopCalls++; this.timeline.clips = []; this.sources.clear(); }
    get metrics() { return { ...this.counters, generation: this.generation }; }
    handle(message) {
      if (message.type === 'avatar_meta') {
        this.meta = message;
        this.timeline.clips.push(message.clip_id);
      } else if (message.type === 'media') {
        this.timeline.push(message);
        const { frame } = this.timeline.advance(this.context.currentTime);
        const source = this.context.createBufferSource();
        source.buffer = { duration: 0.04 };
        source.start(this.context.currentTime);
        this.counters.scheduledSamples += 960;
        this.callbacks.onFrame(frame, this.meta);
        this.counters.renderedFrames++;
        this.counters.droppedStale += 2;
      } else if (message.type === 'clip_end') {
        this.timeline.clips = [];
      }
    }
  }
  class MockSocket {
    constructor(url) {
      this.url = url;
      this.closed = false;
      const outcome = outcomes[sockets.length] ?? 'passed';
      sockets.push(this);
      queueMicrotask(() => {
        if (this.closed) return;
        const send = message => this.onmessage({ data: JSON.stringify(message) });
        if (outcome === 'passed') {
          send({ type: 'avatar_meta', kind: '2d', adapter_elapsed_ms: 1 });
          send({ type: 'media', frame_index: 0, adapter_elapsed_ms: 2 });
          send({ type: 'clip_end', adapter_elapsed_ms: 3 });
        }
        send({ type: 'stream_end', status: outcome, ...(outcome === 'failed' ? { error_type: 'BackendError' } : {}) });
      });
    }
    close() { this.closed = true; this.onclose?.(); }
  }
  const dependencies = {
    AvatarPlayer: MockPlayer, HeadRenderer: MockRenderer, PortraitRenderer: MockRenderer,
    document, window, Option: class { constructor(label, value) { this.value = value; } },
    fetch: async () => ({ json: async () => config }),
    performance: { now: () => now }, location: { origin: 'http://127.0.0.1:18424' },
    WebSocket: MockSocket,
    setTimeout(callback, milliseconds) { now += milliseconds; queueMicrotask(callback); },
    createImageBitmap: async () => ({ width: 4, height: 4 }), Blob,
  };
  await new AsyncFunction(...Object.keys(dependencies), moduleBody)(...Object.values(dependencies));
  return { lab: window.lab, nodes, sockets, renderCompletions, input, config };
}

test('reserve a run before audio unlock so a concurrent start cannot reset it', async () => {
  const gate = deferred();
  const env = await harness({ unlock: () => gate.promise });
  const first = env.lab.run();
  const originalRow = env.lab.last;
  assert.equal(env.nodes.get('run').disabled, true);
  assert.equal(env.sockets.length, 0);
  await assert.rejects(env.lab.run(), /Already running/);
  assert.equal(env.lab.last, originalRow);
  assert.equal(env.lab.player.stopCalls, 0);
  gate.resolve();
  assert.equal((await first).status, 'passed');
  assert.equal(env.sockets.length, 1);
});

test('an unlock failure retains provenance and releases the run for retry', async () => {
  let attempts = 0;
  const env = await harness({ unlock: async () => {
    if (++attempts === 1) throw new Error('AudioUnavailable');
  } });
  await assert.rejects(env.lab.run(), /AudioUnavailable/);
  const failed = env.lab.last;
  assert.equal(failed.status, 'failed');
  assert.deepEqual(failed.input, env.input);
  assert.deepEqual(failed.provenance, env.config.source_sha256);
  assert.ok(failed.finishedMs >= failed.startedMs);
  assert.equal(env.nodes.get('run').disabled, false);
  assert.equal(env.lab.player.stopCalls, 1);
  assert.equal(env.sockets.length, 0);
  assert.equal((await env.lab.run()).status, 'passed');
});

test('a stream failure retains provenance, closes resources, and allows retry', async () => {
  const env = await harness({ outcomes: ['failed', 'passed'] });
  await assert.rejects(env.lab.run(), /BackendError/);
  const failed = env.lab.last;
  assert.equal(failed.status, 'failed');
  assert.deepEqual(failed.input, env.input);
  assert.deepEqual(failed.provenance, env.config.source_sha256);
  assert.ok(failed.finishedMs >= failed.startedMs);
  assert.equal(env.sockets[0].closed, true);
  assert.equal(env.lab.player.stopCalls, 1);
  assert.equal(env.nodes.get('run').disabled, false);
  assert.equal('stop' in failed, false);
  assert.equal((await env.lab.run()).status, 'passed');
});

test('successive runs report their own counter deltas without resetting player totals', async () => {
  const env = await harness();
  const first = await env.lab.run();
  const second = await env.lab.run();
  for (const row of [first, second]) {
    assert.equal(row.metrics.renderedFrames, 1);
    assert.equal(row.metrics.scheduledSamples, 960);
    assert.equal(row.metrics.droppedStale, 2);
    assert.equal(row.sources.length, 1);
    assert.equal(row.rendered.length, 1);
    assert.equal(row.sources[0].duration, 0.04);
  }
  assert.equal(env.lab.player.metrics.renderedFrames, 2);
  assert.equal(env.lab.player.metrics.scheduledSamples, 1920);
  assert.equal(env.lab.player.metrics.droppedStale, 4);
});

test('render timestamps are captured after the renderer callback completes', async () => {
  const env = await harness();
  const row = await env.lab.run();
  assert.equal(row.rendered.length, 1);
  assert.equal(row.rendered[0].frame, 0);
  assert.equal(row.rendered[0].atMs, env.renderCompletions[0]);
  assert.ok(row.rendered[0].atMs > row.sources[0].atMs);
  assert.equal(row.rendered[0].audioTime, env.lab.player.context.currentTime);
});
