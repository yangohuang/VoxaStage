# 多驱动数字人协议 v1

浏览器 → Pipecat 对话端点：`/avatar/ws?provider=streamingtalker|flashhead|dinet`。
不指定 provider 时使用配置的默认驱动。`GET /avatar/providers` 中 configured 只表示部署者配置了地址，不等于模型已健康。

## 模型服务边界

StreamingTalker 使用已有 `/v1/stream` 网格协议，输入为 16k PCM。下面定义的是 **FlashHead 使用的 2D 桥接协议**。DINet 原生协议由 `dinet_backend.py` 转换：open_stream / HEADER 103 / YUV420P IMAGE 101 / AUDIO 102 / CTL 100。模型输入为持续重采样的 16k PCM16；输出 JPEG 限制最长边 640，音轨保留原始 TTS PCM；模型尾部补零帧丢弃。关闭原生连接表示本地传输清理，不声称上游 API 提供 GPU 清理确认。

2D WebSocket `/v1/stream` 的单连接对应一段 TTS 回复：

1. 客户端发送 `{"type":"start","protocol":1,"sample_rate":24000}`。
2. 服务发送 `{"type":"metadata","protocol":1,"kind":"2d","codec":"jpeg","sample_rate":24000,"fps":25,"width":512,"height":512}`。模型应在接入会话前完成加载。
3. 客户端连续发送 binary PCM16LE，单声道 24kHz。适配层保持原音频作为最终播放音轨。服务可内部重采样、补零，但不能把补零计入播放时长。
4. 每帧发送 `{"type":"frame","index":0,"pts_seconds":0,"image":"<base64 JPEG>"}`。index 从 0 连续递增，pts_seconds=index/fps。帧须对应已接收的音频，不能提前输出无音频帧；末尾可包含一个不满整帧的真实音频区间，不能输出补零帧。
5. 客户端发送 `{"type":"end"}` 后服务处理剩余音频，完成清理后发送 `{"type":"done","cleanup_complete":true}`。
6. `{"type":"cancel"}` 或连接关闭立即请求取消。服务可以在当前 GPU 运算返回后清理，但在运算结束前必须继续占有推理锁，不能允许新会话并发修改 pipeline。取消后的图像不得回送旧会话。浏览器通过 generation 隔离，立即静音，不等待 GPU 运算结束。
7. 忙碌或故障使用 `{"type":"error","code":"busy|backend_error|invalid_input"}`，随后关闭连接。部署日志记录具体原因，前端错误不暴露路径。

约束：每段最多 30 秒音频；服务帧尺寸 <=2048×2048，fps 1–60；单 JPEG base64 <=1,400,000 字符；帧等待超时 20 秒。生产环境需要另外部署认证与 TLS，当前保持本地服务边界。

## Pipecat → 浏览器

`ready` 包含 session_id、generation、provider、kind、input_sample_rate=16000。麦克风输入保持 16k，与模型服务的 TTS 输入采样率独立。

`avatar_meta` 带 generation、clip_id，并包含网格描述或 2D 的 kind/codec/width/height/fps/sample_rate。旧 3D 未带 kind 时仍按网格处理。

`media` 带 generation、clip_id、frame_index、start_sample、pts=start_sample/24000、audio(base64 PCM16LE)。3D 附 vertices，2D 附 image。浏览器先解码图像，再以同一 AudioContext 时钟安排音画；clip_end 要等待先前图像解码完成。

`reset` 增加 generation。停止旧声音、清除队列和未完成解码，保留已经显示的静帧及对话文字。旧 generation、旧连接、已取消的异步解码都不得恢复播放。

模型输出过快时，2D 适配器将发送进度限制在播放时钟前约 1 秒。浏览器解码队列最多 64 项、16MiB 编码数据和 64MiB 预计解码像素；播放队列另有 64MiB 图像上限。超限会停止并提示，不无限增长内存。
