# 浏览器数字人界面

原生 HTML/CSS/ES modules；没有 CDN、npm 运行依赖、外部字体或人物资产。
后端将 `index.html` 挂到 `/avatar`，将此目录挂到 `/avatar/static/`。
WebSocket 使用同源 `/avatar/ws`，事件遵循上级 `AVATAR-PLAN.md`。

页面默认离线。点击「连接会话」启用播放，再选择文字输入或显式开启麦克风。
麦克风需要 localhost 或 HTTPS；拒绝权限时仍可用文字。
文本消息以服务端 `transcript` 回显，避免语音识别/文本路径重复显示。

## 播放与渲染

- `timeline.mjs`：纯播放队列，120ms 预缓冲，350ms 调度提前量，30秒/900帧
  上限；同代多 clip 按 metadata 到达顺序串行，完成 clip 不接受迟到包。
- `audio.mjs`：24kHz PCM16LE 解码为 WebAudio buffer；所有播放和选帧均读取
  同一个 `AudioContext.currentTime`。当前音频设备采样率由 WebAudio 转换。
- `renderer.mjs`：真实 float32 顶点和三角形索引、逐帧顶点法线、原生 WebGL
  双面明暗材质。按每个 clip 首帧的包围盒居中并以 `1.65 / extent` 缩放，
  正面沿 Z 轴观察，与研究脚本 `render_preview.py` 方向一致。
- `microphone.mjs` / `mic-worklet.mjs` / `resampler.mjs`：AEC/NS、独立采集
  AudioContext；以实际采样率连续重采样到16k，每包320采样点/640字节。
  降采样使用线性插值，是演示实现，不是高质量离线音频转换器。

手动打断立即停止并断开全部音频源、清网格与队列，发送 `interrupt` 后等待服务端
新 generation，客户端不会自行推测代号。reset、断连、媒体错误同样清空播放。
浏览器后台节流时舍弃已过期网格帧，不追放旧嘴型；音频欠载后将新音频和网格一起
移动到当前音频时钟，可能产生静音间隔。建议保持标签页前台。

## 验证

Node 20+，不需要安装包，在仓库根目录运行：

```sh
node --test pipecat-local/avatar-web/tests/*.test.mjs
```

分别运行测试文件可看到完整子测试列表：

```sh
node pipecat-local/avatar-web/tests/timeline.test.mjs
node pipecat-local/avatar-web/tests/audio.test.mjs
node pipecat-local/avatar-web/tests/resampler.test.mjs
```

浏览器测试入口 `window.avatarDemo` 暴露 `connect()`、`disconnect()`、
`sendText(text)`、`interrupt()`，只读 `metrics`、`generation`、`renderedFrames`、
`droppedStale`、`playing`。metrics 包括队列时长、计划音频源数量、累计调度采样点、
音频时钟、连接和麦克风状态，不包含大体积 base64 或媒体内容。

真实服务验收：连接→发送文字→确认 renderedFrames 与 scheduledSamples 增长、
声音与网格同时播放→打断时 playing=false、pendingFrames=0、scheduledSources=0
→发送下一句恢复→断开后麦克风与播放停止。此页面展示的是无真人纹理、无身体的
3D 研究头部，不代表特定真人。
