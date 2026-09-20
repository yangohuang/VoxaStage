# 第三方来源与贡献边界

[返回项目首页](README.md)

VoxaStage 是基于公开模型与既有模型服务构建的集成项目。本页区分运行依赖、随包扩展代码、部署素材与本项目实现，避免把上游算法能力归为本项目原创。所有上游项目均独立维护，本项目不是其官方发行版。

## 核心来源

| 来源 | 在本项目中的用途与分发范围 | 版本 / 依据 |
| --- | --- | --- |
| [Pipecat](https://github.com/pipecat-ai/pipecat) · Daily / 社区 | 会话处理器、VAD 集成、话轮、上下文聚合与传输。通过依赖安装，不复制整个框架。 | `pipecat-ai==1.10.0`，见 [requirements.in](requirements.in) 与锁文件。 |
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) · Qwen 团队 | 本地语音识别模型；仓库提供 HTTP 服务与客户端适配，不含权重。 | 已部署验证 1.7B；模型 revision `7278e1e70fe206f11671096ffdd38061171dd6e5`。示例环境仍从 0.6B 起步。 |
| [Qwen3](https://github.com/QwenLM/Qwen3) · Qwen 团队 | 级联链路中的文本大模型；仓库提供流式服务和提示配置，不含权重。 | 当前本地默认 Qwen3-4B NF4；候选比较见[体验记录](docs/EXPERIENCE-UPGRADE.md)。 |
| [IndexTTS](https://github.com/index-tts/index-tts) · IndexTTS 团队 | 级联语音合成引擎。连接部署者已有的 WebSocket 服务；仓库包含客户端及固定音色兼容扩展，不包含完整推理服务。 | [TTS 兼容层](tts-compat/README.md)；不能把本项目协议当作上游原生 API。 |
| [MiniCPM-o](https://github.com/OpenBMB/MiniCPM-o) · OpenBMB | 端到端语音模型及可选视觉编码器；仓库提供 worker、历史与取消管理、按需画面输入契约，不含模型代码快照或权重。视觉真实推理仍待验收。 | [`openbmb/MiniCPM-o-4_5-awq`](https://huggingface.co/openbmb/MiniCPM-o-4_5-awq)，revision `a3073852f52e4beec3f278d1f6616d40c26fe343`；[部署说明](omni-server/README.md)。 |
| [DINet](https://github.com/MRzzm/DINet) · 论文作者 | 音频驱动的人脸视频生成。这里只分发客户端及特定既有部署的节奏启动器，不分发 DINet 训练代码、原生推理实现或角色资源。 | [部署协议](docs/DINET-DEPLOYMENT.md)。既有服务的完整实现与版本未在本仓库审计，不宣称与官方仓库原样等同。 |
| [SoulX-FlashHead](https://github.com/Soul-AILab/SoulX-FlashHead) · Soul AI Lab | 使用 Lite 管线生成 2D 肖像帧；分发桥接 worker 和单 GPU 兼容补丁，不分发上游 checkout 或权重。 | 源码固定 `9bc03de06bb0de82cd6bc477804512ae06144bf2`；[worker 与补丁说明](video-server/README.md)。 |
| [StreamingTalker](https://github.com/zju3dv/StreamingTalker) · ZJU3DV | 使用音频驱动的 3D 人脸模型；随包的是既有本地增量服务扩展、测试、补丁和审计清单。 | 内容比对基线 `25b613ac273624947e5fa51c4c41c4933d8147be`；[NOTICE](avatar-server/NOTICE.md)、[逐文件比对](avatar-server/upstream-comparison.json)。 |

MindTalker 是作者此前基于 MiniCPM-o 的项目。本次端到端接入参考了其接口经验；本仓库未打包原 MindTalker 私有工程，也没有把它作为第三方基础模型。

## 配套依赖与素材

- **VAD**：使用 Pipecat 的 Silero VAD 集成，模型来自 [Silero VAD](https://github.com/snakers4/silero-vad)。
- **音频与视频处理**：使用 [soxr](https://github.com/dofuuz/python-soxr)、[PyAV](https://github.com/PyAV-Org/PyAV) 等现有库。浏览器播放与绘制依托 Web Audio、Canvas2D 和 WebGL。直接与传递依赖见各环境锁文件。
- **FlashHead 模型依赖**：Lite 管线还需要上游指定的 LTX VAE 和 wav2vec2 资源；下载器记录解析后的版本及文件校验值。它们不是 VoxaStage 自研模型，来源与部署入口见 [FlashHead 说明](video-server/README.md)及[下载器](video-server/fetch_models.py)。
- **StreamingTalker 模型依赖**：需要 HuBERT、VQ-VAE、FLAME / VOCASET 模板等。文件来源与准备方式见 [StreamingTalker 部署说明](avatar-server/README.md)，权重与数据不随本仓库分发。
- **演示音色**：当前男声参考使用 [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) 的固定音色离线生成，女声来自既有部署的参考资源。Qwen3-TTS 不是当前运行链路的必需服务；音频不随本仓库分发，部署者需自行准备音色。
- **展示截图**：[docs/assets](docs/assets) 中四张 PNG 来自本地联调界面和模型生成的 idle 画面，仅用于说明集成效果。它们不是基础人物素材或训练集；底层肖像的来源与许可未在本仓库完整记录，不能据此取得人物、肖像或音色的再使用权。

## 本项目实现了什么

| 实现范围 | 主要代码 |
| --- | --- |
| 本地模型适配、可选兼容 API、话轮结束策略 | `backend.py`、`services.py`、`api_backends.py`、`turn_strategy.py` |
| 对话后端与数字人驱动独立选择 | `dialogue_backends.py`、`avatar_providers.py`、`omni_processor.py` |
| 数字人媒体协议、连续重采样、帧与原音频配对 | `avatar_backend.py`、`avatar_video_backend.py`、`dinet_backend.py`、`AVATAR-PROTOCOL.md` |
| 打断隔离、会话准入、取消清理、长回复分段与背压 | `avatar_session.py`、`avatar_demo.py` |
| 浏览器交互、同一时钟播放音画、角色切换与 idle | `avatar-web/` |
| FlashHead 桥接、MiniCPM worker、StreamingTalker 增量服务扩展 | `video-server/`、`omni-server/`、`avatar-server/serving/` |
| 角色音色兼容、验证脚本与回归测试 | `tts-compat/`、`smoke_avatar.py`、`smoke_video.py`、各 `test_*` 文件 |
| 固定版本DINet部署的消费提前量、256人物配置与有界代理清理启动器；原生服务和模型仍由部署者提供 | `dinet-server/` |

这些工作不包含上述基础模型的预训练、原创网络设计或数据集构建。真实工具调用与慢任务编排尚未实现，不应作为现有能力宣传。

## 许可说明

- VoxaStage 的集成层与前端使用根目录 [MIT License](LICENSE)。第三方代码、补丁对应的上游文件、权重和素材不因该许可而改变自身条款。
- `avatar-server/LICENSE` 保留所用历史 StreamingTalker 基线的 Apache-2.0 原文。上游后续提交 `1f6452752dbd6c11f4bb1e94982e000e68d8f76d` 已修改许可；本项目不把当前上游 `main`、模型权重或数据集统称为 Apache-2.0。完整说明见 [NOTICE](avatar-server/NOTICE.md)。
- 其他依赖以实际安装版本、权重发布页及素材提供方条款为准。源码许可与模型、人物、声音、数据的使用许可分别处理；本页不是所有素材已获重新分发授权的声明。
- 若后续公开仓库，需先补齐演示肖像来源和展示授权记录，或替换为来源清楚的演示素材。当前私有联调截图不应被当作可自由复用的公共素材。
