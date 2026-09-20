import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('../microphone.mjs', import.meta.url), 'utf8')
  .replace('export class Microphone', 'globalThis.Microphone = class Microphone');
function setup(options) {
  const packets = [], nodes = [];
  let now = 100, stopped = 0;
  const stream = {getTracks: () => [{stop: () => stopped++}]};
  const scope = {
    ArrayBuffer, performance: {now: () => now++}, window: {isSecureContext: true},
    navigator: {mediaDevices: {getUserMedia: async () => stream}},
    AudioContext: class {
      constructor() { this.currentTime = 1.25; this.state = 'running'; this.destination = {}; this.audioWorklet = {addModule: async () => {}}; }
      createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
      createGain() { return {gain: {}, connect() {}, disconnect() {}}; }
      async resume() {}
      async close() { this.state = 'closed'; }
    },
    AudioWorkletNode: class {
      constructor(context, name, opts) { this.context = context; this.name = name; this.options = opts; this.port = {}; nodes.push(this); }
      connect() {}
      disconnect() {}
    },
  };
  vm.runInNewContext(code, scope);
  const mic = new scope.Microphone((...args) => packets.push(args), options);
  return {mic, packets, nodes, scope, stopped: () => stopped};
}

test('timed microphone forwards original PCM and a main-thread clock anchor separately', async () => {
  const {mic, packets, nodes} = setup({captureTiming: true});
  await mic.start();
  assert.equal(nodes[0].options?.processorOptions.captureTiming, true);
  const pcm = new ArrayBuffer(640);
  nodes[0].port.onmessage({data: {pcm, timing: {valid: true, firstInputFrame: 1234}}});
  assert.equal(packets[0][0], pcm);
  assert.equal(packets[0][1].firstInputFrame, 1234);
  assert.equal(packets[0][1].contextTimeAtReceive, 1.25);
  assert.equal(packets[0][1].anchorPerformanceBeforeMs, 100);
  assert.equal(packets[0][1].anchorPerformanceAfterMs, 101);
  await mic.stop();
});

test('default microphone preserves the binary-only callback contract', async () => {
  const {mic, packets, nodes} = setup();
  await mic.start();
  const pcm = new ArrayBuffer(640);
  nodes[0].port.onmessage({data: pcm});
  assert.equal(packets[0].length, 1);
  assert.equal(packets[0][0], pcm);
  assert.notEqual(nodes[0].options?.processorOptions.captureTiming, true);
  await mic.stop();
});

test('stopping a timed microphone ignores late packets and closes its capture resources', async () => {
  const {mic, packets, nodes, stopped} = setup({captureTiming: true});
  await mic.start();
  const oldHandler = nodes[0].port.onmessage, context = mic.context;
  await mic.stop();
  oldHandler({data: {pcm: new ArrayBuffer(640), timing: {valid: true}}});
  assert.equal(packets.length, 0);
  assert.equal(mic.active, false);
  assert.equal(context.state, 'closed');
  assert.equal(nodes[0].port.onmessage, null);
  assert.equal(stopped(), 1);
});

test('a cached legacy worklet does not fabricate timing metadata', async () => {
  const {mic, packets, nodes} = setup({captureTiming: true});
  await mic.start();
  const pcm = new ArrayBuffer(640);
  nodes[0].port.onmessage({data: pcm});
  assert.equal(packets[0][0], pcm);
  assert.equal(packets[0][1], undefined);
  await mic.stop();
});
