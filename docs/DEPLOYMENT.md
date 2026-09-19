# 部署与后端协议

[返回项目首页](../README.md)

## 当前机器直接体验

打开现有 Pipecat 地址的 **`/avatar`**，例如
<http://127.0.0.1:18314/avatar>。点击「连接会话」，输入「介绍一下语音 agent 技术」，
或点击「开启麦克风」。连接前可选择已配置的驱动。点击「打断」立即停止旧声音和动画队列，恢复待机循环并保留对话文字；再次开口也可由
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

这是**模型服务之上的集成 demo**。先准备下表中四个兼容后端；安装 Python demo 本身不会下载、
启动全部模型，也不能把任意 兼容云端 API 直接填入这些自定义协议地址。
已有验证使用部署好的本地模型服务，部分服务经 SSH 隧道连接另一台自有 GPU 主机；尚未完成空白 GPU 机器的全量安装验收。

源码包已完成[独立应用环境安装检查](SOURCE-INSTALL-VALIDATION.md)：新建虚拟环境安装100个锁定依赖，203项分层测试及本机页面/能力/健康接口检查通过。该检查复用现有系统库，未安装GPU模型或验证真实推理。

1. 安装 Python 3.11、`uv`、Node 18+（仅测试需要），以及系统音频依赖。
2. 解压源码包，在目录中执行 `bash install_demo.sh`。仅创建本目录 `.venv`，按锁文件安装
   Pipecat 1.10.0 和依赖，下载并校验 NLTK 分句资源。
3. `cp .env.example .env`，填写已启动的模型服务与有权使用的音色 ID。
4. 启动：

```bash
set -a
source .env
set +a
mkdir -p runtime/numba-cache
.venv/bin/python bot.py --host 127.0.0.1 --port 18314 -t webrtc
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
