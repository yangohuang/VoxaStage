// Deterministic aperture mapping: prediction geometry, never audio loudness.
export class MouthRetarget {
  constructor(config) {
    const c = config;
    if (!c || c.version !== 1 || !Number.isSafeInteger(c.vertex_count) || c.vertex_count < 4 || c.vertex_count > 20000
        || !Array.isArray(c.pairs) || !c.pairs.length || c.pairs.length > 32
        || !c.pairs.every(p => Array.isArray(p) && p.length === 2 && p[0] !== p[1]
          && p.every(i => Number.isSafeInteger(i) && i >= 0 && i < c.vertex_count))
        || !Number.isFinite(c.closed) || c.closed < 0 || !Number.isFinite(c.open) || c.open <= c.closed
        || ![c.upper,c.lower].every(j => j && typeof j.node === 'string' && j.node.length > 0 && j.node.length <= 120
          && ['x','y','z'].includes(j.axis) && Number.isFinite(j.radians) && Math.abs(j.radians) <= Math.PI/2)
        || c.upper.node === c.lower.node) throw new Error('嘴型映射配置不兼容');
    this.config = structuredClone(c);
  }
  measure(vertices) {
    const c = this.config;
    if (!vertices || vertices.length !== c.vertex_count * 3) throw new Error('嘴型源网格拓扑不匹配');
    let distance = 0;
    for (const [upper, lower] of c.pairs) {
      const deltas = [0,1,2].map(k => vertices[upper*3+k] - vertices[lower*3+k]);
      if (!deltas.every(Number.isFinite)) throw new Error('嘴型源网格包含无效坐标');
      distance += Math.hypot(...deltas);
    }
    return Math.max(0, Math.min(1, (distance / c.pairs.length - c.closed) / (c.open - c.closed)));
  }
}
