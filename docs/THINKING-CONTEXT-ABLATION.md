# 项目事实提示与思考模式对照

2026-10-16。本轮比较当前本地Qwen3-4B在固定采样参数下，项目背景提示与思考模式对最终回答质量和等待时间的影响。它延续[上一轮模型与解码对照](MODEL-DECODING-COMPARISON.md)，只使用八道既有开发题，不构成公开基准或独立盲测。**未切换日常模型、SYSTEM或思考策略。**

## 预先固定的设计

输入与逐题rubric在运行前提交于 `743b83c`，采集器扩展提交于 `1682033`。四条件为 production／general SYSTEM × direct／thinking。production是原应用SYSTEM；general仅移除项目事实段，保留基础语音助手规则和末尾防编造规则。移除同时改变内容和长度，不能进一步归因到某个词或模型模块。

| 项目 | 固定配置 |
| --- | --- |
| 权重与推理 | 同一本地Qwen3-4B；NF4 double quant、BF16 compute、SDPA |
| 条件 | production-direct、production-thinking、general-direct、general-thinking |
| 采样 | temperature0.7、top_p0.8、top_k20、min_p0、repetition_penalty1；全部do_sample=True |
| 预算 | 每请求1024生成token，包含思考和最终回答 |
| 样本 | 八道既有开发题 × 四条件 × seed17/43，共64条；另一次非思考预热 |
| 顺序 | 每题交替条件方向，seed顺序轮换；不是随机化顺序实验 |

统一采样参数用于控制变量，不宣称每种模式的最优配置。两seed不是两个独立问题，思考与直接回答也共享生成预算；结论只适用于这套配置。

## 采集与中断记录

首批完成40条后，日常LLM出现占用；第41条在生成53个token后被 `production_model_busy` 保护停止，剩余23条保持pending，进程退出码1。该条没有闭合思考或最终回答。这是外部占用导致的采集中断，不能计为模型语义失败或删去后伪装成完整批次。

再次确认日常服务空闲后，用新文件重跑原矩阵。第二批完成54条，第55条又在生成100个token后因同一占用保护中断，剩余9条pending，进程退出码1。该失败条目虽然已出现部分最终回答，也不参与语义评分。

随后在补跑前提交 `3d91316`：只补跑最后两道题的完整16条组合，另一次预热，正常退出码0。主分析由第二批前六道完整题48条和补跑两题16条组成，明确为**分段执行的64条**，没有一次完整无中断的64条运行。两个来源的权重、模板、环境和生成配置一致，末两题的条件及seed顺序与原矩阵一致。

实际留下110条完整非预热生成、2条中断生成和3次预热。第一批40条与第二批对应输出token40/40相同；第二批末题6条与补跑对应输出token6/6相同。46条完整重复结果不进入64条主分析，全部保留，没有根据正确与否挑选。来源选择规则在补跑前确定，汇总记录每条答案来自哪个源文件、哪个行索引及源文件SHA256。

采集器保留实际输入／输出token、完整流式文本、最终decode、权重与配置哈希。开启思考必须出现 `</think>` 后才提取最终回答，未闭合思考或标记后只有空白均记无最终回答。真实Qwen3 tokenizer验证该标记不会被 `skip_special_tokens=True` 删除。关闭思考却输出思考标记时，也分离并标记unexpected_thinking。

`first_final_text_s` 是首个非空白最终回答字符到达采集器的耗时，从模型生成开始计时；不是完整首句、首token、物理首声或端到端延迟。无最终回答的请求单列，不能从失败分母中消失。有最终回答样本的P95采用nearest-rank，每条件最多16条，因此本实验P95等于有效样本最大值，不能当成稳定的总体长尾估计。

## 结果

64条选入记录全部EOS结束、均有最终回答；未出现长度截断、未闭合思考或直接模式意外思考。协议完成不代表质量通过。

