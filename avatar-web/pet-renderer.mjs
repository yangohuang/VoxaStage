import * as THREE from './vendor/three/build/three.module.js';
import { GLTFLoader } from './vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { MouthRetarget } from './mouth-retarget.mjs';

function release(root) {
  const textures = new Set();
  root?.traverse(object => {
    object.geometry?.dispose();
    for (const material of Array.isArray(object.material) ? object.material : [object.material]) {
      if (!material) continue;
      for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
      material.dispose();
    }
  });
  for (const texture of textures) { texture.source?.data?.close?.(); texture.dispose(); }
}

async function asset(url, extension, limit) {
  if (!new RegExp(`^/avatar/mesh/[a-z0-9][a-z0-9_-]{0,63}\\.${extension}$`).test(url)) throw new Error('角色资源路径无效');
  const response = await fetch(url, {signal:AbortSignal.timeout(25000)});
  if (!response.ok || Number(response.headers.get('content-length')) > limit) throw new Error('角色资源不可用');
  const buffer = await response.arrayBuffer();
  if (buffer.byteLength > limit) throw new Error('角色资源过大');
  return buffer;
}

export class PetRenderer {
  static async create(canvas, profile) {
    const [buffer, calibration] = await Promise.all([
      asset(profile.mesh_url,'glb',32*1024*1024), asset(profile.retarget_url,'json',65536),
    ]);
    const mapping = new MouthRetarget(JSON.parse(new TextDecoder().decode(calibration)));
    const manager = new THREE.LoadingManager();
    // Assets are self-contained GLBs; reject external images/buffers.
    manager.setURLModifier(url => { if (!url.startsWith('blob:')) throw new Error('角色资源必须自包含'); return url; });
    const gltf = await new GLTFLoader(manager).parseAsync(buffer, '');
    try { return new PetRenderer(canvas, gltf.scene, mapping); }
    catch (error) { release(gltf.scene); throw error; }
  }

  constructor(canvas, root, mapping) {
    this.canvas = canvas; this.root = root; this.mapping = mapping; this.opening = 0;
    this.jaws = ['upper','lower'].map(key => {
      const rig = mapping.config[key]; let node;
      root.traverse(object => { if (object.userData.name === rig.node || object.name === rig.node) node = object; });
      if (!node) throw new Error('角色缺少嘴型控制节点');
      return {node, axis:new THREE.Vector3(...['x','y','z'].map(axis => Number(axis === rig.axis))),
        rest:node.quaternion.clone(), radians:rig.radians};
    });
    this.scene = new THREE.Scene(); this.scene.add(root);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x787e87, 2.5));
    const key = new THREE.DirectionalLight(0xfff4e4, 3); key.position.set(-2,3,4); this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xdbeaff, 1); fill.position.set(3,1,2); this.scene.add(fill);
    const bounds = new THREE.Box3().setFromObject(root), size = bounds.getSize(new THREE.Vector3());
    this.center = bounds.getCenter(new THREE.Vector3()); this.span = Math.max(size.y, size.x)*1.18;
    if (!Number.isFinite(this.span) || this.span <= 0) throw new Error('角色网格边界无效');
    this.camera = new THREE.OrthographicCamera(-1,1,1,-1,.01,100);
    this.camera.position.copy(this.center).add(new THREE.Vector3(.35,.08,4)); this.camera.lookAt(this.center);
    this.clear();
  }
  mount() {
    this.webgl = new THREE.WebGLRenderer({canvas:this.canvas,alpha:true,antialias:true});
    this.webgl.setPixelRatio(Math.min(window.devicePixelRatio || 1,2));
    this.webgl.outputColorSpace = THREE.SRGBColorSpace;
    this.webgl.toneMapping = THREE.ACESFilmicToneMapping; this.webgl.toneMappingExposure = 1;
    this.resize = new ResizeObserver(() => this.draw()); this.resize.observe(this.canvas);
    this.draw();
  }
  render(vertices, meta) {
    if (meta?.vertex_count !== this.mapping.config.vertex_count) throw new Error('当前预测模型与角色嘴型映射不匹配');
    this.pose(this.mapping.measure(vertices));
  }
  pose(opening) {
    this.opening = opening;
    for (const jaw of this.jaws) jaw.node.quaternion.copy(jaw.rest)
      .multiply(new THREE.Quaternion().setFromAxisAngle(jaw.axis, jaw.radians*opening));
    this.draw();
  }
  clear() { this.pose(0); }
  draw() {
    if (this.disposed || !this.webgl) return;
    const w = Math.max(1,this.canvas.clientWidth), h = Math.max(1,this.canvas.clientHeight), aspect = w/h;
    this.webgl.setSize(w,h,false);
    const half = this.span/2;
    this.camera.left = -half*Math.max(1,aspect); this.camera.right = -this.camera.left;
    this.camera.top = half/Math.min(1,aspect); this.camera.bottom = -this.camera.top;
    this.camera.updateProjectionMatrix(); this.webgl.render(this.scene,this.camera);
  }
  dispose() {
    this.disposed = true; this.resize?.disconnect(); release(this.root);
    this.webgl?.clear(); this.webgl?.dispose();
  }
}
