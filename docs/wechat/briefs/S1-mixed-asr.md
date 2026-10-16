# S1 · “语音 agent”为什么总被听错？中英混合识别的排查与评测

状态：**选题简报，不是已发表文章**。储备：可写固定输入实验与局限。

目标篇幅：2500–3500字。核心论点：术语提示能否改善识别，需要区分输入真实性和领域词偏置。

## 写作结构

1. 呈现一个真实记录的术语混淆。
2. 区分VAD切分、采样率和模型识别。
3. 解释固定术语对照设计。
4. 报告合成提示词差异率与真人CER区别。
5. 规划未见术语、噪声和真人样本验证。

## 配图与素材

- 音频输入排查图。
- plain/terms文本结果与来源表。

## 发布门槛

复算公开文本分数；如要讨论真人识别率，先获得授权录音与人工参考。

必须保留的边界：合成提示词不是人工听写参考；术语集与样本重合不能声称泛化。

## 已有来源

- [ASR-LIVE-BASELINE.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/ASR-LIVE-BASELINE.md)
- [ASR-EVIDENCE-BASELINE.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/ASR-EVIDENCE-BASELINE.md)
- [MINICPM-SPEECH-CONTEXT.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/MINICPM-SPEECH-CONTEXT.md)

[返回系列目录](../README.md)
