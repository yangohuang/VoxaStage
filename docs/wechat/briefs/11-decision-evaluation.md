# 11 · 规则、LLM、JEV：谁更适合做实时交互决策？

状态：**选题简报，不是已发表文章**。待独立实验；代表作。

目标篇幅：3200–4200字。核心论点：决策更快是否改善整个任务，必须在同一任务与输入上验证。

## 写作结构

1. 选择明确的有限决策任务并预注册候选与标注规则。
2. 按独立样本划分开发和测试，固定规则与提示。
3. 统一输入比较规则、本地LLM及JEV。
4. 报告混淆矩阵、误触发、P50/P95与调用成本。
5. 加入低置信度回退、故障与任务级成功率分析。

## 配图与素材

- 输入与基线公平性图。
- 质量/延迟/成本对照图。
- 失败样本与回退轨迹图。

## 发布门槛

获取可用API与独立标注数据；预提交协议并完整保留失败；未完成前不写实验结论、不填预测成绩。

必须保留的边界：不能用模型单次耗时代替用户体验；不能根据测试集修改阈值再报同集成绩。

## 已有来源

- [redoc](https://api.typesafe.ai/redoc)
- [STREAMING-FUNCTIONS.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/STREAMING-FUNCTIONS.md)
- [ACCEPTANCE.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/ACCEPTANCE.md)

[返回系列目录](../README.md)
