# 独立 IndexTTS 2.5 worker

此入口提供 VoxaStage 已有 TTS 协议，移除对既有业务服务的部署依赖。
模型和文本分段合成来自 [IndexTTS 官方项目](https://github.com/index-tts/index-tts)，
固定源码版本 `a371df7d0746a0ae7fdf075798b6b04e34a0132e`。
VoxaStage 新增协议服务、单模型调度、取消清理、管理员音色映射及流式重采样。
**上游先完整合成一个文本片段，再输出该片段音频；这不是语音 token 级流式推理。**

目前完成独立 Python 环境安装、模型类导入及 CPU 协议验证，尚未完成此 worker 的
真实 GPU 模型加载、双音色合成和首包时延验证。现有日常服务没有切换。
参考 [实施与验收计划](../docs/plans/2026-10-16-tts-standalone.md)。

## 安装与资源

目标环境为 Linux x86_64、Python 3.11、支持 BF16 的 NVIDIA CUDA GPU。
模型依赖固定为 PyTorch/Torchaudio 2.8.0+cu128、Transformers 4.52.1。
`requirements-standalone.lock` 锁定154个发行包（包括 HTTP 客户端验证依赖）。
它来自官方冻结安装后记录的完整环境，不包含宿主驱动、系统音频库或模型资源。
不启用 DeepSpeed、自定义 CUDA kernel、torch.compile 或文本情感模型。

```sh
git clone https://github.com/index-tts/index-tts.git /path/to/index-tts
git -C /path/to/index-tts checkout --detach a371df7d0746a0ae7fdf075798b6b04e34a0132e
uv venv --python python3.11 runtime/tts-standalone
uv pip sync --python runtime/tts-standalone/bin/python --require-hashes \
  --index-url https://pypi.org/simple \
  --extra-index-url https://download.pytorch.org/whl/cu128 \
  --index-strategy unsafe-best-match tts-server/requirements-standalone.lock
uv pip check --python runtime/tts-standalone/bin/python
```

索引策略允许解析 PyTorch 的 `+cu128` 包；精确版本和 SHA256 仍必须匹配锁文件。
重新生成锁使用相同索引及
`uv pip compile --generate-hashes --no-header --no-annotate tts-server/requirements-standalone.in`。
模型来源为 [IndexTeam/IndexTTS-2.5](https://huggingface.co/IndexTeam/IndexTTS-2.5)。
完整模型目录必须包含 `tts_native.py` 中 `REQUIRED_MODEL_FILES` 列出的全部资源，
包括 `hf_cache/w2v-bert-2.0`、`hf_cache/bigvgan` 和 `hf_cache/campplus_cn_common.bin`。
基础权重文件存在不代表辅助资源齐全；启动前检查缺失项，HF 加载设为离线。
本次已将14个实际使用文件与四个官方固定 revision 比对，全部匹配，记录在
[`model-files.json`](model-files.json)。启动时重新校验全部文件大小和 SHA256。
未纳入固定输入的 `glossary.yaml` 会被拒绝；需要自定义词表时应另建实验清单。
文件来源匹配不等于 GPU 加载或真实合成通过。

可在模型环境按清单取得资源（下载工具使用网络，worker 启动不下载）：

```sh
runtime/tts-standalone/bin/python - <<'PY'
import json
from pathlib import Path
from huggingface_hub import hf_hub_download
root = Path("/path/to/IndexTTS-2.5")
manifest = json.loads(Path("tts-server/model-files.json").read_text())
for group in manifest["groups"]:
    for item in group["files"]:
        hf_hub_download(repo_id=group["repository"], revision=group["revision"],
                        filename=item["upstream_name"],
                        local_dir=root / Path(item["local_name"]).parent)
from tts_native import verify_model_files
verify_model_files(root)
PY
```

参考声音不随仓库分发。由管理员创建 `voices.json`，只允许1–8个唯一 ID、绝对路径，
WAV 为单声道 PCM16、8–48kHz、1–15秒。例：

```json
[
  {"id": "pipecat_male", "wav_local_path": "/path/to/authorized-male.wav"},
  {"id": "female11", "wav_local_path": "/path/to/authorized-female.wav"}
]
```

## 启动

在 VoxaStage 根目录执行，使用空闲端口进行验收：

```sh
env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 \
  runtime/tts-standalone/bin/python tts_server.py \
  --source /path/to/index-tts --model /path/to/IndexTTS-2.5 \
  --voices /path/to/voices.json --port 19008
```

服务只监听 localhost。模型成功加载后才完成启动。应用选择
`PIPECAT_TTS_URL=http://127.0.0.1:19008`；音色 ID 沿用已有男女角色配置。
未完成模型验收前，不替换日常 `PIPECAT_TTS_URL`。

`GET /health` 返回 `ready`、`busy`、`streaming: "text-segment"` 和输出采样率。
`POST /api/tts/list` 只返回音色 ID，不暴露路径。
`/ws/tts` 接收与 `backend.py` 相同的四条文本 JSON：`open_stream`、`is_start`、`text`、`is_end`。
每条都携带相同的 `session_id`、`transaction_id`、`audio_id`、`audio_sr:24000`。
合成文本限制1000字符，一条连接只接受一次合成。返回24kHz单声道 PCM16LE，
正常完成返回 `{"is_end":1,"code":0}`；错误返回 `error` 和400/409/500/504。

源音频按22.05kHz连续重采样，保留最终尾部；网络包上限40ms，队列最多四包。
这些40ms包只是传输分包，不能把它们视为模型每40ms生成一次。
握手限时10秒，合成发送限时90秒。断连或超时请求协作取消并丢弃旧音频；
正在执行的 GPU kernel 不能立即停止。直到推理生成器清理完毕，第二请求仍返回409。
关闭服务会等待已有推理线程退出。

## CPU 验证

```sh
runtime/tts-standalone/bin/python -m unittest \
  test_tts_native test_tts_server test_tts_wire test_backend -v
```

`test_tts_wire` 实际启动临时 localhost HTTP/WebSocket 服务并调用原有 `LocalBackend`。
模型使用合成波形替身；它证明协议、采样率和生命周期，不证明语音质量、真实推理速度或口型效果。
