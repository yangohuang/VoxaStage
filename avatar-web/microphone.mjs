export class Microphone {
  constructor(onPacket, {captureTiming = false} = {}) {
    this.onPacket = onPacket; this.ticket = 0; this.active = false;
    this.captureTiming = captureTiming === true;
  }
  async start() {
    const ticket = ++this.ticket;
    if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) throw new Error('麦克风需要 localhost 或 HTTPS；仍可用文字对话');
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    if (ticket !== this.ticket) { stream.getTracks().forEach(t => t.stop()); return; }
    this.stream = stream;
    try {
      try { this.context = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' }); }
      catch { this.context = new AudioContext({ latencyHint: 'interactive' }); }
      const context = this.context;
      await context.audioWorklet.addModule('/avatar/static/mic-worklet.mjs');
      if (ticket !== this.ticket) return;
      const node = new AudioWorkletNode(context, 'avatar-microphone', {
        processorOptions: {captureTiming: this.captureTiming},
      });
      this.source = context.createMediaStreamSource(stream); this.node = node;
      node.port.onmessage = ({ data }) => {
        if (!this.active || ticket !== this.ticket) return;
        if (!this.captureTiming || data instanceof ArrayBuffer) { this.onPacket(data); return; }
        const before = performance.now(), contextTime = context.currentTime, after = performance.now();
        this.onPacket(data.pcm, {...data.timing, contextTimeAtReceive: contextTime,
          anchorPerformanceBeforeMs: before, anchorPerformanceAfterMs: after});
      };
      // A muted output keeps the worklet live without microphone feedback.
      this.gain = context.createGain(); this.gain.gain.value = 0;
      this.source.connect(node); node.connect(this.gain); this.gain.connect(context.destination);
      await context.resume();
      if (ticket !== this.ticket) return;
      this.active = true;
    } catch (error) { await this.stop(); throw error; }
  }
  async stop() {
    this.ticket++; this.active = false;
    this.stream?.getTracks().forEach(track => track.stop()); this.stream = null;
    if (this.node) { this.node.port.onmessage = null; this.node.disconnect(); this.node = null; }
    this.source?.disconnect(); this.source = null; this.gain?.disconnect(); this.gain = null;
    const context = this.context; this.context = null;
    if (context && context.state !== 'closed') await context.close();
  }
}
