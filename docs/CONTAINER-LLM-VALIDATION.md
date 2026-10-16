# Qwen 流式 worker：隔离容器部署验证

2026-10-16，日常 Qwen3-4B NF4 worker 在独立容器用户空间完成 **17 次协议请求**：14 次完整响应、3 次主动关闭流，随后 3 次恢复请求均成功。代码、模型、依赖锁与输入均留有校验记录；测试容器已移除，原有服务未切换。此前一次模型启动失败、三次未成功的构建尝试也保留在记录中。

这补齐了 LLM worker 的 Linux 用户空间安装证据。它共享原主机内核和 GPU 驱动，没有验证全新 GPU 主机、ASR、TTS 或数字人全链路，也没有给答案正确性评分。

## 环境与来源

| 项目 | 实测配置 |
| --- | --- |
| GPU / 驱动 | RTX 4090 / 595.91.07 |
| 宿主 | Linux 6.8.0-138-generic；Docker 29.3.1；控制器 Python 3.10.12 |
| 容器 | 官方 Python 3.12.15、Debian Bookworm；UID 65532 |
| 模型 | Qwen3-4B；revision `1cfa9a7208912126459214e8b04321603b3df60c`；NF4 |
| 依赖 | 63 个哈希锁定包；Torch 2.11.0+cu130、Transformers 5.12.1、BitsAndBytes 0.50.2、Accelerate 1.14.0 |
| 系统依赖 | libgomp1 / libstdc++6 `12.2.0-14+deb12u1`；libc6 `2.36-9+deb12u14` |
| 最终镜像 | `sha256:52c11d4beef410a82a1a78f60ca6e7209c1ba3904214d2419f463fd02938c6d1` |

[基础镜像与构建文件](../llm-server/deploy/Dockerfile.cached-wheels)固定 Python 镜像摘要和 libgomp1 版本。所有模型包从原始 wheel 安装到 `/opt/worker`，没有复制宿主虚拟环境。完整 wheelhouse 的 Python 安装步骤禁止网络，并使用 `--no-index`、`--require-hashes` 和 `pip check`；基础镜像与 APT 获取仍需网络。系统仓库没有做完整快照，未来构建不能保证得到相同镜像 ID。

模型、Transformers 推理与 BitsAndBytes 量化来自上游。本项目新增的是流式 HTTP 接口、部署隔离、占用保护、按容器身份清理以及可追溯的测试记录，不能把上游模型或量化方法当作原创贡献。

## 真实协议结果

[固定输入](benchmarks/2026-10-16-container-llm-input.json)在执行前保存，顺序为预热、中文/英文/中英混合各 3 次、长回复、3 组关闭流与恢复。共 17 次请求，其中语言部分是 **3 个不同问题各重复 3 次**。历史协议的用户问题相同，但旧记录没有完整系统提示词，因此不宣称两次实验的完整消息完全一致。

| 检查 | 结果 |
| --- | --- |
| 完整响应 | 14 次，包含 1 次预热、9 次语言请求、1 次长回复、3 次恢复 |
| 流关闭与恢复 | 3/3；首个文字片段时观察到 busy，关闭后观察到 idle，下一请求成功 |
| 长回复 | 199 个文字片段；首片段早于整轮结束 |
| 原始协议记录 | 2,452 条 JSONL 事件；无 NDJSON 错误或客户端失败 |
| 模型完整性 | 10 个核心文件在运行前后哈希一致 |
| 清理 | 客户端退出 0，容器退出 0，容器移除，清理错误为 0 |
| 日常服务保护 | 128 次健康探测；最长一次约 0.300 秒 |

9 次语言请求的首文字片段中位数为 **47.62 ms**，完整流结束中位数为 **1,531.75 ms**；模型加载为 3.68 秒。这是同一容器内回环 HTTP 的小样本观测，首文字片段时间在健康观察前记录，完整流结束计时包含首片段健康观察等客户端开销。它不是麦克风到声音的延迟、跨机器性能比较或生产 SLA。

取消证据来自客户端关闭、HTTP busy/idle 与后续请求，没有 GPU 内部取消事件；不能推断内核被强制终止。中英混合文字请求通过，也不能证明中英混合语音识别改善。

可复核数据：

- [原始 JSONL](benchmarks/2026-10-16-container-protocol-events.jsonl)、[控制器原始结果](benchmarks/2026-10-16-container-protocol-result.json)、[逐例统计](benchmarks/2026-10-16-container-protocol-summary.json)。
- [最终环境检查](benchmarks/2026-10-16-container-runtime-environment.json)、[实际执行的环境探针](benchmarks/2026-10-16-container-environment-probe.py)、[宿主与镜像配置](benchmarks/2026-10-16-container-host-runtime.json)。
- [63 包 wheel 清单](benchmarks/2026-10-16-container-wheels.json)、[模型来源与哈希](benchmarks/2026-10-16-qwen3-file-provenance.json)、[五次构建记录](benchmarks/2026-10-16-container-build-attempts.json)。

## 保留的失败与修复

