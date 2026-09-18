# 2D / 3D 扩展进展与验收记录

日期：2026-09-18。本记录验收范围：**DINet 2D、FlashHead 2D、StreamingTalker 3D**。完成这轮验收后，用户另行要求增加 MiniCPM-o 端到端对话后端；现已部署并通过真实语音、多轮记忆及三种渲染器打断恢复测试，见 [MiniCPM 实测记录](docs/MINICPM-RESULTS.md)。它作为对话后端接入，不新增 MindTalker 渲染器。

## 已实现与验证

- 驱动注册表：StreamingTalker 3D、DINet 2D、FlashHead 2D；默认兼容原 3D。浏览器无法指定任意服务地址。未配置驱动禁用并说明原因。
- 2D 桥接适配器：JPEG 帧与原始 24k TTS PCM 配对，保留尾音；有界输入、帧校验、超时、取消与忙碌重试。该协议为本项目定义，不是未经确认的 MindTalker 原生 API。
- 双渲染器：WebGL 网格 / Canvas2D 肖像，共用 WebAudio 时钟；异步 JPEG 解码保持顺序，clip_end 等待解码，打断丢弃并释放旧帧。
- 每轮保留人物静帧及对话文字。显式断开或切换驱动会清理画面。
- FlashHead worker：对接官方 Lite 推理函数，连续 24k→16k 重采样，8 秒音频历史，24 帧 / 0.96 秒增量输出；只保留真实音频对应的帧。

2026-09-18 本轮测试：

| 验证 | 实际结果 | 边界 |
| --- | --- | --- |
| Pipecat Python 回归 | 34 项完整通过 | 含已有 ASR、TTS、3D 和新增注册表、2D 适配器、尾帧边界 |
| 浏览器 JS 单元测试 | 3 个测试文件通过 | 包含异步解码、尾音、旧帧隔离 |
| FlashHead worker 契约 | 8 项通过；另测11个PCM长度边界通过 | 使用 fake engine；不代表模型已运行 |
| 真实 StreamingTalker 浏览器 | 三轮、6 条对话文字、350 帧呈现，无页面错误 | 文本输入，本轮未重跑物理麦克风 |
| 播放中打断 | 播放源 9→0；400ms 后帧数保持 315；人物保留 | 浏览器 WebAudio / WebGL 实测 |
| 打断后恢复 | 第三轮“你好”正常完成，generation=5 | 同一会话 |
| 浏览器 2D 播放契约 | 5 帧呈现，播放源 10→0，无解码错误，静帧保留 | 合成 JPEG 测试图；不是 FlashHead 生成画面 |
| FlashHead 上游模块导入 | 通过 | 环境准备验证，无 GPU 推理 |
| 量化后3D语音链路 | 合成WAV输入、VAD打断恢复通过；两轮首媒体分别为测试开始后5.564s/11.072s | 计时包含输入音频播放；不是结束说话后的延迟，也不是物理麦克风验证 |

审查修复：JPEG 解码未完成时结束消息导致长度误报；中断时已解码队列图像泄漏；输入队列满阻塞 cancel；输出故障后等待输入导致推理占用不释放。相应回归已加入。

实机回归还发现：24k→16k 重采样四舍五入可能跨过 30fps 边界，导致多出无音频的网格帧。已按模型输入时长向下取整，原始 TTS 播放音轨仍逐样本保留。固定 25600 样本复现测试修复前失败、修复后通过；真实打断回归通过，首媒体 0.902s，打断后下一轮媒体到达为原请求后 1.625s。

## FlashHead 真实模型验收（2026-09-18）

权重完整下载并通过官方 LFS SHA256 校验。服务在本机 `127.0.0.1:8203` 预加载后启动，网页 FlashHead 选项已启用。MindTalker 仍未启用。

