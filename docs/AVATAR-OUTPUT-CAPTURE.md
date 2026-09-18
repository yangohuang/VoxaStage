# 输出存档与浏览器探针

这些工具扩展[适配层基线](AVATAR-EVALUATION.md)。生成输出的数值连续性、人工口型质量和浏览器播放是三个不同层面，分别记录。

## 固定输入与原始输出

在已配置本地TTS的环境执行；每次指定新的输出目录，避免旧成功结果混入。合成文本仅是生成提示，`transcript_reviewed`默认false。

```bash
.venv/bin/python prepare_avatar_cases.py --voice female --output artifacts/quality-inputs
.venv/bin/python capture_avatar.py \
  --manifest artifacts/quality-inputs/cases.json --provider flashhead \
  --config runtime/avatar-config.json --output artifacts/quality-flashhead \
  --deployment-label 'model revision; anonymous character; settings; device; residency'
```

分别替换provider为`dinet`和`streamingtalker`串行执行。输入集包括中文、英文、中英混合各3条，1/2/3秒静音，以及由已有中文音频拼接且插入400/700/1000ms静音的3条停顿探针。停顿样本不是独立录音。

每个case保存原始JPEG或float32顶点、3D拓扑、配对PCM WAV、逐帧PTS/样本位置/载荷哈希、元数据和完成状态。只有完整流和清理结束、PCM逐样本校验通过后才标记成功。失败保存异常类别，不保存后端错误文本。运行素材默认在忽略目录，不导出进源码包。

相邻像素MAE采用RGB [0,1]，重复率按JPEG字节相等判断；顶点RMS采用模型原生单位。它们用于寻找待检查片段，不能代表口型正确、身份保持或感知质量。不同人物/驱动的数值不直接排名。捕获包含解码和磁盘开销，不用于替代独立性能基线。

部署标签需记录非敏感模型/权重版本、匿名人物标识、推理设置、设备及其他模型驻留情况。工具不向模型服务查询或核验这些信息；未能核实的部分必须明确写“未核实”，不能据此承诺精确重现数值。

## 浏览器观察

`evaluation/browser-probe.js`在连接前注入专用测试页面，观察WebSocket与WebAudio；`evaluation/browser-round.js`在真实点击连接、AudioContext解锁后注入并调用：

```javascript
// 先选定对话后端与数字人，再点击连接；保持麦克风关闭。
const result = await runVoxaBrowserRound('cascade-flashhead');
const trace = voxaProbe.snapshot();
```

使用浏览器DevTools或部署者自备自动化工具注入；不得在用户正在通话的页面运行。可用独立Chrome profile、`--use-fake-device-for-media-stream`、`--use-fake-ui-for-media-stream`和`--use-file-for-fake-audio-capture=/absolute/input.wav%noloop`固定采集输入。合成输入前后留静音，让采集/VAD完整结束；记录输入WAV哈希、Chrome版本、GPU渲染方式和应用源码指纹。自动化需单独记录连接失败和探针失败，不能只保留成功轮次。

探针字段的准确含义：

- `endOfActivePacketToScheduledStartMs`：最后一个RMS超过0.004的PCM发送包，到首个AudioContext安排起点。不是已知WAV最终语音样本的精确采集时间，也不是物理麦克风或扬声器延迟。
- `firstMediaToScheduledStartMs`：首配对媒体事件到安排起点；同一浏览器单调时钟。首媒体与音源按当前generation筛选。
- 打断：按钮点击前后源数量、generation变化和250ms内旧帧停止情况；不是语音触发barge-in延迟。
- 恢复：发送短文本后等待新播放、匹配generation的clip_end、队列排空且idle播放，再断开检查。只验证这条短恢复请求，不代表任意多clip长回复均已生成结束。
- 异常也会执行disconnect；事件/音源记录有上限，`truncated=true`的证据不得用于完整性结论。不要在同一页面无限累积轮次。

成功只支持本次输入、环境和事件边界；必须区分首轮、预加载和重复请求。物理设备回声、人工试听、语音打断、精确采集样本时间及长期稳定性仍需要独立评测。
