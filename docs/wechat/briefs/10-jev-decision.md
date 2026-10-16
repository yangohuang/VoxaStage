# 10 · 语音 Agent 的每一个判断，都需要大模型生成一段话吗？

状态：**选题简报，不是已发表文章**。可写原理和候选设计；接入未确认。

目标篇幅：2500–3200字。核心论点：有限候选的决策可以与自然语言生成、实际执行分离。

## 写作结构

1. 从继续讲解还是处理用户插话的问题切入。
2. 依据官方文档解释状态、问题、候选与概率。
3. 划分JEV决策、LLM生成和程序执行职责。
4. 提出工具路由、打断和动作选择三个候选点。
5. 讨论阈值、回退、延迟预算及本地优先的边界。

## 配图与素材

- 生成/决策/执行职责图。
- 候选决策请求与返回示意图。
- 置信度回退示意图。

## 发布门槛

发布前重新核对官方可用模型、服务方式和条款；本地默认路线保留，云API只作明确标识的可选对照。

必须保留的边界：接口类型受约束不保证判断正确；官方宣传不当作本项目性能证据。

## 已有来源

- [redoc](https://api.typesafe.ai/redoc)
- [introducing-system-one-models-and-jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- [STREAMING-FUNCTIONS.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/STREAMING-FUNCTIONS.md)

[返回系列目录](../README.md)
