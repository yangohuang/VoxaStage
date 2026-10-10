# MiniCPM-o 端到端语音后端

状态：本地协议、Pipecat 路由与取消测试，以及真实模型文本、语音、多轮记忆、三种数字人组合的打断恢复测试通过。详见 [实测记录](../docs/MINICPM-RESULTS.md)。
本适配参考既有 MindTalker 的 MiniCPM-o 接口，将对话模型与数字人渲染分开选择。
MiniCPM-o 直接接收 16k 单声道原始语音或文字，生成增量文字与 24k PCM；
这条回复路径不调用外部 ASR、Qwen LLM 或 IndexTTS。
数字人继续复用 DINet、FlashHead 或 StreamingTalker 的适配器。

## 独立环境重建

`deploy/requirements-standalone.in` 声明 Python 3.10 / Linux x86_64 / CUDA 13.0 的运行依赖，`deploy/requirements-standalone.lock` 固定完整分发版本与 SHA256。它包含 MiniCPM TTS extras、AWQ 的 Triton 路径和参考音色读取所需 CPU 版 TorchCodec；不继承用户目录或旧 Conda 包。需要兼容 CUDA 13.0 的驱动及可供 TorchCodec 加载的 FFmpeg 动态库。

下列命令只安装新的虚拟环境，不获取模型、音色或修改已有服务。先准备 Python 3.10 与 uv，从项目根目录运行：

```sh
test ! -e runtime/minicpm-standalone || exit 1
uv venv --python python3.10 runtime/minicpm-standalone
uv pip sync --python runtime/minicpm-standalone/bin/python --require-hashes \
  --extra-index-url https://download.pytorch.org/whl/cu130 \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match omni-server/deploy/requirements-standalone.lock
uv pip check --python runtime/minicpm-standalone/bin/python
```

