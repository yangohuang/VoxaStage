import { Timeline } from './timeline.mjs';

function bytes(value, maxLength) {
  if (typeof value !== 'string' || value.length > maxLength) throw new Error('媒体数据长度无效');
  const raw = atob(value);
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

function pcm16(value) {
  if (!value.length || value.length % 2) throw new Error('媒体编码无效');
  const view = new DataView(value.buffer, value.byteOffset, value.byteLength);
  const pcm = new Float32Array(value.length / 2);
  for (let i = 0; i < pcm.length; i++) pcm[i] = view.getInt16(i * 2, true) / 32768;
  return pcm;
}

export function decodeMedia(message) {
  const audio = bytes(message.audio, 64000);
  const mesh = bytes(message.vertices, 1100000);
  if (!audio.length || audio.length % 2 || mesh.length % 4) throw new Error('媒体编码无效');
  const vv = new DataView(mesh.buffer);
  const pcm = pcm16(audio), vertices = new Float32Array(mesh.length / 4);
  for (let i = 0; i < vertices.length; i++) vertices[i] = vv.getFloat32(i * 4, true);
  return { generation: message.generation, clip_id: message.clip_id, start_sample: message.start_sample, frame_index: message.frame_index, pts: message.pts, pcm, vertices };
}

export async function decode2DMedia(message, decodeImage = defaultDecodeImage) {
  const pcm = pcm16(bytes(message.audio, 64000));
  const imageBytes = bytes(message.image, 1_400_000);
  if (imageBytes.length < 4 || imageBytes[0] !== 0xff || imageBytes[1] !== 0xd8 || imageBytes[imageBytes.length - 2] !== 0xff || imageBytes[imageBytes.length - 1] !== 0xd9) throw new Error('JPEG 图像编码无效');
  let image;
  try { image = await decodeImage(imageBytes); }
  catch { throw new Error('JPEG 图像解码失败'); }
  if (!image || !Number.isInteger(image.width) || !Number.isInteger(image.height)) { image?.close?.(); throw new Error('JPEG 图像解码失败'); }
  return { generation: message.generation, clip_id: message.clip_id, start_sample: message.start_sample, frame_index: message.frame_index, pts: message.pts, pcm, image,
    dispose() { image.close?.(); } };
}

async function defaultDecodeImage(imageBytes) {
  if (typeof createImageBitmap !== 'function') throw new Error('浏览器不支持 JPEG 图像解码');
  return createImageBitmap(new Blob([imageBytes], { type: 'image/jpeg' }));
}

export class AvatarPlayer {
  constructor({ onFrame, onState, onError, decodeImage = defaultDecodeImage }) {
    this.timeline = new Timeline();
    this.sources = new Set(); this.metadata = new Map();
    this.onFrame = onFrame; this.onState = onState; this.onError = onError;
    this.renderedFrames = 0; this.scheduledSamples = 0; this.startedReported = false;
    this.decodeImage = decodeImage; this.decodeQueue = []; this.decodeBytes = 0; this.decodePixels = 0; this.epoch = 0;
    this.timer = setInterval(() => this.tick(), 15);
  }

  async unlock() {
    if (!this.context) this.context = new AudioContext({ sampleRate: 24000, latencyHint: 'interactive' });
    await this.context.resume();
    if (this.context.state !== 'running') throw new Error('请点击连接以启用声音');
  }

  stop() {
    for (const source of this.sources) { try { source.stop(); } catch {} source.disconnect(); }
    this.sources.clear(); this.metadata.clear(); this.epoch++; this.discardDecodes(); this.timeline.suspend();
    this.startedReported = false;
  }

  reset(generation) {
    if (!Number.isSafeInteger(generation) || generation <= this.timeline.generation) return false;
    this.stop();
    return this.timeline.reset(generation);
  }

  handle(message) {
    if (message.type === 'avatar_meta') {
      if (this.timeline.addMeta(message)) this.metadata.set(message.clip_id, message);
    } else if (message.type === 'media' && this.metadata.get(message.clip_id)?.kind === '2d') {
      this.decode2D(message);
    } else if (message.type === 'media') {
      // Reject stale messages before decoding large base64 fields.
      if (this.timeline.accepts(message)) this.timeline.push(decodeMedia(message));
    } else if (message.type === 'clip_end') {
      if (this.metadata.get(message.clip_id)?.kind === '2d' && this.timeline.accepts(message)) {
        if (this.decodeQueue.length >= 64) throw new Error('JPEG 解码缓冲已满');
        this.decodeQueue.push({ end: message, generation: message.generation, epoch: this.epoch, settled: true, bytes: 0, pixels: 0 });
        this.flushDecoded();
      } else this.timeline.end(message);
    }
    this.tick();
  }

  discardDecodes() {
    for (const entry of this.decodeQueue) { entry.discard = true; entry.packet?.dispose?.(); }
    this.decodeQueue = []; this.decodeBytes = 0; this.decodePixels = 0;
  }

  decode2D(message) {
    // Reject stale packets before decoding their base64 image, and keep decoded frames bounded.
    if (!this.timeline.accepts(message)) return;
    const meta = this.metadata.get(message.clip_id);
    const pixels = meta.width * meta.height * 4;
    const imageLength = typeof message.image === 'string' ? message.image.length : 0;
    if (this.decodeQueue.length >= 64 || this.decodeBytes + imageLength > 16 * 1024 * 1024 || this.decodePixels + pixels > 64 * 1024 * 1024) throw new Error('JPEG 解码缓冲已满，请打断后重试');
    const entry = { generation: message.generation, epoch: this.epoch, settled: false, discard: false, bytes: imageLength, pixels };
    this.decodeQueue.push(entry); this.decodeBytes += imageLength; this.decodePixels += pixels;
    decode2DMedia(message, this.decodeImage).then(packet => {
      if (entry.discard) { packet.dispose?.(); return; }
      entry.packet = packet; entry.settled = true; this.flushDecoded();
    }, error => { if (!entry.discard) { entry.error = error; entry.settled = true; this.flushDecoded(); } });
  }

  flushDecoded() {
    while (this.decodeQueue[0]?.settled) {
      const entry = this.decodeQueue.shift(); this.decodeBytes -= entry.bytes; this.decodePixels -= entry.pixels;
      if (entry.discard || entry.epoch !== this.epoch || entry.generation !== this.timeline.generation || this.timeline.suspended) { entry.packet?.dispose?.(); continue; }
      if (entry.error) { this.stop(); this.onError(entry.error); return; }
      try {
        if (entry.end) this.timeline.end(entry.end);
        else if (!this.timeline.push(entry.packet)) entry.packet?.dispose?.();
      } catch (error) { entry.packet?.dispose?.(); this.stop(); this.onError(error); return; }
    }
    this.tick();
  }

  tick() {
    if (!this.context || this.context.state !== 'running' || this.timeline.suspended) return;
    try {
      const { jobs, frame } = this.timeline.advance(this.context.currentTime);
      for (const { when, packet } of jobs) {
        const buffer = this.context.createBuffer(1, packet.pcm.length, 24000);
        buffer.copyToChannel(packet.pcm, 0);
        const source = this.context.createBufferSource(); source.buffer = buffer;
        source.connect(this.context.destination); this.sources.add(source);
        source.onended = () => { this.sources.delete(source); source.disconnect(); };
        source.start(when); this.scheduledSamples += packet.pcm.length;
      }
      if (frame) {
        this.onFrame(frame.image || frame.vertices, this.metadata.get(frame.clip_id)); this.renderedFrames++;
      }
      for (const id of this.metadata.keys()) if (this.timeline.retired.has(id)) this.metadata.delete(id);
      if (this.timeline.playing && !this.startedReported) {
        this.startedReported = true; this.onState('started');
      }
      if (this.startedReported && !this.timeline.clips.length && !this.sources.size) {
        this.startedReported = false; this.onState('ended');
      }
    } catch (error) { this.stop(); this.onError(error); }
  }

  get metrics() {
    return { generation: this.timeline.generation, renderedFrames: this.renderedFrames, droppedStale: this.timeline.droppedStale,
      playing: this.timeline.playing, pendingFrames: this.timeline.pendingFrames, queuedSeconds: this.timeline.queuedSamples / 24000,
      scheduledSources: this.sources.size, scheduledSamples: this.scheduledSamples, audioTime: this.context?.currentTime ?? 0 };
  }
}
