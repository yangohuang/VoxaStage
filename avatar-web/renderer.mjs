export class HeadRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl', { alpha: true, antialias: true });
    if (!gl) throw new Error('浏览器未启用 WebGL，无法显示 3D 头部');
    this.gl = gl;
    const shader = (type, source) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, source); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const program = gl.createProgram();
    gl.attachShader(program, shader(gl.VERTEX_SHADER, `
      attribute vec3 position; attribute vec3 normal;
      uniform vec3 center; uniform float scale; uniform float aspect;
      varying vec3 n; varying vec3 p;
      void main() {
        p=(position-center)*scale; n=normal;
        gl_Position=vec4(p.x/max(aspect,1.0), p.y*min(aspect,1.0), -p.z/5.0, 1.0);
      }
    `));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, `
      precision mediump float; varying vec3 n; varying vec3 p;
      void main() {
        vec3 N=normalize(n); if(!gl_FrontFacing) N=-N;
        float key=max(dot(N,normalize(vec3(-0.5,0.8,1.5))),0.0);
        float fill=max(dot(N,normalize(vec3(1.0,0.2,0.6))),0.0);
        float rim=pow(1.0-abs(N.z),3.0)*0.15;
        vec3 color=vec3(0.25,0.61,0.70)*(0.32+key*0.7+fill*0.2)+vec3(rim);
        gl_FragColor=vec4(color,1.0);
      }
    `));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    this.program = program;
    this.position = gl.createBuffer(); this.normal = gl.createBuffer(); this.indices = gl.createBuffer();
    this.resize = new ResizeObserver(() => this.draw()); this.resize.observe(canvas);
    this.clear();
  }

  setMeta(meta) {
    if (this.clipId === meta.clip_id) return;
    this.clipId = meta.clip_id;
    this.faces = new Uint16Array(meta.faces.flat());
    this.vertices = null;
    this.center = null;
    const gl = this.gl;
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indices);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, this.faces, gl.STATIC_DRAW);
  }

  render(vertices) {
    if (!this.faces) return;
    const gl = this.gl;
    if (!this.center) {
      const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
      for (let i = 0; i < vertices.length; i++) { const k = i % 3; lo[k] = Math.min(lo[k], vertices[i]); hi[k] = Math.max(hi[k], vertices[i]); }
      this.center = lo.map((v, i) => (v + hi[i]) / 2);
      this.scale = 1.65 / Math.max(0.0001, ...hi.map((v, i) => v - lo[i]));
    }
    const normals = new Float32Array(vertices.length);
    for (let i = 0; i < this.faces.length; i += 3) {
      const a = this.faces[i] * 3, b = this.faces[i + 1] * 3, c = this.faces[i + 2] * 3;
      const ux = vertices[b] - vertices[a], uy = vertices[b + 1] - vertices[a + 1], uz = vertices[b + 2] - vertices[a + 2];
      const vx = vertices[c] - vertices[a], vy = vertices[c + 1] - vertices[a + 1], vz = vertices[c + 2] - vertices[a + 2];
      const x = uy * vz - uz * vy, y = uz * vx - ux * vz, z = ux * vy - uy * vx;
      for (const k of [a, b, c]) { normals[k] += x; normals[k + 1] += y; normals[k + 2] += z; }
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.position); gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.normal); gl.bufferData(gl.ARRAY_BUFFER, normals, gl.DYNAMIC_DRAW);
    this.vertices = vertices;
    this.draw();
  }

  clear() {
    this.vertices = null; this.faces = null; this.center = null; this.clipId = null;
    this.draw();
  }

  draw() {
    const gl = this.gl;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(this.canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(this.canvas.clientHeight * ratio));
    if (this.canvas.width !== width || this.canvas.height !== height) { this.canvas.width = width; this.canvas.height = height; }
    gl.viewport(0, 0, width, height); gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!this.vertices) return;
    gl.enable(gl.DEPTH_TEST); gl.disable(gl.CULL_FACE); gl.useProgram(this.program);
    for (const [name, buffer] of [['position', this.position], ['normal', this.normal]]) {
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      const location = gl.getAttribLocation(this.program, name);
      gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 0, 0);
    }
    gl.uniform3fv(gl.getUniformLocation(this.program, 'center'), this.center);
    gl.uniform1f(gl.getUniformLocation(this.program, 'scale'), this.scale);
    gl.uniform1f(gl.getUniformLocation(this.program, 'aspect'), width / height);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.indices);
    gl.drawElements(gl.TRIANGLES, this.faces.length, gl.UNSIGNED_SHORT, 0);
  }
}

export class PortraitRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext('2d', { alpha: true });
    if (!this.context) throw new Error('浏览器不支持 2D 画布');
    this.resize = new ResizeObserver(() => this.draw()); this.resize.observe(canvas);
  }

  render(image) {
    this.image?.close?.();
    this.image = image;
    this.draw();
  }

  clear() { this.image?.close?.(); this.image = null; this.draw(); }

  draw() {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(this.canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(this.canvas.clientHeight * ratio));
    if (this.canvas.width !== width || this.canvas.height !== height) { this.canvas.width = width; this.canvas.height = height; }
    this.context.clearRect(0, 0, width, height);
    if (!this.image) return;
    const scale = Math.min(width / this.image.width, height / this.image.height);
    const drawWidth = this.image.width * scale, drawHeight = this.image.height * scale;
    this.context.drawImage(this.image, (width - drawWidth) / 2, (height - drawHeight) / 2, drawWidth, drawHeight);
  }
}
