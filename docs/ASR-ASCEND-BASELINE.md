# 真人中英混合 ASR 基线

使用 ASCEND 官方人工转写测试集的固定 60 条中英混合语音，对现有 Qwen3-ASR-1.7B 比较无提示词与既有技术术语提示。120 次正式请求及 6 次预热均成功。术语提示只减少了 4 个字符编辑，同时也有退化案例；本实验不足以支持“中英混合识别已解决”或更换日常配置。

| 条件 | 成功正式请求 | 规范化 CER | 完全匹配语句 | 请求耗时中位数 | 请求耗时 P95 |
| --- | --- | --- | --- | --- | --- |
| plain：空 context | 60/60 | 142/1505 = 9.44% | 24/60 | 0.158 秒 | 0.307 秒 |
| terms：既有术语列表 | 60/60 | 138/1505 = 9.17% | 26/60 | 0.156 秒 | 0.283 秒 |

逐句比较为 4 条编辑数减少、2 条增加、54 条不变，净差 0.27 个百分点。只有一轮，不报告显著性或声称稳定加速。每个条件时延样本数 60，P95 为 nearest-rank；计时从完整 WAV HTTP 请求开始，到完整响应接收完毕，不含用户说话时长、VAD 等待、后续模型/语音合成及物理播放。

## 数据和预先固定的选择

