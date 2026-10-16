# Qwen ASR 容器安装与真实推理验证

2026-10-16：现有 Qwen ASR worker 在独立 Python 3.12.15 / Debian Bookworm 容器中完成 111 个锁定依赖的安装，实际 `Qwen3ASRModel` 导入和 3 项原有 WAV 协议测试通过。

随后在该镜像中加载当前日常 Qwen3-ASR-1.7B，完成 **92 次真实 WAV 请求**：15 条既有合成输入 × 2 种术语配置 × 3 轮，加 2 次预热。与[此前 1.7B 独立环境结果](ASR17-RECOVERY-VALIDATION.md)相比，92 条文本逐字一致，评分一致；这是本镜像新执行的批次。模型、SDK 与识别方法来自 Qwen 上游；本项目提供部署、占用保护和证据采集。

## 已验证内容

| 检查 | 结果 |
| --- | --- |
| 基础镜像 | Python 3.12.15，固定摘要 `34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` |
| 原始分发文件 | 110 个 wheel、1 个 sox 源码包，共 2,995,922,136 字节；逐个匹配原依赖锁 SHA256 |
| 获取方式 | 58 个原始包复用缓存后重验，53 个从官方 PyPI 文件地址取得；未复制宿主已安装包 |
| 源码包构建 | 锁内 setuptools 81.0.0 作为 bootstrap；sox 1.5.0 在禁网安装步骤内构建 |
| Python 安装 | 全部包位于 `/opt/worker`，111 个版本与锁一致；`pip check` 通过 |
| 实际导入 | Torch 2.11.0+cu130、Transformers 4.57.6、Qwen3ASRModel、librosa、soundfile、sox |
| 原有协议测试 | 3 项通过：格式/静音、忙碌及异常恢复、术语长度及传递；使用测试 decoder，不是模型识别 |
| 运行隔离 | UID 65532、Python `-I`、只读文件系统、无网络；CPU 探针无 GPU，独立推理批次开放 GPU；临时容器均已移除 |
| 最终镜像 | `sha256:7cbd3e055a371d25f7ee387eed2e5021486429aa1910092d16024cb487316567` |

实际系统包为 libgomp1 / libstdc++6 `12.2.0-14+deb12u1`、libc6 `2.36-9+deb12u14`、sox `14.4.2+git20190427-3.5`。基础镜像与 APT 获取联网；只有 Python 安装步骤禁网。APT 仓库没有完整快照，不保证未来镜像 ID 相同。

[配方](../asr-server/deploy/Dockerfile)先从[原锁子集](../asr-server/deploy/build-bootstrap.lock)安装 setuptools，再以 `--no-build-isolation --require-hashes` 安装完整依赖，避免自动获取未锁定构建工具。没有使用 `--no-deps` 绕过 SDK 的安装依赖。sox 源码包官方哈希与本地生成 wheel 的哈希分别记录，后者不是官方发布物，也不意味着逐字可重复构建。

## 模型来源补充

应用当前配置与健康接口确认日常服务为 Qwen3-ASR-1.7B；GPU 验证沿用[既有 1.7B 固定版本和输入](ASR17-RECOVERY-VALIDATION.md)，revision 为 `7278e1e70fe206f11671096ffdd38061171dd6e5`。12 个模型文件在执行前后 SHA256 相同，均与既有官方来源清单一致。本镜像安装通用 Qwen ASR SDK；CPU 探针与真实 GPU 推理分别记录。

另对历史 0.6B 基线补做来源核验：已有模型目录的 9 个文件全部匹配官方 revision `5eb144179a02acc5e5ba31e748d22b0cf3e303b0`。权重核对官方 LFS SHA256，普通 Git 文件核对包含 blob 头部的 Git SHA1，并另存本地 SHA256；每个文件同时核对长度。没有重新下载权重或更换模型。

