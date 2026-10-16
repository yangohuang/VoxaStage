# 组合会话验收客户端

当前是验收工具准备阶段，尚未完成三容器控制器或真实模型组合批次。已有单组件报告不能替代组合结果。

客户端连接隔离应用的 `/avatar/ws`，选择 StreamingTalker 与级联后端。`cases.json` 固定四项：文字、既有中英混合合成 WAV、媒体输出后的显式打断及恢复、重连第三项会话。WAV 只记录文件哈希与合成提示词；原始素材不纳入公开源码，也不把提示词作为人工核准转写。

验收条件包括：

- PCM16、网格结构、帧编号、采样偏移与时间戳有效；每段结束样本数一致。
- reset 后不得收到旧 generation 的媒体。
- 完成判定同时核对初始历史前缀、当前新增记录、当前输入和回复，且所有已收到的音频段结束；上一轮 `heard` 不能证明本轮完成。
- 每个请求先输出 `request_ready`，等待宿主通过 stdin 发送相同 case 的 `{"continue":"case-id"}`；未准入不连接。
- 超时或失败保留检查点。被拒事件只记录有界坐标、哈希和错误码，排除原始 PCM、网格与服务错误详情。

播放确认按收到的样本数模拟，**不代表浏览器实际播放、用户听到声音或音画同步质量通过**。`first_media_s` 从 WebSocket ready 开始计时；语音用例包含输入发送时间，不能直接称为端点检测后的响应延迟。

使用应用依赖环境运行 CPU 检查：

```sh
python -m unittest discover -s deploy/session -p 'test_*.py'
```

其中 `test_session_wire.py` 启动临时回环 HTTP/WebSocket 假服务和真实客户端子进程，检查 stdin 准入、文字、历史恢复、20ms WAV 分包及失败退出。测试不调用日常服务或 GPU。其余测试覆盖状态机边界。需要允许本机回环 socket。

客户端入口：

```sh
python deploy/session/session_client.py \
  --manifest deploy/session/cases.json \
  --inputs /path/to/frozen-inputs \
  --url http://127.0.0.1:23714
```

该命令需由后续宿主控制器驱动 stdin 准入。真实执行前仍须完成容器归属校验、模型哈希、资源准入、日常服务监测和异常清理；目前不提供绕过这些检查的自动放行方式。
