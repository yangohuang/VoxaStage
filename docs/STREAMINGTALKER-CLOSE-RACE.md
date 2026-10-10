# StreamingTalker 断连终态竞态修复

2026-10-10。[独立部署验证](STREAMINGTALKER-STANDALONE-VALIDATION.md)中，90 次客户端断言通过，但服务端出现 1 次 `Cannot call "send" once a close message has been sent.`。本轮稳定复现这一发送失败与输入控制同时就绪的路径，并修复断连后再次发送终态的问题。

## 根因与改动

帧发送遇到网络 `OSError` 时，Starlette 将 WebSocket 的 application_state 置为 DISCONNECTED，writer 捕获 WebSocketDisconnect 后退出。与此同时，reader 可能已经收到 cancel 或无效控制消息。主协程优先处理已完成 reader 的结果，尝试发送 cancelled/error，触发断开状态禁止 send 的 RuntimeError。

唯一终态发送入口 `send_terminal` 现在在占有终态发送权后检查连接状态：已经 DISCONNECTED 则返回。reader 分支在此之前已取消并等待 writer，因此该状态检查到实际发送状态检查之间没有异步让出执行权。连接仍有效时的异常不会被宽泛吞掉。模型调用仍是合作式取消，GPU 占用仍由模型线程完成 session.close 后释放。

此改动属于本项目服务 overlay 的连接生命周期修复，不改变上游 StreamingTalker 模型、推理窗口、权重或语音生成能力。

## 可重复回归

新测试使用真实 Starlette WebSocket 状态机，仅替换 ASGI 发送端与模型会话：帧发送回调排入 cancel 或无效控制消息，然后抛 BrokenPipeError；由框架自身改变连接状态，没有直接篡改状态变量。

| 检查 | 修复前 | 修复后 |
| --- | --- | --- |
| 帧发送断连与取消同时到达 | 同一 RuntimeError，测试失败 | 通过，无再次终态发送 |
| 帧发送断连与无效输入同时到达 | 同一 RuntimeError，测试失败 | 通过，无再次终态发送 |
| 会话清理、占用释放及下一连接正常完成 | 在异常处中断正常断言 | 两种回归均检查通过 |
| 活连接终态发送的其他 RuntimeError | 保留异常契约 | 仍向调用者暴露，不吞异常 |
| 全部 CPU 协议测试 | 历史 46 项通过但未覆盖上述组合 | 49 项通过，1 个第三方弃用警告 |

另有 31 项现有评测测试通过。回归测试覆盖传输与资源生命周期，使用桩模型，不证明模型输出质量。

## 真实模型与日志复验

复用上一轮已按锁安装的 Python 3.10 独立环境，在新的源码副本、临时端口加载相同 7 个模型/模板文件。15 条输入、参数和资产摘要与上一轮逐项一致。仅服务 overlay 的断连处理改变；本轮没有重装环境或下载权重。

| 阶段 | 请求通过 | 网格帧 | 音频保持 |
| --- | --- | ---: | --- |
| 预热 | 15/15 | 1366 | 完整 PCM 一致 |
| 正式测量 | 45/45 | 4098 | 完整 PCM 一致 |
| 取消 | 15/15 | 15 | 已接收前缀一致 |
| 恢复 | 15/15 | 1366 | 完整 PCM 一致 |

90 次客户端检查全部通过，最大已接收帧 PTS 偏差为 0；本轮 worker 日志 **0 个 ERROR 行、0 个 Traceback、0 次未捕获 ASGI 异常**。临时 worker 已停止，7 个资产文件前后摘要不变。取消请求不按完整音频计分。

旧运行的 1 次异常与新运行的 0 次异常是有限复验结果，修复的因果证据主要来自同一受控回归在旧代码失败、新代码通过；不能由 90 次通过宣称所有断连场景或长期稳定性均已解决。没有据此宣称性能加速或网格逐字节等价。日常服务本轮未切换，部署者需更新 serving overlay 并重启对应 worker 才会使用修复。

## 证据与复现

- [部署和回归记录](benchmarks/2026-10-10-streamingtalker-close-race-validation.json)：环境、资产/源码摘要、旧测试失败记录摘要、新测试数量、服务日志摘要及清理状态。
- [逐请求记录](benchmarks/2026-10-10-streamingtalker-close-race-results.json)：全部 90 次请求与适配器接收指标，包含每条输入/PCM 摘要。客户端 Python 与模型 Python 分别记录。
- [安装说明](../avatar-server/README.md)和[服务协议](../avatar-server/PROTOCOL.md)。

公开 JSON 删除临时进程号并声明新增的 publication/regression 字段。原始测试/worker 日志、输入音频、权重、模板与本机配置不公开，记录其哈希；不能仅凭摘要还原这些资产。历史失败报告保持原样，新增报告记录修复结果。

从 avatar-server 目录、已安装独立环境执行：

```sh
PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=4 python -m pytest -p no:cacheprovider tests -q
```

真实模型复验沿用独立部署报告的 benchmark_avatar.py 命令，固定 `--mode burst --rounds 3 --warmup 1 --timeout 90 --interrupt`，deployment-label 改为 `close-race-fixed-cuda124-rtx4090`。必须同时检查 worker 日志，客户端 passed 不足以证明服务端无异常。

人工口型、身份/伪影、物理麦克风与真正独立 GPU 主机全量部署验收仍未完成。