这补齐了先前报告的模型来源缺口，但不改变采集器的 `model_configuration_complete=false`：健康接口没有完整记录解码器配置。容器批次另行保存镜像、worker、采集代码和模型哈希，不能据此回填历史健康信息。

## 真实 GPU 批次

在现有 RTX 4090 主机运行，准入时有 18,801 MiB 空闲显存。应用、LLM、日常 ASR 三个健康接口接受启动前、请求前与后台检查，共 211 次；没有停止或切换日常服务。模型及 26 个采集载荷文件只读挂载，执行源码绑定已记录哈希。每次请求前需宿主准入，单请求上限 30 秒、加载上限 180 秒、整批上限 600 秒。

首轮在模型哈希阶段发现历史目录缺少第二个权重分片，**尚未创建容器或发送请求便退出**。在新建的测试模型目录复用其余 11 个文件，并补入此前取得的同版本分片（478,200,688 字节，SHA256 `6e0b9d9e09e2e0238e7ef3cc8a484ab387e91b90f1900bedf88bc92d7929ccfc`），重新核对全部 12 个哈希后运行成功。原目录未修改；[失败记录](benchmarks/2026-10-16-asr-container-admission-failure.json)保留，公开版仅去掉错误中的私有绝对路径。

成功批次产生 466 条事件，其中 187 份 checkpoint、92 条转写结果；客户端与容器退出码均为 0。容器已移除，清理错误为空；独立事后检查确认显存恢复至 18,801 MiB，三项日常服务仍就绪、空闲，专用健康转发已关闭。

| 配置 | 正式请求 | 与合成提示词的字符差异率 | WAV 请求耗时 P50 / P95 |
| --- | ---: | ---: | ---: |
| 无术语 | 45 | 18 / 804 = 2.24% | 115.3 / 185.0 ms |
| 有术语 | 45 | 0 / 804 = 0% | 114.2 / 187.6 ms |

计时从完整 WAV 的 HTTP 上传开始，到响应解析完成为止，排除两次预热；每组含 9 次静音请求，P50 为中位数、P95 为最近秩。此前独立环境对应 P50/P95 为无术语 107.1/174.9 ms、有术语 107.1/175.3 ms。本轮只是跨日期、不同部署环境的观察，不是控制变量性能实验，不能把差值归因于容器。

评分沿用原有规范化规则，**合成提示词不是真人转写标注**，0% 不代表真人中英混合识别无误。92 条文本与旧批次逐字一致，包括既有错误；本轮没有改善或改变识别算法。

GPU 之前还在实际镜像中演练了客户端完整运行和 SIGTERM：假服务完成 92 次 POST；另一轮在第二条准入前终止，仅发送第一条，保留 `ok/running` checkpoint 和元数据。强停的部分报告不能当作最终完成。此 CPU 演练与上述模型推理分别计数。

相关 CPU 回归共 76 项通过：采集/评分 22 项、ASR 控制器/客户端 12 项、共享 LLM 生命周期/准入/协议/HTTP 健康检查 42 项。三项 HTTP 测试需要本机 socket 权限，在允许回环监听的环境重跑通过。ASR 两个测试模块采用独立名称，修复整目录 discovery 与 LLM 同名测试冲突；该改动不涉及冻结的推理源码。

## 原始记录

- [原始分发文件及官方地址](benchmarks/2026-10-16-asr-container-distributions.json)
- [构建结果与源码哈希](benchmarks/2026-10-16-asr-container-build.json)
- [环境与测试结果](benchmarks/2026-10-16-asr-container-environment.json)、[实际 CPU 探针](../asr-server/deploy/probe_environment.py)
- [模型逐文件来源核验](benchmarks/2026-10-16-qwen3-asr-file-provenance.json)
- [冻结 GPU 输入与载荷](benchmarks/2026-10-16-asr-container-input.json)、[GPU 生命周期与哈希检查](benchmarks/2026-10-16-asr-container-validation.json)
- [原始事件](benchmarks/2026-10-16-asr-container-events.jsonl)、[完整采集报告](benchmarks/2026-10-16-asr-container-capture.json)、[逐项对照及耗时](benchmarks/2026-10-16-asr-container-comparison.json)
- [实际 CLI 中断演练](benchmarks/2026-10-16-asr-container-client-drill.json)、[事后服务与清理检查](benchmarks/2026-10-16-asr-container-postcheck.json)

