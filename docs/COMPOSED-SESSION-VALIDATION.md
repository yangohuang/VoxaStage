# 隔离应用与真实模型的组合会话验证

2026-10-16，独立应用、Qwen3-ASR-1.7B 与 Qwen3-4B NF4 三个容器连接既有 TTS 和 StreamingTalker 服务，固定四项流式会话全部完成。收到 2,416 个音频/网格媒体包，6 段音频中 5 段完整结束、1 段按计划中断。22 个模型文件前后哈希一致，三个测试容器正常退出（退出码均为 0）并删除，日常服务恢复检查通过。

这是应用与真实模型服务的**协议组合验证**。播放进度按收到的样本数模拟；没有浏览器扬声器播放、屏幕渲染或物理麦克风测量，也没有人工音画质量评分。

## 固定范围与结果

| 用例 | 收到的媒体包 | 音频段（完整／总数） | 收到的 PCM 时长 | ready 后首包 |
| --- | ---: | ---: | ---: | ---: |
| 文字介绍实时数字人 | 361 | 1 / 1 | 12.032 秒 | 1.630 秒 |
| 合成中英混合语音 | 1,387 | 2 / 2 | 46.208 秒 | 4.044 秒 |
| 首包后打断，再请求简短回复 | 272 | 1 / 2 | 9.036 秒 | 0.393 秒 |
| 重连第三项会话后继续提问 | 396 | 1 / 1 | 13.184 秒 | 1.167 秒 |

时长来自收到的 24 kHz PCM 样本数，包含被中断段已收到的部分，不是扬声器播放时间。首包时间从 WebSocket `ready` 起算；语音用例包含约 2.091 秒输入音频的发送过程，不能当作用户说完后的响应延迟。打断用例的恢复回复首包位于同一 ready 后 1.508 秒。每项仅一次，不计算或声称总体 P95。

中英混合输入沿用既有 `mixed-00.wav`，SHA256 为 `d7ab797543e4517ba9d48daaaf7a77c8a1602ae2169a270b42c63357b7ba3b03`。ASR 返回“介绍一下语音 agent 技术。”，与合成提示词相同；输入未经人工听写，这不是新的真人识别率结论。

打断发生在首个媒体包后，输出 generation 从 1 经中断状态切换到恢复回复的 3。reset 后未接受旧 generation 媒体；原回复记录为 `interrupted`，未完成的短语没有确认进入已听文本。恢复回复完成后标记 `heard`。重连时精确核对这四条历史记录，再完成新的用户／助手两条记录。

## 防止验收误报

客户端逐包检查 PCM16、网格浮点数与维度、帧编号、采样偏移、时间戳、generation 和 `clip_end` 总样本数。整轮通过还要求持久化历史的原前缀、新增记录数、当前用户输入及当前回复全部匹配。不能用第一个音频段结束或上一轮的 `heard` 证明本轮完成。

开发审查发现并修复了三类验收问题：旧历史可能提前通过；恢复只能选择第一项而漏掉中断会话；拒绝包缺少失败原因。对应负例与真实回环 CLI 检查均保留。最终 19 项客户端测试、17 项组合控制器测试及 26 项既有生命周期测试通过。

## 容器与资源边界

- 复用已验证的[应用镜像](APPLICATION-CONTAINER-VALIDATION.md)、[ASR 镜像](ASR-CONTAINER-VALIDATION.md)和 [LLM 镜像](CONTAINER-LLM-VALIDATION.md)，版本和镜像 ID 见控制器记录。模型权重只读挂载，应用使用独立临时会话目录且无 GPU。
- 使用宿主网络连接专用 SSH 回环转发，三个服务都只绑定 `127.0.0.1`。另核对监听 socket 属于本容器 PID1，防止健康检查命中同端口的其他进程。
- 初次与 LLM 启动前均有 18,801 MiB 空闲显存；LLM 驻留后、ASR 启动前为 15,753 MiB。批次结束回到 18,801 MiB。未停止其他服务。
- 日常应用、LLM、ASR 共完成 314 次占用检查，最长一次约 0.542 秒；检查间隔是探针完成后的延时，不保证 200 ms 内发现占用。TTS 仅验证目标音色存在，其列表接口没有占用信号；3D 服务在批次前后验证空闲。
- 创建、启动、停止和删除绑定完整容器 ID、镜像 ID 与本轮标签。源码载荷和 LLM 10 文件、ASR 12 文件前后核验；结束后另行 `docker inspect` 确认三个 ID 不再存在，专用转发已关闭。

## 失败演练与保留记录

真实 GPU 批次之前执行无 GPU 的真实 Docker 演练。首次正常批次通过；第二批复用端口被 TIME_WAIT 阻止，在创建容器前失败。修正演练为每批分配新端口后，正常协议和第二请求注入失败两批均按预期结束，6 个不同容器全部删除，失败前的部分事件保留。

CPU 假服务没有处理 SIGTERM，Docker 超时强制停止后退出码为 `137`；这只证明强制清理路径。真实 GPU 批次的应用、LLM、ASR 退出码为 `0`。两类结果分别记录，不能把假服务演练说成模型推理或优雅退出验证。

## 证据与复算

- [四项用例与模型／载荷哈希](benchmarks/2026-10-16-composed-session-manifest.json)
- [完整客户端事件与检查点，5,241 行](benchmarks/2026-10-16-composed-session-events.jsonl)：不含原始声音或人物网格，只保留媒体哈希、长度和时间信息。
- [真实控制器、模型前后哈希与清理记录](benchmarks/2026-10-16-composed-session-controller.json)
- [统计、输出文本与首次 CPU 失败记录](benchmarks/2026-10-16-composed-session-summary.json)
- [修正后的 CPU Docker 演练](benchmarks/2026-10-16-composed-session-cpu-fixture.json)
- [运行工具与配置说明](../deploy/session/README.md)

从仓库根目录复算媒体统计：

```sh
python - <<'PY'
import json
from pathlib import Path
p = Path('docs/benchmarks/2026-10-16-composed-session-events.jsonl')
events = [json.loads(line) for line in p.read_text().splitlines()]
capture = next(row['report'] for row in events if row['event'] == 'capture')
assert capture['status'] == 'passed'
for case in capture['cases']:
    clips = list(case['evidence']['clips'].values())
    print(case['case'], sum(c['frames'] for c in clips),
          sum(c['samples'] for c in clips) / 24000,
          sum(c['ended'] for c in clips), len(clips))
PY
```

## 尚未证明的内容

本轮未覆盖 DINet、FlashHead、MiniCPM-o、视觉输入或完整新 GPU 主机部署，也不证明长期稳定性或多用户能力。中断前模型输出仍出现 Markdown 强调符，与口语提示约束不符；协议通过不能替代回答遵循性、语义质量和真实听感验收。更广泛真人语音、人工动态音画、物理回声与延迟门槛仍保留在[阶段验收](ACCEPTANCE.md)中。