| 验证 | 实际结果 | 证据与边界 |
| --- | --- | --- |
| 真实音频连续三轮 | 每轮90113个24k样本、94帧；输出音频逐样本等于输入 | `artifacts/flashhead-model/results.json`；输入为合成WAV |
| 模型首媒体 | 三轮0.630 / 0.348 / 0.346秒；取消后恢复0.341秒 | 模型已预加载；音频快速送入，不含ASR/LLM/TTS，不是说完话后的端到端延迟 |
| 生成速度 | 每24帧约0.33秒，对应0.96秒音频 | 单次本机测量，未开torch.compile，不是压力测试分位数 |
| 文本完整链路 | LLM→TTS→FlashHead及打断恢复通过；首媒体1.103秒 | `artifacts/avatar-flashhead-text.json`；恢复媒体为原始请求后1.651秒 |
| 合成语音完整链路 | ASR/VAD→LLM→TTS→FlashHead及插话恢复通过 | `artifacts/avatar-flashhead-voice.json`；5.880 / 11.676秒包含输入播放，非物理麦克风 |
| 浏览器播放与打断 | 实际播放时10个音频源立即归零，400ms后帧数保持176，人物静帧保留 | `artifacts/avatar-flashhead-browser.json`，实际Canvas2D/WebAudio |
| 2D→3D→2D切换 | 断开后切换再连接，两种实际模型均正常播放，无页面异常 | 同一浏览器报告；测试结束已释放会话 |
| 图片检查 | 已查看生成尾帧与网页截图，人像正常呈现 | `artifacts/avatar-flashhead-browser.png`；不等于完整口型质量评测 |

FlashHead 的 PyTorch 分配峰值5108.1MiB，缓存预留5620MiB，`nvidia-smi`进程占用6090MiB。其他现有模型保持驻留，整卡余量约0.8GB；本轮文本、合成语音及切换没有OOM，但尚未证明长期运行、并行推理或任意长度输入下稳定。

真实启动发现健康接口引用未定义的busy变量。已改为读取推理租约锁状态，并增加未加载503、空闲200、推理占用与释放后的回归测试；修复前复现、修复后8项全部通过。

浏览器自动化首次直接调用connect被Chrome音频解锁要求挂起；改用实际按钮点击后通过。产品按钮连接路径正常，验收没有绕过音频播放。

上述artifact为本机验收记录，不含在公开源码包中；权重与参考肖像同样不进入源码包。

### 连续推理资源检查

`artifacts/flashhead-soak/results.json`：连续30轮真实模型请求通过，其中第10、20、29轮在首帧后取消，下一轮恢复；各轮均检查音频前缀/完整样本一致性与推理租约释放。每轮释放后GPU已分配显存均为4118.8MiB，最大最小差0.0MiB。本测试总输入约113秒，27轮完整输出、3轮取消；这是短时重复请求与资源回收验证，不是小时级稳定性或并发吞吐承诺。

`artifacts/flashhead-boundary/results.json`：将同一合成语音明确循环至30秒，按实时速度输入，验证服务允许的最长单clip。真实模型输出750帧、720000个24k样本，音频逐样本一致；首媒体1.123秒（包含积累首段输入），总用时30.726秒。该测试验证时长上限与持续输入，循环音频不用于口型质量评测。

### 独立环境部署复验

新增 `video-server/requirements.in`、带SHA256的 `requirements.lock` 和 `install.sh`，在新建的 `runtime/flashhead-standalone` 中完成103个包的安装；`include-system-site-packages=false`，没有继承旧环境的不匹配torchaudio。`uv pip check`全部通过，上游推理模块导入通过，8项worker测试通过。安装脚本拒绝继承系统包的已有环境；重复安装同一独立环境也通过。旧 `requirements.local.lock` 只保留作本机诊断且不导出。

当前FlashHead服务已切换到该独立环境，原环境保留供回退。`artifacts/flashhead-independent-environment.json`记录Python、103个包版本、上游commit、补丁与依赖锁SHA256。

