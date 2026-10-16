# Qwen ASR 容器安装验证

2026-10-16：现有 Qwen ASR worker 在独立 Python 3.12.15 / Debian Bookworm 容器中完成 111 个锁定依赖的安装，实际 `Qwen3ASRModel` 导入和 3 项原有 WAV 协议测试通过。

**当前只完成安装与 CPU 协议验证，尚未在这个镜像中加载模型或进行 GPU 转写。** 之前的 [92 次独立虚拟环境请求](ASR-STANDALONE-VALIDATION.md)不能当作本镜像的推理证据。模型、SDK 与识别方法来自 Qwen 上游；本项目提供部署与协议验证。

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
| 运行隔离 | UID 65532、Python `-I`、只读文件系统、无网络、无 GPU；临时容器已移除 |
| 最终镜像 | `sha256:7cbd3e055a371d25f7ee387eed2e5021486429aa1910092d16024cb487316567` |

实际系统包为 libgomp1 / libstdc++6 `12.2.0-14+deb12u1`、libc6 `2.36-9+deb12u14`、sox `14.4.2+git20190427-3.5`。基础镜像与 APT 获取联网；只有 Python 安装步骤禁网。APT 仓库没有完整快照，不保证未来镜像 ID 相同。

[配方](../asr-server/deploy/Dockerfile)先从[原锁子集](../asr-server/deploy/build-bootstrap.lock)安装 setuptools，再以 `--no-build-isolation --require-hashes` 安装完整依赖，避免自动获取未锁定构建工具。没有使用 `--no-deps` 绕过 SDK 的安装依赖。sox 源码包官方哈希与本地生成 wheel 的哈希分别记录，后者不是官方发布物，也不意味着逐字可重复构建。

## 模型来源补充

应用当前配置与健康接口确认日常服务为 Qwen3-ASR-1.7B；后续 GPU 验证以该模型为主，沿用[既有 1.7B 固定版本和输入](ASR17-RECOVERY-VALIDATION.md)。本镜像安装的是通用 Qwen ASR SDK，CPU 测试没有加载任何模型。

另对历史 0.6B 基线补做来源核验：已有模型目录的 9 个文件全部匹配官方 revision `5eb144179a02acc5e5ba31e748d22b0cf3e303b0`。权重核对官方 LFS SHA256，普通 Git 文件核对包含 blob 头部的 Git SHA1，并另存本地 SHA256；每个文件同时核对长度。没有重新下载权重或更换模型。

这补齐了先前报告的模型来源缺口，但不改变先前采集器的 `model_configuration_complete=false`：历史健康接口没有完整记录解码器配置，本轮也还没有产生新的转写结果。

## 原始记录

- [原始分发文件及官方地址](benchmarks/2026-10-16-asr-container-distributions.json)
- [构建结果与源码哈希](benchmarks/2026-10-16-asr-container-build.json)
- [环境与测试结果](benchmarks/2026-10-16-asr-container-environment.json)、[实际 CPU 探针](../asr-server/deploy/probe_environment.py)
- [模型逐文件来源核验](benchmarks/2026-10-16-qwen3-asr-file-provenance.json)

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

## 下一步与边界

当前 1.7B 模型的 GPU 验证仍需固定真实 WAV 输入、日常应用/LLM/ASR 空闲保护、有界请求与失败清理，再完成 15 条合成音频 × 2 个术语配置 × 3 轮的正式请求及两次预热。已有 LLM 控制器只理解文字流事件，不能直接把它的测试通过当作 ASR 已受保护。

本阶段不证明中英混合识别率、真人语音质量、物理声学延迟、独立 GPU 主机安装或数字人全链路通过。原人工质量与物理设备验收仍待完成。
