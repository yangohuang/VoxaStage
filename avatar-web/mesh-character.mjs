// Selection epochs prevent late loads from changing a newly selected character.
export class MeshCharacter {
  constructor({load, changed = () => {}}) {
    this.load = load; this.changed = changed; this.epoch = 0;
    this.key = ''; this.renderer = null; this.pending = false; this.error = '';
  }
  get active() { return !!this.key; }
  get ready() { return !!this.renderer && !this.pending; }
  async select(profile) {
    const key = profile?.mesh_url ? JSON.stringify([profile.mesh_url, profile.retarget_url]) : '';
    if (key === this.key && (this.ready || this.pending || !key)) return;
    const epoch = ++this.epoch;
    this.renderer?.dispose(); this.renderer = null;
    this.key = key; this.pending = !!key; this.error = ''; this.changed();
    if (!key) return;
    let renderer;
    try {
      renderer = await this.load(profile);
      if (epoch !== this.epoch) { renderer.dispose(); return; }
      renderer.mount?.();
      this.renderer = renderer; this.pending = false; this.changed();
    } catch {
      renderer?.dispose();
      if (epoch !== this.epoch) return;
      this.pending = false; this.error = '角色加载失败，请重新选择角色后重试。'; this.changed();
    }
  }
  render(vertices, meta) { if (this.ready) this.renderer.render(vertices, meta); }
  clear() { this.renderer?.clear(); }
}
