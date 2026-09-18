# 角色音色、idle 与质量升级

用户选择：本地优先，开源时提供云端 API 接入方式。

## 角色与音色

| 数字人 | 语音角色 | 级联默认音色 |
| --- | --- | --- |
| StreamingTalker | 男声 | pipecat_male |
| DINet | 男声 | pipecat_male |
| FlashHead | 女声 | female11 |

映射由服务器的 Provider 决定，客户端不能提交音频文件路径。级联通过
`PIPECAT_MALE_SPEAKER_ID` / `PIPECAT_FEMALE_SPEAKER_ID` 配置已注册的 IndexTTS 音色；
MiniCPM worker 使用 `--male-reference /path/to/male.wav --female-reference /path/to/female.wav`。
MiniCPM 同时使用系统音色条件和 token2wav 参考，切换在单模型推理锁内完成。
本机 TTS 原先启用固定音色，忽略 audio_id；现已添加 [本地多音色映射](../tts-compat/README.md)，启动时同时加载男女声参考。
音色名称和人物外观不是声纹验证，本机生成样本保存在 `artifacts/voice-matching/` 供试听。

男声参考由本地 Qwen3-TTS-1.7B-CustomVoice 的固定 Uncle_Fu 音色生成，女声沿用已有 female11；各取最多8秒，保存在忽略的 runtime。原 samsoncai 与 yzl 实为同一参考文件，已不再用作角色默认男声。
这些角色音色、人物模板和媒体不随源码发布。新部署需自行提供素材并注册音色；
缺少对应音色会明确失败，不默默回退到另一性别的音色。

## Idle

每个角色使用 `runtime/idle/<provider>.mp4`，provider 仅允许 dinet、flashhead、streamingtalker。
服务端提供 `/avatar/idle/<provider>.mp4`。缺少素材时保留原占位画面。
可放入已有的同角色待机素材；本机素材由各自模型6秒静音输出生成，正反向拼接为约12秒无声循环。
StreamingTalker 使用同一个 WebGL 渲染器制作，保持头部外观一致。

待机视频 muted、loop、playsinline。连接前、等待说话、回答播完、打断后及断开会话后播放；
实际音画开始播放时暂停。切换角色时清空上一角色实时画面并切换idle。
循环播放不再调用模型，也不参与对话声音的 AudioContext 时间轴。

浏览器实测 FlashHead：idle开始正常、回答渲染76帧、72704音频采样；
未出现 speaking 与 idle 同时显示；播完恢复idle，断开后继续播放。
记录：`artifacts/idle-browser.json`、`artifacts/idle-browser-extra.json`。
另外验证了 DINet 级联、StreamingTalker MiniCPM 的实际回复、播完与断开恢复；三种idle均检查了图像及媒体格式。

## 识别与回答的验收口径

MiniCPM 直接理解语音，与级联 ASR 是不同路径；术语上下文仅影响级联。
本地 Qwen ASR 新增可选 `X-ASR-Context`，应用通过 `PIPECAT_ASR_CONTEXT` 提供
最多500个ASCII字符的技术词汇，如 `Pipecat, MiniCPM-o, agent, WebRTC`。
不对识别结果做字符串替换，也不强制中文语言，保持中英混合识别。

0.6B 的既有4条录音中，术语提示没有带来明显词汇改善，最初保留为空。
随后部署 Qwen3-ASR-1.7B（官方 revision `7278e1e70fe206f11671096ffdd38061171dd6e5`）。
当前本机切换到1.7B并启用上列项目术语：相同合成录音中 MiniCPM-o、DINet、StreamingTalker 拼写得到修正；4条普通中文/中英混合回归内容保持正确，仅有标点差异。
新模型7条录音的结果位于 `artifacts/asr-quality/qwen17*.json`，技术样本每次推理约0.16–0.24秒，不包括麦克风端点判断。
这说明项目术语样本改善，不代表已经测得真实麦克风总体误识别率下降。
记录：`artifacts/asr-context-comparison.json`。新增技术术语合成样本保留生成文本和原始识别结果；
生成文本不等于经过人工校对的语音真值，不能将该实验当作真实麦克风错误率。

