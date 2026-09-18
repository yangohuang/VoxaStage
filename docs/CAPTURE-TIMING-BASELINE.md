# 浏览器采集边界 · 2026-09-19

`Microphone`现在支持默认关闭的`captureTiming`诊断选项。启用后，每个PCM包可关联重采样输出样本区间、AudioWorklet输入帧坐标与主线程时钟观察；普通通话继续发送原来的PCM二进制包。此改动已在独立工作副本验证，没有替换当前在线页面。

## 三次真实浏览器检查

使用同一份合成WAV作为Chromium文件虚拟麦克风，经`getUserMedia → MediaStreamSource → AudioWorklet → Microphone回调 → 本地WebSocket`。每次约5.2秒，默认保留浏览器回声消除、降噪和自动增益请求；实际track为48kHz单声道，AudioContext分别为16k和48k。这里没有运行ASR或大模型，也没有使用物理麦克风。

| 配置 | 收发包数 | 输出样本数 | 有效时间映射包 | 跨断点无效包 | 回调PCM与接收PCM SHA256 |
| --- | ---: | ---: | ---: | ---: | --- |
| 默认模式 / 16k context | 260 | 83200 | 不记录 | 不记录 | 一致 |
| 诊断模式 / 16k context | 260 | 83200 | 259 | 1 | 一致 |
| 诊断模式 / 48k context | 260 | 83200 | 259 | 1 | 一致 |

每包640字节、320个16k样本；诊断结构仅在worklet与主线程之间传递，WebSocket仍接收纯PCM。输入、代码摘要、全部包记录及初次失败见[机器记录](benchmarks/2026-09-19-capture-timing.json)。每种配置一次最终观察，不报告P95，也不声称经过浏览器DSP的不同请求彼此逐字节相同。

## 实际发现并处理的启动断点

最初两个诊断请求传输校验通过，但全部时间映射失效。临时记录前几个处理块，观察到16k context的`[帧位置, 块长]`为`[0,128] → [384,128] → [512,128]`，第一个和第二个块之间有256帧缺口。不能假设节点启动后的回调一直连续。

最终实现保留重采样器和PCM字节，单独跟踪“已接收输入样本索引→当前连续段的真实帧位置”。遇到缺口增加`segment`；跨越缺口的包设置`valid:false`，两个输入帧坐标均为`null`。完全落在新连续段的包恢复映射。没有丢包、补零或重置重采样相位来隐藏缺口。最终两次诊断均保留一个无效首包，之后259个包有效。

## 字段含义

| 字段 | 含义 |
| --- | --- |
| `outputStartSample`、`samples` | 本节点输出PCM的起始样本索引及包长度；16kHz，重新建节点从零开始 |
| `inputRate`、`outputRate` | AudioContext实际采样率和输出PCM采样率 |
| `segment`、`valid` | 连续输入段编号；包是否能用单个连续段映射 |
| `firstInputFrame`、`lastInputFrame` | 首末输出样本在Web Audio输入图中的线性插值位置，可能为小数 |
| `blockStartFrame`、`processedThroughFrame` | 产生该包的处理块起点及块末尾的排他边界 |
| `anchorPerformanceBeforeMs`、`contextTimeAtReceive`、`anchorPerformanceAfterMs` | 在主线程收到包后，用两次`performance.now()`夹住`context.currentTime`读取 |

分数位置由相邻两个输入样本插值，不等于只依赖一个原始输入样本。输入缺口前后的PCM仍按原有逻辑拼接，诊断只标明其采样时间不连续。

这些帧坐标属于Web Audio处理图；主线程观察锚点受到音频量子精度和调度影响，不能据此精确还原麦克风声学输入时刻。`getOutputTimestamp()`描述输出设备的渲染位置，不能拿来当输入捕获时间。[Web Audio规范](https://www.w3.org/TR/webaudio-1.0/#AudioWorkletGlobalScope)、[输出时间戳定义](https://www.w3.org/TR/webaudio-1.0/#dom-audiocontext-getoutputtimestamp)。

## 使用与复核

在加载此版本静态资源的页面中，通过现有浏览器脚本执行接口或开发者工具导入模块。下面只示范采集诊断；运行前停止同页已有采集，调用`start()`需由用户交互触发。不要把诊断对象序列化成音频发给后端。

```javascript
const {Microphone} = await import('/avatar/static/microphone.mjs');
const captureRecords = [];
const microphone = new Microphone((pcm, timing) => {
  // 仅保存有界诊断，不保存原始音频。
  if (captureRecords.length < 1500) {
    captureRecords.push({bytes: pcm.byteLength, timing: timing ?? null});
  }
  // 已连接且缓冲有界的会话仍使用 socket.send(pcm)。
}, {captureTiming: true});
// 在点击处理器中 await microphone.start()；完成后 await microphone.stop()。
```

文件虚拟麦克风验证使用独立Chromium配置目录与`--use-fake-device-for-media-stream --use-fake-ui-for-media-stream --use-file-for-fake-audio-capture=/absolute/path/input.wav`；浏览器仍经过真实采集API与worklet。最终运行的本地驱动脚本与源码摘要随本地实验产物保存，脚本含部署路径，不进入源码包。单元测试可在源码目录直接运行：

```bash
node avatar-web/tests/capture-timing.test.mjs
node avatar-web/tests/microphone.test.mjs
```

新增12项测试通过，包括三个采样率、PCM不变、断点恢复、44.1k已知斜坡的独立数值验证、多断点相位保持、默认回调兼容、迟到包清理与旧worklet无元数据。前端与评测共49项Node测试及1个idle断言脚本通过，分段映射已独立审查。

本检查点补齐浏览器处理图中的采样位置与传输关联，不把它称为物理设备延迟验收。动态口型质量、人工试听与实际设备声学测量仍分别保留为未完成项。