## 复现入口

先按原始分发清单准备完整 `distributions/`，只放锁内原始 wheel 与 sox tar.gz，不能用本地重打包文件替换官方源码包。下载和传输后核对文件长度与 SHA256。保持 Python 3.12/Linux x86_64 目标；其他 ABI 未验证。

在仓库根目录构建最小上下文，`DISTRIBUTIONS_DIR` 是经过核验的包目录：

```sh
ASR_BUILD_CONTEXT=$(mktemp -d)
mkdir -p "$ASR_BUILD_CONTEXT/asr-server/deploy" "$ASR_BUILD_CONTEXT/distributions"
cp asr_server.py "$ASR_BUILD_CONTEXT/"
cp asr-server/requirements.lock "$ASR_BUILD_CONTEXT/asr-server/"
cp asr-server/deploy/Dockerfile asr-server/deploy/build-bootstrap.lock \
  "$ASR_BUILD_CONTEXT/asr-server/deploy/"
cp "$DISTRIBUTIONS_DIR"/* "$ASR_BUILD_CONTEXT/distributions/"
docker build --progress=plain --iidfile "$ASR_BUILD_CONTEXT/image.id" \
  -f "$ASR_BUILD_CONTEXT/asr-server/deploy/Dockerfile" \
  -t voxastage-asr-isolated:local "$ASR_BUILD_CONTEXT"
```

以下只运行 CPU 检查。`ASR_PROBE_DIR` 是临时探针目录；容器读模型包但不加载模型权重，不传 `--gpus`：

```sh
ASR_IMAGE_ID=$(cat "$ASR_BUILD_CONTEXT/image.id")
ASR_PROBE_DIR=$(mktemp -d)
cp asr-server/deploy/probe_environment.py test_asr_server.py "$ASR_PROBE_DIR/"
chmod 0755 "$ASR_PROBE_DIR"
chmod 0644 "$ASR_PROBE_DIR/probe_environment.py" "$ASR_PROBE_DIR/test_asr_server.py"
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,nosuid,size=256m --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount "type=bind,src=$ASR_PROBE_DIR,dst=/probe,readonly" \
  --entrypoint /opt/worker/bin/python "$ASR_IMAGE_ID" \
  -I /probe/probe_environment.py
```

探针目录需要可被容器 UID 65532 遍历。真实容器复现了默认 `mktemp` 0700 权限导致读取失败，并验证上述 0755/0644 配置可读，见[权限检查记录](benchmarks/2026-10-16-asr-container-probe-permissions.json)。这只给公开测试脚本增加读取权限。

GPU 验证入口为 `asr-server/deploy/container_trial.py`；`--help` 列出模型、镜像、输入 manifest、来源、载荷、输出目录及三个回环健康 URL。输出目录必须全新。冻结 manifest 要求原始音频字节与源码哈希一致；原 WAV 未随公开仓库发布，仅提供字节哈希和合成提示词身份，因此本报告不承诺从公开仓库独立重建完全相同的输入。替换输入应另建 manifest，不能沿用本批次成绩。

## 下一步与边界

已完成 ASR 独立 Linux 用户空间安装与直接 HTTP 推理验证；仍共享宿主内核和 GPU 驱动。剩余包括应用与其他 worker 的组合部署、独立 GPU 主机、更多真人语音、物理声学延迟与人工动态音画验收。本阶段不等于数字人全链路或识别质量达到 SOTA。
