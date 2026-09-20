# 部署与后端协议

[返回项目首页](../README.md)

## 当前机器直接体验

打开现有 Pipecat 地址的 **`/avatar`**，例如
<http://127.0.0.1:18314/avatar>。点击「连接会话」，输入「介绍一下语音 agent 技术」，
或点击「开启麦克风」。连接前可选择已配置的对话模型和角色；角色绑定驱动、声音和 idle。会话保存、恢复与工具范围见[流式功能说明](STREAMING-FUNCTIONS.md)。点击「打断」立即停止旧声音和动画队列，恢复待机循环并保留对话文字；再次开口也可由
VAD 自动打断。纯语音 WebRTC 页面仍是 `/`。两种入口共享一个会话名额，测试前先断开另一页面。

在当前工程中 `python3 ctl.py stop` / `python3 ctl.py start` 管理 Pipecat，保留模型进程。
数字人本机地址保存在被忽略的 `runtime/avatar-config.json`（`{"url":"ws://.../v1/stream"}`），
`PIPECAT_AVATAR_URL` 优先级更高。该配置不进入发布包。

2D 服务准备好后设置 `PIPECAT_FLASHHEAD_URL=ws://127.0.0.1:8203/v1/stream`。
DINet 设置 `PIPECAT_DINET_URL=ws://127.0.0.1:19003/api/ws/live_video/<character_id>`，使用既有部署服务的自定义音频接口（并非 DINet 官方仓库的标准 API）。角色资源须由部署者提供；不随本源码包分发。FlashHead 继续使用 `/v1/stream` 桥接协议。
`PIPECAT_AVATAR_PROVIDER` 可更改默认驱动。也支持以下本地配置，修改后重启 Pipecat：

```json
{
  "default_provider": "streamingtalker",
  "providers": {
    "streamingtalker": {"url": "ws://127.0.0.1:12544/v1/stream"},
    "flashhead": {"url": ""},
    "dinet": {"url": "ws://127.0.0.1:19003/api/ws/live_video/<character_id>"}
  }
}
```

空地址表示未配置；配置地址并不代表模型已加载或健康。用户界面不会展示内部服务地址。

## 在另一台机器启动

这是**模型服务之上的集成 demo**。先准备所选链路需要的兼容后端；安装 Python demo 本身不会下载、
启动全部模型，也不能把任意云端 API 直接填入这些自定义协议地址。
已有验证使用部署好的本地模型服务，部分服务经 SSH 隧道连接另一台自有 GPU 主机；尚未完成空白 GPU 机器的全量安装验收。

源码包已完成[独立应用环境安装检查](SOURCE-INSTALL-VALIDATION.md)：新建虚拟环境安装100个锁定依赖，203项分层测试及本机页面/能力/健康接口检查通过。该检查复用现有系统库，未安装GPU模型或验证真实推理。

1. 安装 Python 3.11、`uv`、Node 18+（仅测试需要），以及系统音频依赖。
2. 解压源码包，在目录中执行 `bash install_demo.sh`。仅创建本目录 `.venv`，按锁文件安装
   Pipecat 1.10.0 和依赖，下载并校验 NLTK 分句资源。
3. `cp .env.example .env`，填写已启动的模型服务与有权使用的音色 ID。
4. 启动：

```bash
.venv/bin/python demo.py check
.venv/bin/python demo.py run
```

浏览器打开 `/avatar`。远程机器可用 SSH 将 18314 转发到本地，通过 localhost 访问，
以满足浏览器麦克风安全上下文要求；此版没有公网认证与多租户隔离。

| 配置 | 必须实现的协议 |
| --- | --- |
| `PIPECAT_ASR_URL` | `GET /health` 返回 `ready`；`POST /transcribe` 接收16k单声道PCM16 WAV，返回 `{"text":"..."}`。包内 `asr_server.py` 可作参考，模型运行环境单独准备。 |
| `PIPECAT_LLM_URL` | `GET /health`；`POST /generate` 接收 `messages,max_new_tokens`，返回逐行JSON增量 `{"text":"..."}`，错误用 `{"error":"..."}`。 |
| `PIPECAT_TTS_URL` | `POST /api/tts/list` 返回 `speaker_ids`；`/ws/tts` 接收 `open_stream,is_start,text,is_end` 四条JSON，返回24k PCM16二进制及终止 `is_end`。完整字段见 `backend.py`。 |
| `PIPECAT_AVATAR_URL` | StreamingTalker 扩展 `/v1/stream`，接收16k PCM16，输出30fps拓扑、网格和完成事件。安装与固定上游版本见 [avatar-server/README.md](../avatar-server/README.md)。官方项目原版并不自带本扩展API。 |

