# 组合会话验收客户端

已完成客户端、三容器控制器和无 GPU 的真实 Docker 生命周期演练。固定四项真实模型组合结果见[组合会话验证](../../docs/COMPOSED-SESSION-VALIDATION.md)；已有单组件和 CPU fixture 结果不能替代组合结果。

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

该命令由 `composed_trial.py` 驱动 stdin 准入。控制器使用私有配置、冻结载荷和新输出目录：

```sh
python deploy/session/composed_trial.py \
  --config /path/to/private-config.json \
  --manifest /path/to/frozen-payload/manifest.json \
  --payload /path/to/frozen-payload \
  --output /path/to/new-run-directory
```

私有配置包含 `images`（`llm/asr/app` 的不可变镜像 ID）、`models`（两个模型目录）、`ports`（三个独立端口）、`daily_health`（日常应用、LLM、ASR 的三个回环健康地址）、`tts_url`、`avatar_url`、`male_speaker` 和 `minimum_free_mib`。服务转发必须显式绑定 `127.0.0.1`，不能与测试端口冲突。模型目录只读挂载；应用无 GPU，并使用独立临时会话目录。

冻结 manifest 除 `cases` 外包含 `payload_sha256` 和 `models`；后者为 `llm/asr` 各自的 `revision` 与文件名到 SHA256 的 `files` 映射。载荷必须包含运行中的控制器/客户端及其依赖源码、用例文件和指定 WAV；这些内容前后重新校验。模型分别核对 10/12 个既有来源文件。

启动前至少需要 16 GiB 空闲显存；在 LLM 启动前重新检查，ASR 启动前须仍有 8 GiB。门槛是本部署的保守准入限制，不是通用显存需求或性能保证。日常应用和模型状态持续监测、逐请求准入；TTS 只暴露音色可用性，不具备可用的占用探针。3D 服务在批次前后检查空闲。

使用宿主网络访问回环转发，因此额外核对每个服务的监听 socket 属于本容器 PID1，且地址为回环。停止/删除前再次核对完整容器 ID、镜像 ID 和本轮标签；日志读取失败仍尝试删除已安全停止的容器，保留错误并判定整批失败。

`check_composed_docker.py` 只执行 CPU 生命周期演练，模型哈希和 GPU 准入明确标记为不适用。它启动三份 HTTP 假服务，分别运行正常协议和注入失败协议；验证真实 Docker 停止/删除及部分事件保存。演练中的 PID1 未处理 SIGTERM，实际退出码为 `137`（Docker 超时强制停止），不能称为优雅退出。
