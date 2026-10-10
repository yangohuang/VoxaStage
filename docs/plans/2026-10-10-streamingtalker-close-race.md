# StreamingTalker 断连终态竞态修复计划

目标：修复真实部署日志发现的断开后终态发送异常，保持取消响应、单次终态和模型清理后才释放占用锁。

根因假设：ASGI 帧发送抛 OSError 后，Starlette 将 application_state 置为 DISCONNECTED；同时就绪的 reader 返回取消或无效输入终态。主协程选择 reader 分支，再次 send_json 触发 RuntimeError。通过真实 Starlette 状态机和注入发送故障验证该序列，不直接修改连接状态来伪造结果。

方案比较：统一吞掉 RuntimeError 会隐藏编程错误；改写全部读写调度扩大变更范围。选择在唯一终态发送入口识别已经断开的应用状态，不再向失效连接发送。保持取消和清理流程，其他 RuntimeError 仍应暴露。

- [x] 在 avatar-server/tests/test_incremental_api.py 用 SocketHarness 注入帧发送 BrokenPipeError，同时排入取消或无效控制消息；确认未修改源码时复现异常。检查会话关闭、占用释放及下一连接正常结束。
- [x] 修改 avatar-server/serving/incremental_api.py 的 send_terminal，检查 WebSocketState.DISCONNECTED；保留 terminal_claimed 的发送前所有权。
- [x] 增加活连接发送 RuntimeError 不被吞掉的对照；执行 avatar-server 现有全部 CPU 测试。
- [x] 复用独立环境与固定模型字节，在新源码副本、临时端口运行15条输入的90次请求；同时审计服务端日志，不覆盖旧证据。
- [x] 更新缺陷报告、协议说明与状态索引，保存源码摘要、测试及日志审计证据；独立审查、脱敏和导出核验通过。PR 合并状态以仓库记录为准。

测试从 avatar-server 目录运行 `python -m pytest -p no:cacheprovider tests -q`，使用已经安装的独立 Python3.10 环境。真实推理使用既有 benchmark_avatar.py，参数 burst、rounds=3、warmup=1、timeout=90、interrupt。通过标准包括客户端90次成功、无未捕获ASGI异常、输入素材摘要不变、临时worker退出；不将一次通过当作长期稳定性、口型质量或物理设备验收。