回答提示取消原有“最多50字”限制，要求解释关键原因并给例子，保留语音可读性和有界输出。
旧模型同样使用新提示词留出基线，模型候选需在相同问题下比较，不能仅凭型号宣称改善。
实际比较了 Qwen3.5-4B NF4：三题完整生成约2.1–2.9秒，旧Qwen3-4B约1.5–1.6秒；
新模型在音色/渲染器问题引入无关的空间音频前提，未体现稳定质量提升，因此已停止候选进程、保留Qwen3-4B作为默认。
记录：`artifacts/llm-quality-baseline.json`、`artifacts/llm-quality-qwen35.json`。
回答长度与指令已改善，但没有宣称本地大模型能力升级完成；真实误识别、主观音色与长期对话仍需反馈。

当前服务器的持久端点配置在忽略的 `runtime/backend-env.json`，`ctl.py` 重启读取；显式环境变量优先。
旧ASR0.6B在18315保留，回退时将该文件的 `PIPECAT_ASR_URL` 改回 `http://127.0.0.1:18315`、模型名改回 `Qwen3-ASR-0.6B`，并清空术语提示后重启应用。
当前1.7B运行于另一台用户自有GPU机器，通过仅监听localhost的SSH隧道18413连接；不使用云端API。

## 云端 API 接口（默认关闭）

ASR 与 LLM 可独立切换。支持兼容 `/audio/transcriptions` 的 multipart 音频接口，以及
`/chat/completions` 的 SSE 文本流，返回 `[DONE]` 完成标记。其他协议需另写适配。
API协议与字段见 `api_backends.py` 和 `.env.example`。

```bash
# 默认均为local；只启用需要切换的组件
PIPECAT_ASR_MODE=local
PIPECAT_LLM_MODE=api
PIPECAT_LLM_API_BASE=https://your-provider.example/v1
PIPECAT_LLM_API_MODEL=your-model
PIPECAT_LLM_API_KEY=your-key
# 按服务端实际协议选择max_tokens或max_completion_tokens
PIPECAT_LLM_TOKEN_FIELD=max_tokens
```

ASR 对应 `PIPECAT_ASR_MODE=api`、`PIPECAT_ASR_API_BASE`、`PIPECAT_ASR_API_MODEL`、
`PIPECAT_ASR_API_KEY`。字段示例见 `.env.example`。启动前导出这些环境变量。
密钥仅在服务端使用，不发给浏览器、不进入发布包。显式启用后相应文字或音频会发往所配API。
本轮只进行了模拟HTTP协议测试，未启用或计费调用任何云端API。
API模式健康信息的 `availability=not_probed` 仅说明配置存在，不保证远端可用。

## 本轮验证

65项应用Python测试、6项MiniCPM worker测试、16项前端测试通过；独立审查发现的长音频取消清理及停顿后突发问题均已复现并修复。
真实音色样本4份已生成：级联男/女与MiniCPM男/女均有声，男声基频中位数约107–111Hz、女声约230–236Hz；这用于排查参考音色被覆盖，主观听感仍以试听为准。
源码包仅含实现与文档，不含这些声音/人物素材。

## 长回复与项目知识

放宽回复长度后的真实语音测试复现了单段音频超过30秒的数字人错误。
AvatarSession 现将PCM按原30秒上限连续分段，不删减采样；跨片段按滚动播放时钟最多预送约1秒，避免停顿后突发积压。
取消期间通过 `aclosing` 等待当前模型流清理；新增分段完整性、暂停恢复节奏、清理屏障三项回归。
MiniCPM本来最多输出30秒，保留其单片段行为。

级联提示词新增仅在项目相关问题时使用的事实背景，依据
[Pipecat官方仓库](https://github.com/pipecat-ai/pipecat)与
[MiniCPM官方仓库](https://github.com/OpenBMB/MiniCPM-V/blob/main/README.md)，
并说明本项目对话后端与形象渲染的分层关系。
该提示修正了当前模型对项目归属和架构的编造，不是模型训练或检索系统，也不能代替更强通用模型。
`artifacts/llm-quality-grounded.json`记录补充事实后的回答。

最终语音输入完整链路：原始7.34秒测试录音识别出Pipecat/MiniCPM-o，级联生成37.12秒音频，拆为30秒与7.12秒两片段；全部配对媒体接收完成，无错误。
首次媒体约在输入开始后9.62秒到达（包含7.34秒说话、VAD结束判断及推理），不等于单模型推理延迟。记录：`artifacts/asr-quality/integrated-smoke.json`。
