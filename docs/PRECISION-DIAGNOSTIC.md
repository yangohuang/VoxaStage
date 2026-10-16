# 本地权重核验与精度诊断准备

2026-10-16准备阶段记录。**在本准备检查点，精度对照尚未运行，没有NF4／BF16质量或性能结论。** 本轮完成权重来源核对、预注册问题和采集代码验证；真实显存准入检查拒绝加载模型。该阶段日常服务及其他GPU任务保持原样。

后续获得临时暂停另一GPU服务、测试后恢复的明确授权，已完成56条正式生成并恢复服务。实际结果见[同权重精度对照](PRECISION-COMPARISON.md)；下文保留准备阶段的原始状态与资源拒绝证据。

## 已核实的事实

通过Qwen官方仓库API取得固定revision `1cfa9a7208912126459214e8b04321603b3df60c`。本地三个权重分片、分片索引、config、generation_config、tokenizer、tokenizer_config、merges、vocab共10个核心文件的SHA256与该版本一致。大文件对照上游LFS SHA256，小文件从固定revision下载后计算SHA256；未下载权重、未上传本地文件。

完整对应关系见[来源核对JSON](benchmarks/2026-10-16-qwen3-file-provenance.json)，上游为[Qwen3-4B固定版本文件](https://huggingface.co/Qwen/Qwen3-4B/tree/1cfa9a7208912126459214e8b04321603b3df60c)。额外的本地configuration.json不参与本实验Transformers加载路径，未将它记为官方核验通过。

这只能排除所核验文件与该上游版本不一致，不能证明运行时数值计算、采样或模型回答必然正确。[上一轮](THINKING-CONTEXT-ABLATION.md)已经发现，修改项目背景或启用思考没有稳定解决全部关键错误；接下来单独检验精度影响。

## 冻结的对照

[输入与参考](benchmarks/2026-10-16-precision-input.json)及[计划](plans/2026-10-16-precision-check.md)在运行前提交于 `5d4c5c4`。

- 12道新编诊断题覆盖分位数取舍、证据不足、权重存储算术、目标更新、迟到结果、未执行动作、过期画面、引用指令和英文术语保持；另保留2道历史锚点。
- 新题根据已知失败类型设计，不是独立盲测或公开基准。参考答案用于检查推理和事实边界，不能拿文字复述题冒充真实中英混合ASR评测。
- 相同中性SYSTEM、权重、tokenizer、模板、SDPA和非思考采样；每精度14题×2seed=28条，两精度共56条，另各一次预热。
- 唯一研究干预是NF4 double quant／BF16 compute与无量化BF16；量化也会改变计算内核，结果不能细分归因某个算子。顺序运行和后台负载限制性能比较。
- 不与生产SYSTEM下的旧成绩直接作精度归因；参考文本不会作为模型输入。rubric和额外错误分别保留。

新采集器是上一轮已审查代码的独立冻结副本，保留旧实验源码和哈希。NF4明确传入量化配置，BF16不传该配置；两者都加载到CUDA，不以CPU卸载规避资源要求。既有流式token记录、最终回答分离、占用停止和原子保存逻辑保留。

## 资源准入结果

准备完成后，设备剩余4892MiB，而预注册门槛为NF4的8192MiB、BF16的11264MiB。另一个独立GPU服务仍运行；未为实验终止它，也未重启日常服务。[资源检查](benchmarks/2026-10-16-precision-resource-check.json)保留当时日常应用ready、active_sessions=0、模型busy=false。

实际调用BF16采集器验证准入：CUDA初始化后可用显存4719050752字节，仍低于11811160064字节门槛，抛出 `Insufficient free memory for isolated experiment` 并以退出码1结束。[原始准入记录](benchmarks/2026-10-16-precision-bf16-admission.json)中的28条均为pending，warmup为null，尚未进入权重加载或生成。该记录不作为模型语义失败，也不计入正式56条结果。

即使首次准入通过，计算权重哈希和初始化tokenizer之后、加载模型之前，还会复查服务占用和空闲显存，避免检查期间有其他任务启动。检查不能保证整段运行完全没有资源竞争，运行中的日常会话保护仍保留。

## 已完成验证与执行入口

6项新CPU测试通过：实际fake runner分别执行NF4与BF16的28条矩阵，检查传给loader的精度参数和共同采样参数；低显存时不加载，初始化检查后12→9GiB时也拒绝加载；模型中途报错保留已生成token和剩余pending记录。独立审查另外重跑12项旧采集器测试，并确认旧采集器与主分支基线逐字节一致。

这些检查验证采集契约，不是GPU推理成绩。使用项目LLM独立环境、已有本地权重，待资源满足后按顺序执行，两份结果文件必须不存在：

```bash
python docs/benchmarks/test_precision_capture.py
python docs/benchmarks/2026-10-16-run-precision-check.py \
  --precision nf4 --model "$LOCAL_MODEL_DIR" --output /tmp/precision-nf4-new.json
python docs/benchmarks/2026-10-16-run-precision-check.py \
  --precision bf16 --model "$LOCAL_MODEL_DIR" --output /tmp/precision-bf16-new.json
```

每题前与生成中检查日常服务，生成上限90秒；发现占用即停止，保留原文件。若后续出现中断，先声明恢复和结果选择规则，再继续，不按答案正确与否挑选。正式完成后需逐条审查并报告最终回答延迟、截断及显存，再决定是否有理由更改日常服务。

原目标中的[人工音画、物理设备与独立GPU完整部署验收](ACCEPTANCE.md)仍待完成。本记录交付的是已验证的准备工作，不关闭这些门槛。