[ASCEND](https://huggingface.co/datasets/CAiRE/ASCEND) 是公开的真人中文/英语混合对话数据。论文说明转写由人工标注；本项目直接使用上游参考，未自行逐条听写。`human_transcript` 与 `transcript_reviewed=true` 在本次记录中指上游人工标注，不表示 VoxaStage 的人工验收已完成。[来源、作者与 CC-BY-SA-4.0 许可](benchmarks/2026-10-16-ascend/LICENSE.md)单独保留。

- 固定 revision `737e9800ae31be9932ba8464c80366559bd28424`，test 共 1315 条。
- 排除 942 条非 mixed 和 15 条包含 `[UNK]` 的记录；其余 358 条均满足汉字/ASCII 字母和时长条件。
- 按 `sha256('voxastage-ascend-v1:' + id)` 升序取前 60 条，无结果导向的替换。抽样规则、提示词和全部音频摘要在推理前冻结，见[冻结记录](benchmarks/2026-10-16-ascend/frozen.json)和[原始协议](benchmarks/2026-10-16-ascend/frozen-plan.md)。文件按采集日期 10 月 16 日命名，报告整理与审查在 10 月 17 日继续；机器时间记录使用 UTC。
- 共 235.300 秒，只有两位说话人：ID 3 的 32 条、ID 17 的 28 条。话题为 persona 23、technology 21、sports 8、education 8。60 条不同语句不等于 60 个独立说话人，不能据此推断人群泛化。
- 读取固定 Parquet 内的音频，16kHz 单声道解码为 int16，写为 PCM16 WAV。无重采样、裁剪、降噪或增益变化；原始编码、PCM、WAV 三层哈希均见[选择记录](benchmarks/2026-10-16-ascend/selection.json)。

terms 与此前合成输入实验使用同一列表，未根据 ASCEND 转写调整：

```text
agent, Pipecat, MiniCPM-o, DINet, FlashHead, StreamingTalker, VAD, ASR, WebRTC, Python
```

这是待比较的固定条件，不声称它就是应用当前的运行配置。不同输入集的 CER 与此前合成提示词差异率不能直接相减，当成模型质量变化。

## 改善与退化都保留

以下是与上游参考的文本差异，不是本项目听音后的纠错结论：

| ASCEND ID | 参考中的关键部分 | plain | terms | 字符编辑数变化 |
| --- | --- | --- | --- | --- |
| 00332 | interesting part | 丢失 part，并多出“的” | 与参考规范化后相同 | 4 → 0 |
| 00999 | 就是 hard choice | It's a hard choice. | 就是 hard choice. | 4 → 0 |
| 00722 | McGrady | McGrady | McGregor | 0 → 4 |
| 01166 | 还有呃 so | Have, so. | Have uh so. | 4 → 6 |

其余两个改善分别是中文同音字与长句差异。提示词影响并不限于列表里的词，也不能仅凭一次运行确定每项差异的稳定性。当前更值得调查的是英文实体、短句语言切换和重复/语气词；后续改进应另取开发集，保持本次测试结果不变，再用新的独立输入复验。

## 协议和模型边界

复用现有采集器，三批各 20 条，每批每个条件各预热一次，再按样本索引交错条件顺序；rounds=1，timeout=30 秒。失败不会自动重试。每次请求前检查日常应用无会话且 ASR 空闲；这是准入检查，不是排他资源锁。本次所有请求完成，结束时应用和 ASR 都就绪、空闲。未停止 OpenJev、重启模型或改变日常服务配置。

服务自报 Qwen3-ASR-1.7B、CUDA、bfloat16；请求协议没有绑定完整权重、tokenizer、解码配置，模型 revision 没有由此次 health 返回，保留 `model_configuration_complete=false`。未验证模型训练是否包含 ASCEND；官方 test split 不证明模型从未见过这些语音。

评分复用 `asr_scoring.py`：NFKC、casefold，去空白和 Unicode 标点，按参考字符总数加权。英文也按字符计算，不是英文 WER 或 ASCEND 论文的 MER；不用于跨论文排名。原始转写的语气词、重复、同音字或标注差异会影响分数，未根据模型输出修改参考。

## 复现

全部正式文本记录、三批完整结果及汇总位于[证据目录](benchmarks/2026-10-16-ascend/LICENSE.md)。每批公开结果仅移除本机 endpoint，保留原文件 SHA256；没有上传音频或机器配置。

在仓库根目录可直接离线复算：

```bash
python evaluation/asr_scoring.py \
  docs/benchmarks/2026-10-16-ascend/scoring-input.json \
  --output /tmp/ascend-scores.json
python - <<'PY'
import json
from pathlib import Path
actual = json.loads(Path('/tmp/ascend-scores.json').read_text())
expected = json.loads(Path('docs/benchmarks/2026-10-16-ascend/scores.json').read_text())
assert actual == expected
print('Scoring reproduced')
PY
```

若需重建音频输入，先按[下载记录](benchmarks/2026-10-16-ascend/download.json)取得固定文件；准备脚本会校验完整 SHA256。使用独立的数据准备环境，实测 PyArrow 24.0.0、SoundFile 0.13.1、libsndfile 1.2.2，完整工具信息见选择记录。

```bash
python docs/benchmarks/2026-10-16-ascend/prepare.py \
  /path/to/test.parquet /tmp/ascend-inputs
```

输出目录必须不存在。脚本重建三份 manifest、WAV、术语条件和选择记录；64 个内容输入文件（60 WAV、3 manifest、1 variants）的字节哈希应与冻结记录一致。`selection.json` 中 Python/底层库版本可能因环境不同而变化，应核对实际版本，不要覆盖历史记录。

将兼容本地 ASR 端口代入下列命令，依次运行 n=1、2、3。应用空闲和 ASR ready/idle 应在每次请求前检查；本次自动执行使用采集器 observer 完成，下面 CLI 仅检查开始状态，不是相同的逐请求准入保护。不要在有用户会话时重跑。

```bash
python evaluation/asr_capture.py \
  --manifest /tmp/ascend-inputs/inputs-1.json \
  --variants /tmp/ascend-inputs/variants.json \
  --output /tmp/ascend-run-1 \
  --url http://127.0.0.1:18413 \
  --expected-model Qwen3-ASR-1.7B --rounds 1 --timeout 30
```

## 仍未验收

这次新增了公开真人中英混合语音及上游人工参考证据，但没有覆盖实际用户麦克风、噪声/远场、更多说话人、连续流式输入、数字人口型和主观音质。它补充[阶段验收索引](ACCEPTANCE.md)，不关闭物理设备或人工音画质量门槛。
