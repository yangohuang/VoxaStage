# StreamingTalker 独立部署验证

2026-10-10。在已有 NVIDIA 驱动的 Linux x86_64 主机上，从固定上游源码和新的 Python 3.10 虚拟环境重建 StreamingTalker 增量服务。94 个依赖按 SHA256 锁安装，真实网格输出、音频保持与取消恢复验证通过。复用本机已持有的模型和模板，不代表空白 GPU 主机部署或口型质量验收。

## 部署结果

| 检查 | 实测结果 |
| --- | --- |
| 固定源码 | 上游提交 `25b613ac273624947e5fa51c4c41c4933d8147be`；38 个基线文件摘要全部匹配 |
| 本项目扩展 | 应用公开无头补丁，补丁后模型摘要与归属记录一致；7 个 serving 文件与公开 overlay 一致 |
| 完整依赖 | 新环境安装 94 包，版本逐项与锁匹配，`uv pip check` 通过 |
| 隔离 | 不启用 system-site-packages；全部发行包位于新虚拟环境，清除外部 Python/Conda/动态库路径并禁用用户 site-packages |
| CPU 协议 | 原有 46 项测试通过，1 个 Starlette/anyio 第三方弃用警告 |
| 模型加载 | RTX 4090 上离线加载已有权重，输出 30 fps、每帧 5023 顶点的网格 |
| 素材完整性 | 7 个权重、配置与模板文件在本次验证前后 SHA256 全部一致 |
| 清理 | 本轮临时 worker 已退出；原服务直连健康检查仍为 ready、busy=false |

环境：Python 3.10.21、PyTorch 2.5.1+cu124、torchvision 0.20.1+cu124、CUDA runtime 12.4、驱动 595.71.05、uv 0.11.31。新环境复用主机基础解释器、标准库、系统库与驱动；没有重新下载权重或模板。锁固定包分发摘要，不锁定系统依赖、源码包构建工具或其编译结果。

## 真实请求

沿用既有五类固定输入（中文、英文、中英混合、全零静音、带停顿语音），每类 3 条，共 15 条、45.348 秒。语音为合成输入，非真人录音。每条 1 次预热、3 次正式测量、1 次中途取消、1 次取消后恢复，总计 90 次全部通过。

| 阶段 | 请求数 | 接收网格帧 | 音频检查 | 完整结束 |
| --- | ---: | ---: | --- | ---: |
| 预热 | 15 | 1366 | 15/15 完整 PCM 一致 | 15/15 |
| 正式测量 | 45 | 4098 | 45/45 完整 PCM 一致 | 45/45 |
| 取消 | 15 | 15 | 15/15 已接收前缀一致 | 0/15，预期取消 |
| 恢复 | 15 | 1366 | 15/15 完整 PCM 一致 | 15/15 |

全部请求的已接收帧最大 PTS 偏差为 0。取消请求只核验已收到的前缀，不能当作完整音频通过。三次重复不增加独立样本数。

使用 `/v1/stream` 的增量 PCM 协议；`burst` 表示客户端尽快送入输入，不模拟物理麦克风实时节奏。机器记录的 `/health` 字段描述共享 HTTP 完整音频接口，实际本轮增量能力由 `/v1/stream/info` 记录。默认 block/lookahead 均为 200 ms；首批输出所需输入音频为 400 ms，不等于端到端延迟。

| 驱动 | 样本 | 成功 / 尝试 | 首媒体 P50 / P95（秒） | 完整耗时 P50 / P95（秒） |
| --- | --- | --- | --- | --- |
| streamingtalker | en-00 | 3 / 3 | 0.079 / 0.090 | 0.675 / 0.746 |
| streamingtalker | en-01 | 3 / 3 | 0.077 / 0.087 | 0.658 / 0.701 |
| streamingtalker | en-02 | 3 / 3 | 0.077 / 0.078 | 0.684 / 0.687 |
| streamingtalker | mixed-00 | 3 / 3 | 0.087 / 0.087 | 0.592 / 0.598 |
| streamingtalker | mixed-01 | 3 / 3 | 0.080 / 0.088 | 0.648 / 0.673 |
| streamingtalker | mixed-02 | 3 / 3 | 0.087 / 0.090 | 0.961 / 1.023 |
| streamingtalker | pause-00 | 3 / 3 | 0.077 / 0.085 | 1.151 / 1.289 |
| streamingtalker | pause-01 | 3 / 3 | 0.076 / 0.189 | 1.254 / 1.319 |
| streamingtalker | pause-02 | 3 / 3 | 0.087 / 0.089 | 1.363 / 1.464 |
| streamingtalker | silence-00 | 3 / 3 | 0.087 / 0.088 | 0.314 / 0.320 |
| streamingtalker | silence-01 | 3 / 3 | 0.078 / 0.192 | 0.557 / 0.649 |
| streamingtalker | silence-02 | 3 / 3 | 0.077 / 0.086 | 0.739 / 0.747 |
| streamingtalker | zh-00 | 3 / 3 | 0.087 / 0.177 | 0.656 / 0.739 |
| streamingtalker | zh-01 | 3 / 3 | 0.076 / 0.077 | 0.595 / 0.603 |
| streamingtalker | zh-02 | 3 / 3 | 0.077 / 0.078 | 0.651 / 0.658 |