**已知上游标签问题：** 当前锁的 `decord==0.6.0` 在官方 wheel 文件名中声明 `py3-none-manylinux2010_x86_64`，内部 `WHEEL` 却写成 `cp36-cp36m-manylinux2010_x86_64`。因此 `uv pip check` 会以非零退出并报告该包的平台不匹配；上游记录见 [Decord #356](https://github.com/dmlc/decord/issues/356) 和 [uv #15832](https://github.com/astral-sh/uv/issues/15832)。不要把这次检查写成通过，也不要用全局忽略错误的方式跳过其他依赖问题。

本次保持上游包字节与元数据不变，另行核验119个版本、180条有效依赖关系、107项 Python 版本约束和环境隔离；在 Python 3.10 下完成四帧合成视频的顺序/跳帧读取，RGB 输出与系统 FFmpeg 解码逐像素一致。这些证据限定该环境中的已知标签例外，不证明完整模型或跨平台兼容性；真实 MiniCPM 推理已另行完成[42次验证与服务恢复](../docs/MINICPM-STANDALONE-VALIDATION.md)。

安装后必须在该解释器中验证 TorchCodec/FFmpeg 能读取实际参考 WAV，再运行 worker 契约测试及真实模型请求；`pip check` 无法发现未写入包元数据的运行依赖。TorchAudio 2.11 的 `load()` 使用 TorchCodec，即使调用处写了 `backend="soundfile"`；librosa 0.9 仍导入 pkg_resources，所以锁中保留支持该模块的 setuptools。

参考音色解码与不加载权重的契约检查：

```sh
env -u PYTHONPATH -u PYTHONHOME -u LD_LIBRARY_PATH \
  PYTHONNOUSERSITE=1 runtime/minicpm-standalone/bin/python -c \
  'import sys,torchaudio; audio,sr=torchaudio.load(sys.argv[1]); assert audio.numel()>0; print(sr,audio.shape)' \
  /path/to/authorized-reference.wav
(cd omni-server && env -u PYTHONPATH -u PYTHONHOME -u LD_LIBRARY_PATH \
  PYTHONNOUSERSITE=1 ../runtime/minicpm-standalone/bin/python \
  -m unittest discover -p 'test_*.py' -q)
```

`evaluation/multimodal.py` 使用 `asyncio.timeout`，必须在 **Python 3.11 以上的应用/评测环境**运行，不能直接用本节 Python 3.10 模型环境执行。客户端通过 HTTP 访问独立模型服务，二者无需共享解释器。

AWQ 的可选扩展未加入新锁：既有环境该扩展导入失败，实际 GEMM 使用 Triton，新环境需核验同一路径。AutoAWQ 会提示它的上游测试版本与本环境不同；只有真实推理验证能支持当前组合可用。其他部署仍需先完成真实推理验证再切换。当前锁的实测范围与已知例外见[独立环境报告](../docs/MINICPM-STANDALONE-VALIDATION.md)。

重新解析依赖需要再次验证：

```sh
uv pip compile omni-server/deploy/requirements-standalone.in \
  --python python3.10 --python-platform x86_64-unknown-linux-gnu \
  --extra-index-url https://download.pytorch.org/whl/cu130 \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match --generate-hashes --no-header --no-annotate \
  --output-file omni-server/deploy/requirements-standalone.lock
```

模型自定义代码、权重、参考音色、系统库和基础解释器仍由部署者准备，锁不覆盖这些资产，也不锁定源码包构建工具。worker 代码、参考素材及启动配置应保存在持久部署目录，避免依赖重启后可能清理的系统临时目录。

## 部署

模型需单独 GPU 环境，不能直接安装进 Pipecat 的锁定环境。已检查的既有环境为
Python 3.10、torch 2.11.0+cu130、transformers 4.51.0、autoawq 0.2.9、
minicpmo-utils 1.0.6，包含 FastAPI、uvicorn、librosa 与 numpy；
这套既有环境已完成实际加载与推理。AutoAWQ 会提示上游测试版本差异；本次结果不能代替空白机器的依赖安装验收。

预期模型为 `openbmb/MiniCPM-o-4_5-awq`，本次检查的固定缓存版本为
`a3073852f52e4beec3f278d1f6616d40c26fe343`，需完整权重及 `assets/token2wav`、
`assets/system_ref_audio.wav`。模型有自定义 Python 代码，worker 从指定本地目录加载。
本仓库不分发模型权重、参考音色或原 MindTalker 的私有文件。

在已准备好且获授权的模型主机启动（替换路径）：

```bash
PYTHONNOUSERSITE=1 /path/to/minicpm/bin/python worker.py \
  --model /path/to/pinned-model-snapshot --port 18201
```

worker 只监听 `127.0.0.1`，远端使用 SSH 本地端口转发访问。当前部署因原端口被占用改用 18211；示例中的 18201 可按部署环境修改。
模型加载完后 `GET /health` 才可用，返回 `ready`、`busy`、采样率与请求计数。
随后在 Pipecat 主机设置并重启应用：

```bash
export PIPECAT_MINICPM_URL=http://127.0.0.1:18201
export PIPECAT_DIALOGUE_BACKEND=cascade
```

也可写入被忽略的 `runtime/dialogue-config.json`：

```json
{"minicpm_url":"http://127.0.0.1:18201","default_backend":"cascade"}
```

环境变量优先。`/avatar` 页连接前选择「MiniCPM-o · 端到端语音」，再选择数字人。
已配置仅代表有地址，连接时会检查模型健康。纯语音 `/` 入口仍使用级联后端。

## 会话与边界

- 使用 Silero VAD 分轮，支持插话打断；尚未实现或验证模型原生全双工。
- 最近最多六轮历史加当前输入，累计用户语音不超过 60 秒，文本不超过 12000 字符。
  超限按旧轮裁剪。每次推理重建模型上下文，未验证长历史延迟与显存上限。
- 原始用户语音以音频保留，不使用外部 ASR 生成伪转写；页面显示「语音输入」。
- 按实际播放确认保留完整语句段；打断后未确认部分以中断标记代替。对话保存在本地，载入同一会话可继续历史；新建或删除会话由用户显式操作。
- 单次输入最多 30 秒，VAD 层约 28 秒切段；输出最多 256 tokens、30 秒音频。
- HTTP 断开会通知推理线程停止，在 prefill 与生成块之间检查；已提交的 CUDA 运算无法强行中断。
  模型锁一直保留到生成器清理结束，忙碌返回 409，客户端最多等待 30 秒。
- 同时只接受一条推理；输出队列最多四个事件，音频事件最多一秒。部署时仍需验证实际显存、延迟和恢复能力。

## 协议与验证

`POST /generate` 接收 `{"messages":[{"role":"user","text":"你好"}]}`。
音频消息改用 `audio` 字段，值为 16k 单声道 PCM16 小端的 base64，无 WAV 头。
历史仅允许 user/assistant，最后一条必须为 user；系统提示由 worker 控制。
响应为 NDJSON：metadata(protocol=1, sample_rate=24000)、text、audio、done(samples)，或 error。
audio 为 24k PCM16 小端 base64，客户端校验总采样数、事件大小与完整结束标记。

```bash
# 在 pipecat-local 下运行，均使用假模型，不加载权重
.venv/bin/python -m unittest test_dialogue_backends test_omni_backend test_omni_processor test_omni_route
(cd omni-server && ../.venv/bin/python -m unittest test_worker)
```

覆盖原音频直入模型、绕过级联组件、增量媒体输出、打断后迟到结果丢弃、播放回执历史、
历史裁剪、协议截断、断线释放会话，以及 HTTP 取消时模型锁和忙碌恢复。
真实文本与语音、多轮记忆、打断恢复、三个渲染器组合已测试；物理麦克风试听、长历史显存上限和长时间稳定性仍待评估。

## 角色音色

启动时可设置 `--male-reference /path/male.wav --female-reference /path/female.wav`。
应用按角色发送固定 `male` / `female` 枚举；worker在推理锁内切换系统音色参考和token2wav缓存。
男声参考缺失时男性角色请求明确失败；需自行提供有权使用的音频。
本机部署见 [体验升级记录](../docs/EXPERIENCE-UPGRADE.md)。

## 可选视觉输入

增加`--vision`才加载视觉编码器，健康接口明确返回`capabilities.image_input`。用户消息可附带两张以内的有界JPEG/PNG及采集元数据；原语音按一秒分块，图片只附在首块。未启用视觉时明确拒绝图片，不静默忽略。启动部署需同时复制`worker.py`、`visual_input.py`和`visual_grounding.py`。

界面、时间与历史边界见[视觉交互](../docs/VISUAL-INTERACTION.md)。真实图文/图音、两帧比较及三驱动流程已有[受控实测](../docs/REAL-MULTIMODAL-BASELINE.md)，当时实验完成后恢复语音服务；2026-09-20 的现有演示已启用视觉和 general 约束。新部署仍需显式添加视觉参数，详情见[当前状态](../docs/STATUS.md)。两图合成探针不代表通用识图质量；无图时猜测视觉属性的问题另有失败记录。

## 实验性视觉证据提示

`--visual-grounding baseline|general|evidence`由部署者选择，默认`baseline`逐字保持原系统提示。`general`增加缺图时不猜测的通用规则；`evidence`再加入服务端统计的本轮、历史及合计可见图片数，文字中的图片声明不增加计数。该策略也适用于纯文字/语音请求；普通问题仍要求正常回答。HTTP客户端不能覆盖策略，健康接口在`capabilities.visual_grounding_policy`返回实际选择。

策略构造与worker接入已通过18项契约测试，并完成[111次真实生成的受控消融](../docs/VISUAL-GROUNDING-ABLATION.md)。在一条重复的无图语音问题上，原提示8/8断言视觉属性，general和evidence均为0/8；有图及普通问题控制保持正常。当前没有证据支持evidence比general更好，不外推通用幻觉解决。默认仍为baseline，部署者可显式选择general。
