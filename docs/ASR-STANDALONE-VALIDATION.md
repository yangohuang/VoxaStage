# Qwen ASR 独立部署验证

2026-10-10。在同一台已有 NVIDIA 驱动的 Linux x86_64 主机上，从新的 Python 3.12 虚拟环境安装完整依赖、加载已有 Qwen3-ASR-0.6B 权重并完成真实请求。该结果验证 ASR 不再需要继承演示机器的共享 Conda 包；不是空白 GPU 主机或全部模型安装验收。

## 结果

| 检查 | 实测结果 |
| --- | --- |
| 依赖安装 | 111 个包按完整 SHA256 锁安装，全部安装版本逐项与锁一致，`uv pip check` 通过 |
| 环境隔离 | `include-system-site-packages=false`；所有已安装发行包的路径都位于新虚拟环境内；不继承 PYTHONPATH/PYTHONHOME/LD_LIBRARY_PATH，禁用用户 site-packages |
| 核心导入 | torch 2.11.0+cu130、transformers 4.57.6、Qwen3ASRModel 导入通过 |
| 协议测试 | 原有 3 项 ASR 测试通过，覆盖格式/静音、忙碌/异常恢复和术语传递 |
| 真实推理 | 15 条固定输入 × 2 个术语配置 × 3 轮，共 90 次正式请求，加 2 次预热，92/92 成功 |
| 输出对照 | 92 条文本与先前部署逐条相同，输入和 context 哈希逐条匹配；未据此宣称速度改善 |
| 权重完整性 | 完整 9 文件模型目录在加载前与验证后哈希一致；权重文件还与既有固定 SHA256 核对 |
| 清理 | 本轮独立端口的临时 worker 已退出；未停止原 ASR 服务 |

硬件为 RTX 4090，驱动 595.71.05；Python 3.12.13、uv 0.11.31。虚拟环境不复制既有 Python 包，但复用主机的 Python 基础解释器、标准库、系统动态库及驱动。权重也来自已有本地目录，没有重新下载或验证模型发布渠道的获取过程。

## 质量与证据边界

与[固定输入术语对照](ASR-LIVE-BASELINE.md)相同，plain 的合成提示词字符差异率为 9/804（1.12%），terms 为 0/804。只有一个缩写样本发生规范化差异，三轮重复不增加独立样本数。全零静音由 worker 直接返回空文本，不是模型静音识别成绩。

该结果支持当前独立环境的功能复现，不能证明真人中英混合准确率、声学端到端延迟、跨硬件一致性或更高性能。两次部署的间接依赖不完全相同，不能当作只改变某个包的因果消融。模型文件清单描述本地使用的字节，不等于独立核验了完整上游 revision；采集协议仍保留 `model_configuration_complete=false`。

- [部署机器记录](benchmarks/2026-10-10-asr-standalone-validation.json)：111 个包版本、模型前后哈希、源码/锁文件哈希、测试范围及清理状态；去除临时端口和进程号。
- [真实采集](benchmarks/2026-10-10-asr-standalone-capture.json)：92 次请求、实际 WAV 与 context 哈希、成功/失败、耗时及评分；去除本机 endpoint。
- [评分输入](benchmarks/2026-10-10-asr-standalone-input.json)：90 次正式请求，支持离线复算。音频转换来源沿用[前次来源记录](benchmarks/2026-10-10-asr-live-provenance.json)。

原始音频、权重、运行日志和本机配置不公开；这些 JSON 支持文本分数复算，不能独立重建原始音频或完成权重安装。原始报告摘要保留于 publication 字段。

## 复现

新环境安装、启动命令与完整依赖锁见 [ASR 部署说明](../asr-server/README.md)。安装完成后可在另一个空闲本机端口按[采集器说明](ASR-REQUEST-CAPTURE.md)运行相同清单；本次参数 rounds=3、timeout=30，模型名为 Qwen3-ASR-0.6B。

```bash
python evaluation/asr_scoring.py \
  docs/benchmarks/2026-10-10-asr-standalone-input.json \
  --output /tmp/asr-standalone-scores.json
python - <<'PYVERIFY'
import json
from pathlib import Path
actual = json.loads(Path('/tmp/asr-standalone-scores.json').read_text())
expected = json.loads(Path('docs/benchmarks/2026-10-10-asr-standalone-capture.json').read_text())['scoring']
assert actual == expected
print('Scoring reproduced')
PYVERIFY
```

## 保留的问题

首次协议测试在受限执行环境内未返回；停止该测试进程后，在获准环境中同一测试通过。初次依赖解析早于输入文件创建而失败；初次虚拟环境创建受到默认缓存目录只读限制，改用可写缓存后成功。这些准备阶段失败不计入 92 次模型请求。

首次 SDK 导入出现 nagisa 的 Python 3.12 无效转义 SyntaxWarning；没有修改第三方包，导入与真实推理仍通过。安装锁覆盖 Python 包及其分发摘要，不锁定操作系统、驱动、源码包的构建工具或编译结果。

本轮不切换日常服务，也未替代人工试听、口型质量和物理麦克风验收。后续仍需逐个补齐其他模型的独立环境，并在真正独立 GPU 主机验证完整部署。
