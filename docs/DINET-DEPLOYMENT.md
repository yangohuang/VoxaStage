# DINet 既有部署服务接入

本项目提供 `dinet_backend.py` 客户端适配器，复用部署者已有的 DINet WebSocket 服务。它不安装 DINet 模型、人物训练资源或商业服务端，不能把客户端源码包当成完整 DINet 推理环境。

此处“原生协议”指既有部署服务的接口，并非 DINet 官方仓库的标准 API；本仓库未审计该服务的完整实现与版本。

当前验证目标为本机 `kyc-video-local` 服务，地址格式：

```bash
PIPECAT_DINET_URL=ws://127.0.0.1:19003/api/ws/live_video/<character_id>
```

也可在本机 `runtime/avatar-config.json` 中设置 `providers.dinet.url`。浏览器不能传服务地址；下拉选择 DINet 后使用已有 Qwen3-ASR → Qwen → IndexTTS 链路。需要新的授权角色时，应在 DINet 服务中部署人物资源并修改服务端配置地址。

原生协议：`open_stream=1`、`input_data_type=audio`、`audio_sr=16000`、`open_h264=0`；103 是尺寸/25fps/YUV420P 元数据，101 是原始图像，102 是音频，100 是开始/结束控制。图像可能拆成多个二进制消息，必须按 frame_size 精确组装。视频转换为最长边 640 的 JPEG；PyAV 已在主环境锁文件中固定。

播放使用原始 TTS 的 24k PCM16，忽略 DINet 返回的音轨。服务端会补齐 200ms 输入块，可能输出尾部补零帧；适配器按输入样本数裁剪，最多接受额外 10 帧，防止无限输出。输入最长 30 秒，发送最多领先实时进度约一秒；只有完整且序号连续的图像才进入播放器。

每段回复使用独立 transaction/session 和 WebSocket。打断发 is_interrupt + is_end 并关闭连接；关闭连接是本地传输清理，不代表上游提供 GPU 清理确认。已通过取消后再次实际生成验证恢复。若尚未取得元数据时服务以1011和明确的“No available workers”原因关闭，适配器在5秒重试窗口内等待容量释放；该窗口不包含连接或首元数据等待的独立超时。模型错误不重试，已发送音频不重放，取消可传播。持续无容量仍显示会话错误。

复验：

```bash
.venv/bin/python -m unittest test_dinet_backend test_avatar_providers
.venv/bin/python smoke_video.py --backend dinet \
  --url ws://127.0.0.1:19003/api/ws/live_video/<character_id> \
  --input /path/to/mono-pcm16.wav --rounds 3 --interrupt \
  --output artifacts/dinet-model
.venv/bin/python smoke_avatar.py \
  --url 'ws://127.0.0.1:18314/avatar/ws?provider=dinet' \
  --text '请用一句话介绍语音agent。' --interrupt \
  --output artifacts/avatar-dinet-text.json
```

`artifacts` 内的图像、输入音频和本机人物 ID 不进入源码导出。导出包包含适配器、协议、测试、配置模板与本文。启动服务、部署模型及人物素材的说明以实际 DINet 服务项目为准。

实际兼容性：原生 worker 的 receive_msg_process 在拼接残留音频后，若刚好填满块，没有清空 rest_chunk；随后的消息可能重复带入旧残留。最初 24k / 250ms 分片出现约 1.5 倍输出时长，直接发送任意长度的重采样块也不能保证正确。适配器使用连续 soxr 24k→16k 重采样，并重新打包为严格的 6400 字节 / 200ms 原生消息，末包补齐后再补一块 200ms 静音，冲刷模型尾部特征窗口，以避开该上游路径。原始 24k 播放音轨保持不变。已增加固定分片约束、重采样样本数和过多输出帧拒绝的测试；未修改共享 DINet 服务。

短句边界：90113 个 24k 样本（3.755 秒）在直接 EOS 时仅得到 92 帧，少于覆盖原音频所需的 94 帧。适配器仅向模型追加上述尾部补零，返回视频仍裁到原始样本时长，补零不进入扬声器音轨。

可选的[消费提前量启动器](../dinet-server/README.md)仅适用于指纹匹配的既有服务。它保留原代理与心跳，在进程内替换等待公式，原生文件不修改。部署配置可明确设置0秒兼容模式或经验证的提前量；不同部署仍需通过原路径复测后启用。

最新的256人物切换、代理背压与断开清理记录见[256部署验证](DINET-256-VALIDATION.md)。
