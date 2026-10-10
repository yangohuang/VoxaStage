# ASR 固定输入采集

`evaluation/asr_capture.py` 对现有本地 ASR 的 `/health` 和 `/transcribe` 接口采集证据。它冻结并校验实际发送的 WAV 字节，在同一模型上交错运行术语配置，输出可由[离线评分器](ASR-EVIDENCE-BASELINE.md)复算的记录。

该工具使用完整 WAV 请求，测量请求发出到接收并校验完整结果的耗时。它不把现有话轮 ASR 改成流式输入，也不测物理麦克风、浏览器或扬声器延迟。

## 准备输入

把 WAV 和清单放在同一目录。每条 WAV 必须为 16kHz、单声道、PCM16，长度大于 0 且不超过 30 秒。每份清单 1–20 条，文件总路径不能逃出清单目录。

`cases.json` 示例（sha256 需填实际 WAV 文件摘要）：

```json
{
  "version": 1,
  "cases": [{
    "id": "mixed-01",
    "audio": "mixed-01.wav",
    "sha256": "替换为实际文件的64位SHA256",
    "reference": "介绍一下语音 agent 技术。",
    "reference_kind": "synthesis_prompt",
    "transcript_reviewed": false,
    "source": "synthetic"
  }]
}
```

参考文字只用于本地评分，不发送给识别服务。真实录音使用 `source=human`，且须填写实际人工听写后的 `reference_kind=human_transcript` 和 `transcript_reviewed=true`。合成语音也可以由人工听写后使用 human_transcript，但 source 仍为 synthetic；不能把模型自动转写当人工参考。真人和合成语音使用不同清单、分别运行。

语音转写为空时用空字符串；静音的空参考单独统计非空识别输出，不纳入非空参考的字符差异率。

`variants.json` 示例：

```json
{
  "version": 1,
  "variants": [
    {"id": "plain", "context": ""},
    {"id": "terms", "context": "agent, Pipecat, MiniCPM-o, DINet, FlashHead, StreamingTalker, VAD, ASR"}
  ]
}
```

每次 1–8 个配置。context 通过 `x-asr-context` 请求头发送，必须是至多 500 字符的可打印 ASCII，不能有首尾空白。工具不会静默修剪或改写配置。

## 运行

先确认现有 ASR 已启动；采集器不会下载权重、切换模型或重启服务。

```bash
.venv/bin/python evaluation/asr_capture.py \
  --manifest /path/to/asr-inputs/cases.json \
  --variants /path/to/asr-inputs/variants.json \
  --url http://127.0.0.1:18315 \
  --expected-model Qwen3-ASR-1.7B \
  --rounds 3 --timeout 30 \
  --output artifacts/asr-capture/run-01
```

URL 仅支持本机 HTTP，远端服务可使用已经建立的本地隧道。模型名需与健康接口完全一致，不同模型分别运行，不在采集器中自动切换。已知模型修订号可通过 `--model-revision` 声明；如果服务同时报告 revision，则二者必须一致。未声明或未报告版本保留 null，不假装已经固定完整模型版本。

输出目录必须是新目录；重复使用会拒绝，不覆盖旧证据。每个配置先预热一次，随后按轮次和样本编号轮换配置顺序。重复采集同一录音增加的是运行次数，不增加独立录音数量。

## 输出与解释

- `results.json`：运行状态、计划/已执行请求、固定输入和配置摘要、源码摘要、端点、已报告的 device/dtype/threads、模型声明/报告版本、逐请求状态与耗时、正式结果评分。
- `scoring-input.json`：只含正式请求，排除预热；与离线评分器兼容。每个重复轮次有独立 ID，同轮所有配置使用同一参考。提前终止时没有完整评分文件，已完成请求仍在 results.json 中。

`audio_sha256` 来自内存中冻结且实际传给 HTTP 请求的完整 WAV；`pcm_sha256` 单独描述音频样本。校验后即使源文件被改动，已冻结的本轮请求字节也保持不变。清单与配置摘要也绑定同次读取的内容。

`request_wall_s` 是客户端完整请求墙钟耗时；`server_elapsed_s` 是服务器自报的内部识别耗时。不要将它们混为裸模型速度或端到端声学时延；超时可能仍有后台 GPU 计算继续，采集器无法保证停止已提交的服务器任务。若服务之后忙碌，请求失败仍保留，不自动重试或隐藏。

仅开始时核验 ready/idle 和模型名，每条响应再核验模型名和音频时长。这不是服务器级独占锁，也不证明运行时所有模型权重和配置不变。健康接口目前不保证报告完整权重、tokenizer 和解码配置，因此 `model_configuration_complete=false`；声明版本与服务报告均有各自证据边界。

失败请求保留在正式评分的尝试数中；成功返回空字符串则按真实空识别评分。HTTP 失败不保存错误响应正文，超时和格式错误单列。取消会保存 aborted 状态；有任何预热或正式请求失败，CLI 返回非零，不能将部分结果算作完整通过。

本地报告会包含参考文字、转写结果和自定义术语。报告、原始录音及本机端点配置留在 artifacts，不自动进入源码导出。公开报告前单独检查内容来源与个人信息。

## 验证范围

```bash
.venv/bin/python -m unittest discover -s evaluation -p 'test_asr_capture.py'
.venv/bin/python -m unittest discover -s evaluation -p 'test_*.py'
```

协议测试使用注入 HTTP transport，报告显式标记 `execution=injected_transport`。这些测试检查冻结字节、顺序、超时、模型不匹配、失败计数与取消持久化，不产生真实模型质量结论。真实请求的报告标记为 `http_worker`；是否成功须看报告状态，不能仅凭该字段推定服务可用。

2026-10-10：9 项新增采集器测试及全部 31 项 evaluation 测试通过，独立审查与复核通过。原有五类 15 条合成输入已在本地转为 16kHz，并保留转换前后哈希、重采样方法和版本；输入没有人工听写。当前尚未执行真实模型的新一轮采集。