数字人音频使用流式24k→16k重采样，模型输出每帧搭配**原始24k TTS PCM**；最后不足一个
视频帧的音频保留、维持最后嘴型。模型使用200ms块和200ms前视，并非零前视因果模型。


## 检查与启动入口

`demo.py` 自动读取项目根目录的 `.env`，按 **runtime/backend-env.json → .env → 进程环境变量 → 命令行所选模型/角色** 的顺序覆盖配置。`.env` 只读取字面值，可使用成对引号；不执行命令、不展开 `$变量`，行末注释也不作特殊处理。相对资源路径以项目目录为基准；本地 ASR / LLM / TTS 地址的结尾斜杠统一移除。运行配置不会写回文件。

```bash
# 级联：仅要求 ASR、LLM、TTS 和当前角色的数字人服务
.venv/bin/python demo.py check --backend cascade --profile dinet
.venv/bin/python demo.py run --backend cascade --profile dinet

# 端到端看图：仅要求支持视觉的 MiniCPM 与当前角色的数字人服务
.venv/bin/python demo.py check --backend minicpm --profile flashhead --require-vision
.venv/bin/python demo.py run --backend minicpm --profile flashhead --require-vision

# 自动化诊断；解释器和端口可显式指定
python3 demo.py check --python .venv/bin/python --env-file .env --json
.venv/bin/python demo.py run --port 18316
```

内置角色 ID 为 `streamingtalker`、`flashhead`、`dinet`；自定义角色见[角色配置](STREAMING-FUNCTIONS.md)。`--profile` 同时设定网页默认角色，后续可在连接前切换。端到端模式的音色由 MiniCPM worker 提供，级联模式则检查当前角色绑定的 TTS 音色。

检查包含 Python 3.11、锁定依赖、NLTK 分句资源、所选链路的服务状态和视觉能力。`run` 还检查端口，失败不会停止已有服务；通过后以前台方式监听 localhost，Ctrl+C 停止应用。它不安装、启动或重启 GPU 模型。

以下提示不阻止启动，但有明确边界：

- DINet 原生接口没有统一健康端点，仅验证 TCP 可达；人物资源与实际推理需单独验证。
- 云端兼容接口只检查配置是否完整，不发送计费请求，因此不证明云端服务可用。
- 缺少 idle 素材时使用页面占位；自行准备有权使用的视频再配置。
- 健康状态不等于真实识别、合成、口型或回答质量通过。

## 各模型怎样准备

各 worker 保持独立环境，不把冲突的 GPU 依赖合并到应用虚拟环境。先搭一条需要的链路即可，三种数字人不必同时部署。

| 服务 | 本仓库提供什么 | 部署者还需准备什么 |
| --- | --- | --- |
| 本地 ASR | `asr_server.py`，支持 Qwen / SenseVoice 的话轮转写接口 | 相应推理环境与权重；例如在该环境运行 `python asr_server.py --engine qwen --model /path/to/model --port 18315`，应用地址与端口一致 |
| 本地 LLM | `llm_server.py`，支持增量文字与工具生成 | 模型推理环境与权重；例如 `python llm_server.py --model /path/to/model --port 18311 --quantization 4bit`，量化需对应 GPU 依赖 |
| IndexTTS | [音色注册兼容层](../tts-compat/README.md)及应用协议适配 | 既有 TTS 推理服务、有权使用的男女参考音色；兼容层本身不是完整推理服务 |
| MiniCPM-o | [worker、部署命令与已有环境说明](../omni-server/README.md) | 独立模型环境、权重、参考声音；看图需要显式开启视觉，尚未验收空白机器安装 |
| FlashHead | [固定上游版本、安装脚本、模型准备和桥接服务](../video-server/README.md) | 独立 GPU 环境、权重、人物素材；按该文档的已验证范围部署 |
| StreamingTalker | [固定上游补丁、服务依赖和启动命令](../avatar-server/README.md) | 模型/音频编码器权重与运行环境；当前说明不是全新机器验证过的完整安装器 |
| DINet | [原生接口接入说明](DINET-DEPLOYMENT.md) | 已部署的兼容 DINet 服务与人物模板；本项目不包含完整人物训练/模型部署系统 |

应用环境安装证据见[独立安装检查](SOURCE-INSTALL-VALIDATION.md)。统一检查/启动入口减少配置遗漏，**不代表空白 GPU 主机全模型一键安装已完成**。