- `artifacts/flashhead-clean-model/results.json`：三轮94帧/90113样本及取消后恢复通过，音频逐样本一致。预加载后首次推理首媒体1.077秒，后两轮0.349/0.348秒，恢复0.339秒；已查看生成图片。
- `artifacts/avatar-flashhead-clean-text.json`：文本完整链路及打断通过，首次媒体1.059秒，恢复媒体为原请求后1.639秒。
- `artifacts/avatar-flashhead-clean-voice.json`：合成语音ASR/VAD完整链路及插话恢复通过，5.863/11.704秒包含输入播放；不是物理麦克风验证。
- 新环境PyTorch分配峰值5105.8MiB，空闲已分配4116.5MiB、预留5628MiB。本次证明同一GPU主机上的独立Python环境安装与推理，不等于在全新操作系统上安装验收。

### 浏览器采集端到端验证

`artifacts/browser-microphone/results.json`：使用Chrome虚拟麦克风播放已知合成WAV，经真实 `getUserMedia → AudioContext → AudioWorklet → PCMResampler → WebSocket → ASR/VAD → LLM → TTS → 数字人 → 浏览器播放`，四次采集全部正确识别“请用一句话解释什么是实时语音交互。”。

| 路径 | 实际结果 |
| --- | --- |
| FlashHead / 正常16k AudioContext | 174帧实际呈现，回复完成 |
| FlashHead / 48k回退 | 模拟创建16k上下文失败，进入产品原有回退路径；48k上下文经worklet重采样后正常识别与播放161帧 |
| FlashHead / VAD插话 | 先确认旧回复正在播放，再开启测试采集；generation 3→4后播放源归零，250ms帧数不变，人物静帧保留；新回复另播放158帧 |
| StreamingTalker / 48k回退 | 同一采集链路产生真实3D网格，217帧实际呈现 |

所有发送包均为640字节，即16k PCM16的20ms音频。正常采集保留产品默认的回声消除、降噪与自动增益设置。关闭麦克风后，音轨结束且包计数不再增长；断开时所有已跟踪的采集AudioContext关闭，音轨结束、采集停止、包计数稳定。没有页面异常。测试浏览器已关闭并释放会话。

