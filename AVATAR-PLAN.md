# Pipecat 本地数字人 Demo v0.1

用户已确认第一版目标并授权执行。目标：现有语音链 + 一个现有数字人后端 +
实时浏览器展示 + 统一音画播放与打断 + 可复现代码和依赖说明。

## 选择

复用已经驻留的 StreamingTalker 增量服务，输出 5023 顶点的 3D 头部。
这是没有真人纹理/身体的研究头部，不将其包装为写实真人。DINet 现有文字驱动
接口和额外显存需求留作下一后端。新功能在现有 FastAPI `/avatar` 入口，纯语音
WebRTC `/` 保持可用。数字人入口采用本地 WebSocket PCM 输入和带时间戳的
PCM/网格输出，避免浏览器音频轨与独立动画时钟漂移；不是公网 RTC 替代品。

同一份 TTS PCM 驱动浏览器声音和数字人。后台将 PCM24k 流式重采样至16k送模型，
按模型帧时间打包对应原始24k音频。浏览器 AudioContext 是唯一播放时钟，WebGL
按该时钟选网格帧。每个 generation 中可包含多个 TTS clip；新打断递增 generation，
立即停所有已排音频、清动画队列、拒收旧 generation。GPU取消是合作式，必须等待
旧后端释放后才开始下一 clip，并有等待上限。WebSocket 消息与音频/网格队列有界。

## 浏览器协议 v1

同源 WebSocket `/avatar/ws`，server `ready` 后可发送16kHz单声道PCM16LE二进制
（每包最多32000字节），或 JSON：

- `{"type":"text","text":"介绍一下语音 agent"}`（最多500字符）
- `{"type":"interrupt"}`
- `{"type":"playback","generation":1,"state":"started"}` / `ended`

server JSON（所有媒体须检查 generation）：

- `ready`: `session_id`, `input_sample_rate:16000`, `generation:0`
- `reset`: `generation`（新值）, `reason`
- `avatar_meta`: `generation`, `clip_id`（字符串）, `sample_rate:24000`,
  `fps:30`, `vertex_count:5023`, `faces`（三角形索引二维数组）
- `media`: `generation`, `clip_id`, `start_sample`（clip内24k采样点位置）,
  `audio`（base64 PCM16LE）, `vertices`（base64 float32LE xyz）,
  `frame_index`, `pts`（clip内秒）
- `clip_end`: `generation`, `clip_id`, `total_samples`
- `transcript`: `text`; `assistant_text`: `text`（增量）
- `error`: `message`; `status`: `state`

浏览器先缓冲约120ms，限制计划播放时间与排队媒体量。对下一clip保证串行，
AudioContext音频源与网格均使用相同起点；断连/打断/后端失败停止二者。
播放 started/ended 回执使 Pipecat 的说话状态反映客户端播放，而非模型生成完成。

## 文件与验证

- [x] `avatar_backend.py`：StreamingTalker 协议适配，增量重采样、模型事件校验、
  对齐24k PCM、取消关闭、超时；使用假后端和真实后端验证。
- [x] `avatar_session.py` / `avatar_demo.py`：Pipecat处理器和 FastAPI 路由，
  VAD/文本/打断/播放回执，与纯语音共用单用户准入。
- [x] `avatar-web/`：无CDN、无外部角色资产的浏览器 WebGL、WebAudio 播放器，
  麦克风、文字输入、停止、对话显示和简要状态。
- [x] Python与Node回归：乱序/旧generation丢弃、取消恢复、畸形媒体、有界队列。
- [x] 真实模型完整链路 + 浏览器截图/播放时钟/打断验证，记录资源和指标口径。
- [x] `AVATAR-README.md`：后端协议、安装启动、环境变量、限制、依赖和素材来源。
  提供发布用源码清单，排除模型权重、本机配置、日志与研究人物素材。

暂不包含工具/后台Reasoner、数字人模型训练、多用户、公网部署或长会话SLA。
