# ASCEND 真人中英混合语音评测计划

**Goal:** 为当前 Qwen3-ASR-1.7B 补充公开真人语音证据，检查既有领域术语提示在通用对话中的收益与退化。

**Architecture:** 复用 `evaluation/asr_capture.py` 与 `evaluation/asr_scoring.py`，只增加评测证据和文档。固定数据版本、选样与提示词后才运行推理；不切换模型、不停止其他服务、不用测试集调参。

**Tech Stack:** Python、PyArrow、SoundFile、既有 HTTP 采集和字符评分工具。

## 预先固定的协议

- 来源：CAiRE/ASCEND，test split，revision `737e9800ae31be9932ba8464c80366559bd28424`。
- 文件 `main/test-00000-of-00001.parquet`，105756434 字节，SHA256 `a4c81d2b5ed6124f052089a695972808c16e0ce0c365ec9773c5d1a8fcf043a7`。
- 仅选 language=mixed、参考含汉字和 ASCII 字母、无 `[UNK]`（忽略大小写）、时长大于零且不超过 30 秒的条目。音频解码必须为 16kHz 单声道；任何不符立即停止并记录，不能静默替换样本。
- 按 `sha256(('voxastage-ascend-v1:' + id).encode('utf-8'))` 升序取前 60 条，ID 为次级排序键。60 条是不同语句，不表示说话人或统计意义上的独立样本。
- 分为三批，每批 20 条。每批两个条件、每条件一次预热、正式各一次，合计 120 次正式请求、6 次预热。保留失败，不自动重试。
- plain context 为空；terms 固定为既有列表 `agent, Pipecat, MiniCPM-o, DINet, FlashHead, StreamingTalker, VAD, ASR, WebRTC, Python`。不声称这是当前应用实际配置，除非另有运行配置证据。
- 复用采集器交错条件顺序；timeout=30 秒；expected-model=Qwen3-ASR-1.7B。每次请求前检查应用没有活动会话及 ASR 不忙，否则停止本轮，保留已完成记录。
- 数据集作者的人工转写用作 human_transcript；`transcript_reviewed=true` 表示上游人工标注，绝不表示本项目逐条复听验收。
- 报告 NFKC、casefold、去空白/Unicode 标点后的字符编辑距离，按参考字符数加权。英文按字符，不是 WER 或论文 MER。不作跨论文排名或 SOTA 声明。
- 公开数据标明 ASCEND 作者、版本、CC-BY-SA-4.0 及变换；音频留在本地。若发布转写衍生文件，独立标明相同许可，不归入代码 MIT 许可。
- 本实验不能排除模型预训练包含 ASCEND；测试 split 不等于模型从未见过。不能替代用户麦克风、噪声、远场、人工音画或独立主机验收。

## 执行清单

- [x] 隔离工作区，既有 ASR 测试 22 项通过；首次模块路径调用不匹配，改用仓库既有 discover 方式后通过，未修改代码。
- [ ] 下载固定文件并校验完整哈希；保存来源和工具版本。
- [ ] 生成选样清单、音频哈希与三个输入 manifest；推理前冻结全部文件和协议摘要。
- [ ] 在现有服务运行有限采集，记录失败、配置边界与结束状态。
- [ ] 合并正式样本，离线复算分数，检查变好/变差/不变样本数及说话人/话题覆盖。
- [ ] 整理报告、许可说明和复现材料，执行脱敏、链接和评分复核，再提交审查。
