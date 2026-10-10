# 固定输入 ASR 术语对照（2026-10-10）

在同一 Qwen3-ASR-0.6B 服务上，15 条固定合成输入分别使用空 context（plain）与固定术语列表（terms），交错运行 3 轮。92 次真实 HTTP 请求全部成功：2 次预热、90 次正式测量。重复轮次不增加独立样本数。

| 配置 | 正式请求成功 | 合成提示词字符差异率 | 非静音请求 p50（秒） | 非静音请求 p95（秒） |
| --- | --- | --- | --- | --- |
| plain | 45/45 | 9/804 = 1.12% | 0.150 | 0.225 |
| terms | 45/45 | 0/804 = 0.00% | 0.151 | 0.233 |

每配置的时延统计含 36 次非静音正式请求；p50 为中位数，p95 用 nearest-rank（排序后第 ceil(0.95×n) 个值）。排除预热与数字静音，因为 worker 对全零 PCM 直接返回空文本，没有运行模型解码。每配置 9 次静音请求均返回空文本，这只能验证服务的数字静音分支，不能证明模型能处理环境噪声。

## 实际差异

只有 `mixed-01` 与规范化后的参考有差异；三轮输出一致：

| 输入合成提示词 | plain | terms |
| --- | --- | --- |
| 请解释一下 VAD 和 ASR 的区别。 | 请解释一下 VIT 和 S R 的区别。 | 请解释一下 VAD 和 ASR 的区别。 |

该条规范化后 15 个参考字符，plain 每轮 3 个编辑距离。其余中文、英文、停顿样本及混合样本在忽略标点、空白和大小写后均与参考一致；包括“介绍一下语音 agent 技术”。这些是 ASR 与合成提示词的文本比较，未经人工听写，不称为人工 CER，也不说明每个词都按提示词正确合成。

terms 的完整 context 为：

```text
agent, Pipecat, MiniCPM-o, DINet, FlashHead, StreamingTalker, VAD, ASR, WebRTC, Python
```

术语与样本内容有重合，本组属于已知领域术语的受控对照，不是未见术语泛化测试。没有针对新结果更换音频、提示词或排除失败；全部正式请求均保留。三轮一致不代表对不同说话人具有统计显著性。

## 环境与证据

- 本机 NVIDIA GeForce RTX 4090；服务报告 CUDA / bfloat16，已有 Qwen3-ASR-0.6B 权重；完整权重、tokenizer 和解码配置未被请求协议绑定，`model_configuration_complete=false`。
- 输入中文、英文、中英混合、全零静音、拼接停顿各 3 条。原始 24kHz 输入通过 soxr 1.0.0 HQ 转为 16kHz mono PCM16；[转换来源](benchmarks/2026-10-10-asr-live-provenance.json)记录前后哈希与样本数。
- [完整采集结果](benchmarks/2026-10-10-asr-live-results.json)保留每次请求的输入哈希、术语哈希、顺序、识别文本、客户端墙钟耗时和服务自报耗时，仅移除本机 endpoint。原始报告 SHA256 存于 publication 字段。
- [评分输入](benchmarks/2026-10-10-asr-live-input.json)含全部 90 次正式请求，可离线复算；完整结果内的 scoring 应与复算结果完全一致。
- 原始音频不随公开源码发布，因此公开仓库可复算文本分数，不能单凭这些 JSON 重新执行同一批音频推理。请求源码摘要记录于 results.source_sha256。

```bash
python evaluation/asr_scoring.py \
  docs/benchmarks/2026-10-10-asr-live-input.json \
  --output /tmp/asr-live-scores.json
python - <<'PYVERIFY'
import json
from pathlib import Path
actual = json.loads(Path('/tmp/asr-live-scores.json').read_text())
expected = json.loads(Path('docs/benchmarks/2026-10-10-asr-live-results.json').read_text())['scoring']
assert actual == expected
print('Scoring reproduced')
PYVERIFY
```

采集参数与输入格式见[请求采集说明](ASR-REQUEST-CAPTURE.md)：本次 rounds=3、timeout=30、expected-model=Qwen3-ASR-0.6B；每配置预热一次，正式顺序按轮次和样本索引轮换。

## 结论与下一步

在这组固定输入上，术语提示消除了一个缩写混淆，未观察到其他规范化文本退化；据此保留领域术语作为候选配置。当前结果不足以决定更换线上模型，也没有把完整 WAV 识别变成流式 ASR。

下一步需要独立真人中英混合录音和人工参考，包含术语外词、噪声及停顿，检验术语提示是否引入偏置。数字人口型、主观音质和物理麦克风/扬声器延迟仍未验收；本报告不替代这些目标。