分位数采用线性插值；小样本 P95 仅描述本次运行，不能视为稳定性承诺。

## 证据与复现

- [独立环境机器记录](benchmarks/2026-10-10-streamingtalker-standalone-validation.json)：94 包版本、固定源码、补丁与 overlay 摘要、锁文件摘要、7 个素材前后摘要、服务能力及清理状态。
- [90 次请求记录](benchmarks/2026-10-10-streamingtalker-standalone-results.json)：各轮成功/失败、完整 PCM 或前缀检查、帧数、时间戳、耗时与输入摘要。
- [安装与启动命令](../avatar-server/README.md)：固定源码、新环境安装、独立运行和 CPU 测试。

公开记录去掉临时进程号，publication 字段保留原记录哈希。原始音频、权重、人物模板、运行日志和本机配置不公开；输入摘要能核对已有文件，不能独立还原输入或取得模型资产。benchmark 的 provenance 描述客户端 Python 3.11 环境；模型环境 Python 3.10 记录在独立部署 JSON 中。记录中的仓库提交是运行时基线，本次新增锁和文档尚未提交，实际参与推理的文件另有逐项哈希。

准备同一输入清单及独立端口配置后，在应用环境运行（路径由部署者提供）：

```sh
python benchmark_avatar.py \
  --manifest "$INPUT_MANIFEST" --provider streamingtalker \
  --config "$AVATAR_CONFIG" --output "$OUTPUT_DIR" \
  --deployment-label standalone-cuda124-rtx4090 \
  --mode burst --rounds 3 --warmup 1 --timeout 90 --interrupt
```

首媒体和完整耗时覆盖客户端连接、模型、传输及适配器接收，不包含 ASR/LLM/TTS、浏览器播放或物理设备，也不是裸模型 RTF。本轮没有与旧部署做同条件性能对照，没有比较新旧网格逐字节相同，不据此宣称加速或质量改善。

## 服务端异常、准备阶段问题与未完成项

**本轮 worker 日志出现 1 次未捕获 ASGI RuntimeError**：`Cannot call "send" once a close message has been sent.`，位于 `serving/incremental_api.py:256` 调用 `send_terminal` 的发送路径。连接关闭与终态发送存在竞态；后续请求仍成功，临时 worker 正常清理。90/90 描述客户端协议断言，不能解释成服务端零异常。本轮安装与证据变更未修复该竞态，后续需用可重复测试定位并验证修复；机器记录保留日志摘要、异常次数和脱敏消息。

首次新环境测试从项目根目录执行，因找不到 `serving` 导入而收集失败；改到 `avatar-server` 目录后原有 46 项测试通过，没有修改产品代码。收尾时健康探针默认继承代理而超时，禁用代理直连原服务确认 ready；受限进程命名空间不能用于判断主机服务是否退出。应用评测回归首次误用未安装的 pytest；改用项目现有 unittest 入口后 31 项通过。这些检查问题不计入 90 次模型请求。

仍需人工检查动态口型、身份保持和视觉伪影，以及物理麦克风/扬声器验证。后续还需真正独立 GPU 主机、模型与模板获取流程和其他驱动的完整部署验收。本轮提供安装与协议复现证据，上游模型能力及许可归属见 [NOTICE](../avatar-server/NOTICE.md)。

后续修复与重新验证见[断连终态竞态报告](STREAMINGTALKER-CLOSE-RACE.md)；本页仍描述修复前的实际运行。
