# Qwen ASR 独立环境

本目录为根目录 `asr_server.py` 提供独立依赖锁，不包含识别模型或新识别算法。Qwen3-ASR 模型与 SDK 属于上游；VoxaStage 提供本地 WAV 协议、术语传递、忙碌互斥和证据采集。SDK 来源与模型归属见 [THIRD_PARTY.md](../THIRD_PARTY.md)。

锁定目标是 Linux x86_64、Python 3.12、PyTorch 2.11.0 / CUDA 13.0、Qwen-ASR 0.0.6 与 Transformers 4.57.6。其他 Python、平台或 CUDA 组合未验收。需要兼容 CUDA 13 的 NVIDIA 驱动及足够显存；本次验证硬件和结果见[独立部署记录](../docs/ASR-STANDALONE-VALIDATION.md)。

## 安装

在项目根目录执行，先准备 Python 3.12 与 uv。目标必须是新目录，避免同步依赖时影响其他环境：

```bash
# 如已存在，先检查已有环境；不要覆盖线上模型环境。
test ! -e runtime/qwen-asr-standalone || exit 1
uv venv --python python3.12 runtime/qwen-asr-standalone
uv pip sync --python runtime/qwen-asr-standalone/bin/python \
  --require-hashes asr-server/requirements.lock
uv pip check --python runtime/qwen-asr-standalone/bin/python
runtime/qwen-asr-standalone/bin/python -m unittest test_asr_server -v
```

不启用 `--system-site-packages`。`requirements.in` 固定核心直接依赖，`requirements.lock` 包含正常解析的完整依赖和包摘要；没有使用 `--no-deps` 跳过 SDK 依赖。Gradio、AV 等虽然不用于本 worker 的 HTTP 入口，仍按 SDK 依赖声明安装，以便依赖检查真实通过。安装可能编译 sox 的 Python 封装；依赖锁不锁定操作系统编译工具或 NVIDIA 驱动。

重新解析锁文件会产生新的部署组合，必须另行验证；不能只修改版本号沿用旧验收：

```bash
uv pip compile asr-server/requirements.in --python python3.12 \
  --python-platform x86_64-unknown-linux-gnu --generate-hashes \
  --no-header --no-annotate --output-file asr-server/requirements.lock
```

## 模型与启动

自行从 Qwen3-ASR-0.6B 发布方取得有权使用的完整模型目录，包括权重、配置和 tokenizer 文件。源码与依赖安装不会自动下载模型。使用本地模型目录，启动时启用离线加载：

```bash
env -u PYTHONPATH -u PYTHONHOME -u LD_LIBRARY_PATH -u VIRTUAL_ENV -u CONDA_PREFIX \
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONNOUSERSITE=1 \
  runtime/qwen-asr-standalone/bin/python asr_server.py \
  --engine qwen --model /path/to/Qwen3-ASR-0.6B \
  --model-name Qwen3-ASR-0.6B --port 18315
```

服务只监听 localhost，初始化和预热完成后才暴露 `/health`。如果该端口正在使用，为独立验证选择其他空闲端口，勿停止已有服务。Ctrl+C 终止此进程。应用设置 `PIPECAT_ASR_URL` 指向所选端口。

`asr_ctl.py` 保留原演示机器的环境目录约定，不负责本独立环境的启动；以上前台命令是此部署方式的入口。

使用[固定输入采集器](../docs/ASR-REQUEST-CAPTURE.md)验证真实推理，另在应用 `.venv` 内运行其命令；不要把模型依赖安装到应用环境。完整 WAV 转写仍为话轮式 ASR，流式 LLM/TTS 不受此安装说明影响。