验证边界：浏览器设备由 `--use-fake-device-for-media-stream` 和 `--use-file-for-fake-audio-capture=<wav>%noloop` 提供（[Chromium官方开关说明](https://chromium.googlesource.com/chromium/src/+/4cfd129eb1f248567113c50e5e320f971aab77f7/media/base/media_switches.cc)）。它补齐此前直接发送WAV未覆盖的网页采集和重采样路径，但不验证物理麦克风、扬声器回声或人工听感。48k回退通过测试脚本让16k构造失败来触发，没有修改产品代码。

复现脚本保存在同一artifact目录（`instrument.js`、`observe.js`、`barge.js`、`switch.js`），音频为原测试语音前加1秒、后加12秒静音。测试中发现CLI后续命令可能进入另一个Chrome端点，最终对每条命令显式传入同一 `--cdp` 端口，并先滚动按钮到可见区域。曾经的“Requested device not found”来自连接到了未配置虚拟设备的浏览器，未据此修改应用。

## 总目标核对

| 原要求 | 当前证据 | 状态 |
| --- | --- | --- |
| 2D支持FlashHead | 独立安装、真实模型、文本与浏览器采集、连续多轮、VAD/按钮打断、帧/音频配对、资源释放 | 基本集成验收通过 |
| 2D支持DINet（用户替换原MindTalker项） | 原生服务适配、真实三轮、长音频、取消恢复、原音频精确配对 | 基本集成验收通过 |
| 3D使用StreamingTalker | 真实网格服务、文本与浏览器采集、打断恢复、2D/3D切换 | 基本集成验收通过 |
| 同一入口的交互与隔离 | 双渲染器、共用音频时钟、generation隔离、有界队列、静帧和文字保留、单会话控制 | 三个实际驱动均有生成与切换证据 |
| 做好文档记录 | 设计、计划、协议、部署、依赖锁、来源边界、测试证据、源码导出 | 已更新三驱动配置、DINet部署边界与真实证据 |

当前三驱动基本集成已实现。物理麦克风/扬声器、系统性口型质量及小时级稳定性未完整验收，不将自动化测试等同于这些结果。

## 模型与环境记录

- 官方源码：Soul-AILab/SoulX-FlashHead，固定 `9bc03de06bb0de82cd6bc477804512ae06144bf2`；单卡可选 xfuser 导入补丁存放 `video-server/single-gpu.patch`。
- 权重：仅下载 Model_Lite、VAE_LTX、wav2vec2-base-960h，约 8GB。下载可续传并校验 SHA256，具体 revision 和文件清单写入本地 `models/*/source.json`。
- 当前环境：`runtime/flashhead-standalone`，由哈希锁文件安装，独立于系统site-packages。原 `runtime/flashhead-venv`保留供回退，环境快照仅作诊断。
- 参考图：官方 HF Space 示例，只用于本地模型验证，不进入源码包。
- 显存：4090 总量约24GB，原服务组合约22GB。Qwen3-4B 已切换 NF4：进程占用8256→约3126MiB，加载FlashHead前整卡空闲约6.9GB；三条文本测试首字0.036–0.329s、回复正常。真实3D打断回归已通过。FlashHead 加载/生成数据见上表，长期稳定性仍待测。

本机量化服务使用 `research/voice-agent-20260915/lab/services.py` 管理，启动时设置 `LOCAL_LLM_QUANTIZATION=4bit` 和 `LOCAL_LLM_PYTHON` 指向 `runtime/llm-lowmem-venv/bin/python`（绝对路径）。原环境保留；不设置这些变量时仍使用原 BF16 配置。量化会改变数值结果，本轮测试不是完整质量评测。

## 体验与后续评估

- 当前三个驱动已配置，可在断开状态选择；默认仍为 StreamingTalker。
- DINet 复用已有服务及人物资源，源码包只提供客户端适配器与协议，不含 DINet 模型、人物素材或其服务端。
- 物理麦克风/扬声器回声、系统性口型质量及小时级稳定性作为后续体验评估；新增 MiniCPM 后端另行验收，不改变本页三种渲染器的既有测试结论。

## 复验命令

```bash
.venv/bin/python -m unittest test_backend test_services test_asr_server test_turns test_avatar_backend test_avatar_session test_tts_lifecycle test_avatar_providers test_avatar_video_backend test_dinet_backend
node --test avatar-web/tests/*.test.mjs
runtime/flashhead-venv/bin/python -m pytest -q video-server/tests
```

CPU 测试不得替代模型验收；下载完成也不得自动改成“已接入”。

## DINet 最终适配与验证

- `dinet_backend.py` 复用现有 VideoBackend：输入持续 24k→16k 重采样，严格 6400 字节 / 200ms 原生消息，末块补齐并追加 200ms 模型特征上下文；输出 YUV420P → 最长边640 JPEG，播放保持原始24k PCM。拒绝超限、错误序号、截断及异常尾帧。
- 排查发现旧服务残留分片在恰好拼满时未清空，任意大小发送块会影响长度；短句直接EOS另有少三帧的边界。本地适配器规避这两处，不修改共享服务。
- `artifacts/dinet-model/results.json`：3轮均94帧 / 90113样本，首媒体0.182 / 0.071 / 0.129秒；打断后恢复94帧，首媒体0.108秒。此处是渲染服务测试，不含ASR/LLM/TTS延迟。
- `artifacts/dinet-long/results.json`：16.755秒输入，419帧 / 402113原始样本，约17.043秒完成，无音频扩展。
- 完整文字/语音对话、浏览器中断与三驱动切换的证据分别在 `artifacts/avatar-dinet-text.json`、`artifacts/avatar-dinet-voice.json`、`artifacts/avatar-dinet-browser.json`。WAV入口测试包含输入播放时间，不等于说完话后的响应延迟。
- Python / JS 回归日志：`artifacts/dinet-python-tests.log`、`artifacts/dinet-js-tests.log`。浏览器与模型图片、人物数据均不进入源码包。

最终回归：42项Python / 15项JS通过。最终浏览器结果为DINet恢复20帧，随后FlashHead28帧、StreamingTalker36帧、DINet20帧；中断音源9→0，300ms人物帧稳定。EOS竞争修复后额外真实复验记录为 `artifacts/avatar-dinet-final.json`。