1. 首次在线构建下载超时。把读取超时调为 120 秒后，第二次收到截断的 cublas wheel，哈希检查正确拒绝安装，均退出 1。
2. 仅提供 10 个大包的 `--find-links` 缓存仍选择了索引 URL。核对构建进程身份后停止该次构建，退出 130。随后补齐 63 个原始 wheel，逐个验证锁内哈希，并禁止 Python 安装步骤访问网络；构建成功。
3. 初次模型启动在处理任何协议请求前失败，不能计作错误回答。顶层 import 探针没有覆盖 `AutoModelForCausalLM` 的延迟导入：Torch Dynamo 初始化缓存时调用 `getpass.getuser()`，数字 UID 65532 没有 passwd 条目，触发异常。容器不是 OOM；[失败批次](benchmarks/2026-10-16-container-startup-failure.json)与[原始启动异常栈](benchmarks/2026-10-16-container-startup-worker.txt)单独保留。
4. 两份 Dockerfile 都增加 `USER=voxastage`、`HOME=/tmp` 环境默认值；控制器也显式传入。实际权限仍是 UID 65532。最终镜像与上一镜像的文件系统层相同，仅镜像配置改变；worker、模型和包锁不变。新的 CPU 探针覆盖真实延迟导入和 `/tmp` 缓存目录，随后运行全新完整 17 次请求。没有筛选答案重跑。

## 复现入口

需要 Linux x86_64、Docker BuildKit、可用的 NVIDIA 容器运行时、已核验的模型目录，以及日常应用和 LLM 的回环健康地址。控制器要求单 GPU 至少 8 GiB 空闲；这只是本轮准入阈值，不代表所有模型的显存需求。

在仓库根目录准备最小构建上下文。`WHEEL_DIR` 指向按锁下载并核验过的完整 Linux/Python 3.12 wheelhouse；不要将 wheel、模型或运行日志提交到 git。

```sh
BUILD_CONTEXT=$(mktemp -d)
mkdir -p "$BUILD_CONTEXT/llm-server/deploy" "$BUILD_CONTEXT/wheels"
cp llm_server.py "$BUILD_CONTEXT/"
cp llm-server/deploy/Dockerfile.cached-wheels \
   llm-server/deploy/requirements-standalone.lock "$BUILD_CONTEXT/llm-server/deploy/"
cp "$WHEEL_DIR"/*.whl "$BUILD_CONTEXT/wheels/"
docker build --progress=plain --iidfile "$BUILD_CONTEXT/image.id" \
  -f "$BUILD_CONTEXT/llm-server/deploy/Dockerfile.cached-wheels" \
  -t voxastage-llm-isolated:local "$BUILD_CONTEXT"
```

读取本次构建的不可变镜像 ID 作为 `IMAGE_ID`。`MODEL_DIR` 为模型绝对路径；`RUN_OUTPUT` 必须尚不存在。`APPLICATION_HEALTH_URL`、`MODEL_HEALTH_URL` 分别指向日常服务，不是待测容器；异地主机需先建立仅回环的健康转发。本轮容器没有网络、没有发布端口，客户端通过 `docker exec` 在容器内访问 worker。

```sh
IMAGE_ID=$(cat "$BUILD_CONTEXT/image.id")
python3 llm-server/deploy/container_trial.py \
  --image "$IMAGE_ID" --model "$MODEL_DIR" \
  --manifest docs/benchmarks/2026-10-16-container-llm-input.json \
  --provenance docs/benchmarks/2026-10-16-qwen3-file-provenance.json \
  --application-health "$APPLICATION_HEALTH_URL" \
  --model-health "$MODEL_HEALTH_URL" --output "$RUN_OUTPUT"
```

控制器在哈希后、启动前、每次请求前和运行期间检查服务空闲；失败后不会因下一次恢复空闲而继续。探测有 5 秒整体期限，200 ms 是探测结束后的间隔，不是最大中止延迟。清理核对完整容器 ID、镜像 ID 和非空运行标签。若创建结果不明，必须根据记录检查该次容器状态，再决定下一次运行。

CPU 验证入口（客户端测试需要 HTTPX）：

```sh
python3 llm-server/deploy/test_container_guard.py
python3 llm-server/deploy/test_protocol_client.py
python3 llm-server/deploy/test_container_trial.py
python3 llm-server/deploy/test_health_probe.py
```

共 40 项本地 CPU 测试；宿主 Python 3.10 单独运行其中 27 项控制器/保护/HTTP 测试。另有 [真实 CPU Docker 故障演练](benchmarks/2026-10-16-container-guard.json)：占用和健康超时均中止并清理本轮容器，错误运行标签被拒绝。该演练中的容器在 5 秒停止期限后被强制结束，退出 137，不能称为优雅退出；正常模型批次退出为 0。

## 尚未覆盖

- ASR、应用、TTS、端到端语音与 2D/3D worker 的独立安装及组合启动。
- 全新主机和 GPU 驱动安装；公开可获取、可再分发的人物/声音/模型资源。
- 物理麦克风与扬声器回声、端到端声学延迟、人工动态音画质量。
- 更多真实语音/视觉输入和独立答案质量评测；当前结果不足以宣称 SOTA。
