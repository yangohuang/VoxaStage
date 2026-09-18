# 数字人 v0.1 验收记录

日期：2026-09-16。Pipecat 1.10.0；同机RTX 4090 24564MiB；Qwen3-ASR-0.6B、
Qwen3-4B、Index-TTS和StreamingTalker增量后端。Chrome的WebGL使用软件渲染测试。

| 实验 | 结果与口径 |
| --- | --- |
| 增量后端独立输入 | 3.75秒合成语音，原始24k音频90113采样全部保留；113个配对媒体事件；首媒体0.696秒，在输入结束前到达，尾部不足一帧的音频也保留。 |
| 文字完整回答 | “介绍一下语音agent技术”，两句回答在Chrome显示并播放，真实5023顶点头部被渲染。 |
| 文字打断恢复 | 首媒体1.020秒；打断后请求“请只说你好”，新代首媒体在初始请求后1.742秒到达；generation从1到3，reset后无旧代媒体。 |
| 语音自动打断 | 两次发送3.75秒合成WAV，均识别为“请用一句话解释什么是实时语音交互。”；再次开口触发VAD打断并恢复。两代首媒体在测试开始后5.536/11.087秒到达；包含输入语音，不能当作语音结束到首声。 |
| 浏览器立即停止 | 正在播放时打断，已安排声音源从5降至0，playing变false；400ms后渲染帧数仍为353，随后新代恢复。是AudioContext/WebGL指标，不是物理扬声器测量。 |
| 浏览器采集链 | 合成WAV构造MediaStream，经真实AudioWorklet、WebSocket、VAD、ASR及数字人链路；回答时再次开口，generation从4到5并恢复播放。未调用物理麦克风。 |
| 纯语音回归 | `smoke_webrtc.py --interrupt`通过，两次转写和两轮非静音音频正常；服务端打断事件到停止事件约1.4ms；语音结束至首非静音接收约2.143秒。 |
| 页面 | 实际头部截图经目视检查；390px宽度下无横向溢出，控制按钮保留。 |

这些是单机单次功能验收，不代表性能分位数、声学嘴型误差、主观音质或并发能力。

Python回归覆盖PCM尾帧保留、异常网格、发送失败唤醒接收、旧代隔离、慢取消与连续reset、
音频积压边界、上游错误通知和双句TTS只在整轮最后结束。前端三个Node测试文件覆盖
排程/解码/打断/多clip/重采样。数字人overlay的46项CPU测试及固定上游版本核对见其README。
独立审查发现并修复了取消竞态、上游错误未上报和说话状态的问题。

验收时GPU总占用22287MiB、剩余1787MiB，包含其他驻留模型，不能视为demo自身用量。
本次复用已驻留StreamingTalker，没有额外加载数字人模型。测试结束服务健康、活动会话0，
`/avatar`和原纯语音`/`入口均可用。

本地记录（不进入源码包）：`artifacts/avatar-backend/result.json`、`avatar-interrupt.json`、
`avatar-voice-interrupt.json`、`avatar-browser-interrupt.json`、`avatar-browser-microphone.json`、
`avatar-browser.png`，以及纯语音`artifacts/20260916-235429/`。

尚未验收空白机器全量安装、真人物理麦克风/扬声器、长会话、主观视觉质量或公网网络。
