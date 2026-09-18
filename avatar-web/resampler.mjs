// Streaming linear resampling preserves fractional position across worklet blocks.
export class PCMResampler {
  constructor(inputRate) {
    if (!Number.isFinite(inputRate) || inputRate < 8000 || inputRate > 192000) throw new Error('麦克风采样率不受支持');
    this.ratio = inputRate / 16000;
    this.pending = new Float32Array(0); this.position = 0;
    this.packet = new ArrayBuffer(640); this.view = new DataView(this.packet); this.count = 0;
  }
  push(input) {
    const combined = new Float32Array(this.pending.length + input.length);
    combined.set(this.pending); combined.set(input, this.pending.length);
    const packets = [];
    while (this.position + 1 < combined.length) {
      const i = Math.floor(this.position), fraction = this.position - i;
      const sample = Math.max(-1, Math.min(1, combined[i] * (1 - fraction) + combined[i + 1] * fraction));
      this.view.setInt16(this.count * 2, Math.round(sample < 0 ? sample * 32768 : sample * 32767), true);
      this.count++; this.position += this.ratio;
      if (this.count === 320) {
        packets.push(this.packet); this.packet = new ArrayBuffer(640); this.view = new DataView(this.packet); this.count = 0;
      }
    }
    const consumed = Math.min(Math.floor(this.position), combined.length - 1);
    this.pending = combined.slice(consumed); this.position -= consumed;
    return packets;
  }
}
