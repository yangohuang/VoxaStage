// Pure scheduling state. Every timestamp passed to advance is AudioContext.currentTime.
export class Timeline {
  constructor({ prebuffer = 0.12, horizon = 0.35, maxSeconds = 30, maxFrames = 900 } = {}) {
    Object.assign(this, { prebuffer, horizon, maxSeconds, maxFrames });
    this.generation = -1;
    this.droppedStale = 0;
    this.clear();
  }

  clear() {
    for (const clip of this.clips || []) for (const packet of clip.queue) packet.dispose?.();
    for (const frame of this.frames || []) frame.packet.dispose?.();
    this.clips = [];
    this.retired = new Set();
    this.frames = [];
    this.queuedSamples = 0;
    this.pendingFrames = 0;
    this.visualBytes = 0;
    this.tail = 0;
    this.activeEnd = 0;
    this.playing = false;
    this.suspended = true;
  }

  reset(generation) {
    if (!Number.isSafeInteger(generation) || generation <= this.generation) return false;
    this.clear();
    this.generation = generation;
    this.suspended = false;
    return true;
  }

  suspend() { this.clear(); }

  accepts(message) {
    if (this.suspended || message.generation !== this.generation) {
      this.droppedStale++;
      return false;
    }
    return true;
  }

  addMeta(meta) {
    if (!this.accepts(meta)) return false;
    if (this.retired.has(meta.clip_id) || this.clips.some(c => c.meta.clip_id === meta.clip_id)) {
      this.droppedStale++;
      return false;
    }
    const is2d = meta.kind === '2d';
    const valid2d = is2d && meta.sample_rate === 24000 && Number.isFinite(meta.fps) && meta.fps >= 1 && meta.fps <= 60 &&
      Number.isInteger(meta.width) && meta.width > 0 && meta.width <= 2048 && Number.isInteger(meta.height) && meta.height > 0 && meta.height <= 2048 && meta.codec === 'jpeg';
    const valid3d = (meta.kind === undefined || meta.kind === '3d') && meta.sample_rate === 24000 && meta.fps === 30 &&
      Number.isInteger(meta.vertex_count) && meta.vertex_count >= 3 && meta.vertex_count <= 65535 &&
      Array.isArray(meta.faces) && meta.faces.length && meta.faces.length <= 150000 &&
      meta.faces.every(f => Array.isArray(f) && f.length === 3 && f.every(v => Number.isInteger(v) && v >= 0 && v < meta.vertex_count));
    if (typeof meta.clip_id !== 'string' || !meta.clip_id || meta.clip_id.length > 256 || (!valid2d && !valid3d)) throw new Error(is2d ? '无效的 2D 图像描述' : '无效的 3D 网格描述');
    if (this.clips.length >= 64 || this.retired.size >= 4096) throw new Error('会话缓冲已满，请重新连接');
    this.clips.push({ meta, queue: [], received: 0, nextFrame: 0, ended: false, started: false, lastEnd: 0 });
    return true;
  }

  push(packet) {
    if (!this.accepts(packet)) return false;
    const clip = this.clips.find(c => c.meta.clip_id === packet.clip_id);
    if (!clip || clip.ended) { this.droppedStale++; return false; }
    if (packet.start_sample !== clip.received || packet.frame_index !== clip.nextFrame ||
        !Number.isFinite(packet.pts) || Math.abs(packet.pts - packet.start_sample / 24000) > 0.002) {
      throw new Error('音频帧必须连续，已停止以避免声画错位');
    }
    const validAudio = packet.pcm instanceof Float32Array && packet.pcm.length && packet.pcm.length <= 24000;
    const valid2d = clip.meta.kind === '2d' && packet.image && packet.image.width === clip.meta.width && packet.image.height === clip.meta.height;
    const valid3d = (clip.meta.kind === undefined || clip.meta.kind === '3d') && packet.vertices instanceof Float32Array && packet.vertices.length === clip.meta.vertex_count * 3 && packet.vertices.every(Number.isFinite);
    if (!validAudio || (!valid2d && !valid3d)) throw new Error(clip.meta.kind === '2d' ? '无效的 JPEG 图像帧' : '无效的音频或网格帧');
    packet.visualBytes = valid2d ? clip.meta.width * clip.meta.height * 4 : packet.vertices.byteLength;
    if (this.pendingFrames >= this.maxFrames || this.visualBytes + packet.visualBytes > 64 * 1024 * 1024 || (this.queuedSamples + packet.pcm.length) / 24000 > this.maxSeconds) {
      throw new Error('播放缓冲已满，请打断后重试');
    }
    clip.queue.push(packet);
    clip.received += packet.pcm.length;
    clip.nextFrame++;
    this.queuedSamples += packet.pcm.length;
    this.pendingFrames++;
    this.visualBytes += packet.visualBytes;
    return true;
  }

  end(message) {
    if (!this.accepts(message)) return false;
    const clip = this.clips.find(c => c.meta.clip_id === message.clip_id);
    if (!clip || clip.ended) { this.droppedStale++; return false; }
    if (message.total_samples !== clip.received) throw new Error('音频总长度不匹配');
    clip.ended = true;
    return true;
  }

  advance(now) {
    const jobs = [];
    let frame = null;
    if (this.suspended) return { jobs, frame };
    while (this.frames.length && this.frames[0].when <= now) {
      const due = this.frames.shift();
      this.pendingFrames--;
      this.visualBytes -= due.packet.visualBytes;
      this.activeEnd = due.end;
      // Skip expired animation when the tab resumes after throttling.
      if (due.end > now) {
        frame?.dispose?.();
        frame = due.packet;
      } else due.packet.dispose?.();
    }
    while (this.clips.length && this.clips[0].ended && !this.clips[0].queue.length && this.clips[0].lastEnd <= now) {
      this.retired.add(this.clips.shift().meta.clip_id);
    }
    for (const clip of this.clips) {
      if (!clip.started && !clip.ended && clip.queue.reduce((n, p) => n + p.pcm.length, 0) / 24000 < this.prebuffer) break;
      while (clip.queue.length) {
        const when = Math.max(now + 0.025, this.tail);
        if (when > now + this.horizon) break;
        const packet = clip.queue.shift();
        const end = when + packet.pcm.length / 24000;
        jobs.push({ when, packet, meta: clip.meta });
        this.frames.push({ when, end, packet });
        this.queuedSamples -= packet.pcm.length;
        this.tail = end;
        clip.lastEnd = end;
        clip.started = true;
      }
      if (clip.queue.length || !clip.ended) break;
    }
    this.playing = this.activeEnd > now;
    return { jobs, frame };
  }
}