1. **思考改善了给定P95题的选择方向。** 关闭思考四条均反向选择尾延迟更高的级联，开启思考四条均选择较低P95的端到端。但其中仍有漏报P50取舍、把P95当最大值、把两条路径错说成不同层框架等问题。选择方向正确不等于整条回答正确。
2. **移除项目事实没有稳定修复历史问题。** 排障题仍未给出完整的输入、指令冲突、独立模型测试闭环；24GB约束题仍未建立质量测量。思考条件还出现量化把7B参数变为4B、INT8的70B权重放入24GB、未经测量的准确率及提速百分比等额外错误。
3. **证据不足回答仍会夹带幻觉。** 有的回答拒绝给音画偏差数值，却编造“未启用同步功能”或“未公开内部参数”；general-direct seed43推荐ISO 22241作为音画评估标准。核对[ISO官方说明](https://www.iso.org/standard/66408.html)，该标准涉及柴油发动机NOx还原剂AUS32，与所问音画同步无关。
4. **目标纠正保持可用，时序边界仍失败。** 目标纠正八条均保留松鼠、450元；视觉时序题八条均没有明确说明12秒记录无法保证13秒的实时状态。部分思考回答反而把该推断直接称为“实时事实”。
5. **打断建议存在不安全的语义迁移。** production-thinking seed17建议将旧文本保存到缓冲区，待新流程启动继续处理；若照此实现，可能把已取消结果带入新话轮。这里仅评估模型的文字建议，未改变应用现有取消机制，也不代表线上已发生该错误。

逐题rubric与额外错误分别保留，不汇成能掩盖关键失败的总分。独立审查由助手执行，不是真人盲评；八题均是已反复使用的开发题，不能据此估计总体准确率。

## 最终回答等待时间

| 条件 | 有最终回答／请求 | 最终回答首字中位数 | 最终回答首字P95 | 原始文本首字中位数 | 生成总耗时中位数 |
| --- | --- | --- | --- | --- | --- |
| production-direct | 16/16 | 0.052s | 0.127s | 0.052s | 1.578s |
| production-thinking | 16/16 | 6.532s | 10.761s | 0.074s | 8.636s |
| general-direct | 16/16 | 0.047s | 0.087s | 0.047s | 1.400s |
| general-thinking | 16/16 | 5.536s | 12.201s | 0.066s | 7.184s |

所有条件均无最终回答缺失；这里的延迟分母均为16。开启思考仍很快输出原始文本，但用户实际答案要等思考结束，不能用约0.07秒的思考首字声称低延迟语音回复。各条件观察到的进程显存峰值均为3282MiB，是约1秒间隔及生成前后采样峰值，可能漏掉瞬时峰值；不代表整套语音系统显存。

这是同一RTX4090上的当前实现测量，分段运行且问题重复；未控制全部后台负载，不能外推上游最优推理性能。原始报告记录torch2.11.0、transformers5.12.1、bitsandbytes0.50.2、accelerate1.14.0与文件哈希。

## 部署决定

不将全局思考模式或删减SYSTEM直接用于日常语音。当前配置的思考等待明显增加，关键排障、约束和证据边界问题仍未解决；单题选择方向改善不足以通过预设质量门槛。

后续应先用独立问题与强参考回答检查当前本地推理的基础能力，必要时分离量化与推理实现的影响，避免继续在同八道开发题上调提示。若将来采用选择性推理或先反馈状态再处理慢任务，需要另行验证路由正确性、取消和实际首声延迟；本轮没有实现或验证这种策略。

## 验证与复现

12项采集器测试通过，包括真实runner的旧／新清单配置传递、生产者异常后保存token及pending条目；独立审查验证真实tokenizer标记和汇总脚本的空答案分母。它们验证采集契约，不代表模型语义正确。

公开拷贝的分段汇总已逐字段复算一致，64条审查的来源哈希、顺序和rubric长度匹配。汇总负例覆盖错序、环境变化、选入失败记录、负时间及中断结构变化，均被拒绝。独立代码、语义和报告审查通过；源码导出376个文件的逐文件哈希与UTF-8文本脱敏通过，既有二进制图片未另做OCR。实验进程已退出，日常应用ready且无活跃会话、LLM空闲，原模型及加载状态保持不变。

```bash
python docs/benchmarks/test_model_decoding_capture.py
python docs/benchmarks/2026-10-16-run-model-decoding.py \
  --manifest docs/benchmarks/2026-10-16-thinking-context-input.json \
  --model "$LOCAL_MODEL_DIR" --output /tmp/thinking-context-new.json
python docs/benchmarks/2026-10-16-summarize-thinking-context.py \
  /tmp/thinking-context-new.json
# 复算本次分段运行结果：
python docs/benchmarks/2026-10-16-summarize-thinking-context.py \
  docs/benchmarks/2026-10-16-thinking-context-interrupted-second.json \
  --completion docs/benchmarks/2026-10-16-thinking-context-completion.json
```

使用项目独立LLM环境和已有本地权重，输出文件必须不存在。开始前要求8GiB可用显存，每条生成前及运行中检查应用与日常LLM空闲，生成上限90秒。汇总在相同采集器源码版本运行，以便核验其SHA256。结果不经过ASR、TTS、工具或数字人；人工质量、物理设备及独立空白GPU部署仍按[原验收要求](ACCEPTANCE.md)保持待完成。


证据：[计划与中断修订](plans/2026-10-16-thinking-context-check.md)、[原输入与rubric](benchmarks/2026-10-16-thinking-context-input.json)、[补跑输入](benchmarks/2026-10-16-thinking-context-completion-input.json)、[第一次中断](benchmarks/2026-10-16-thinking-context-interrupted-first.json)、[第二次中断](benchmarks/2026-10-16-thinking-context-interrupted-second.json)、[补跑结果](benchmarks/2026-10-16-thinking-context-completion.json)、[来源与测量汇总](benchmarks/2026-10-16-thinking-context-measurements.json)、[第一次中断批次审查](benchmarks/2026-10-16-thinking-context-interrupted-review.json)。[最终64条逐项审查](benchmarks/2026-10-16-thinking-context-review.json)。
