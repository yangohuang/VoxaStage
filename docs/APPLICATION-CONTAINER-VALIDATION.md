# 应用容器安装与启动验证

2026-10-16：VoxaStage 应用在独立 Python 3.11.17 / Debian Bookworm 容器中完成安装，**292 项应用测试、34 项评测工具测试及实际 HTTP 启动检查通过**。508 个导出源码文件执行前后哈希不变。未复制开发环境、配置、会话、模型权重或运行时人物素材；公开文档截图仍在源码包内。

这补齐了[此前新虚拟环境检查](SOURCE-INSTALL-VALIDATION.md)未覆盖的独立 Linux 用户空间。此次模型地址为空或指向容器内关闭的回环端口，没有访问日常模型服务，没有 GPU、外部网络或宿主端口映射。它证明应用安装与无模型启动，不代表真实对话、浏览器麦克风、WebRTC 媒体连接或全新 GPU 主机验收。

## 安装输入与来源

| 项目 | 实际结果 |
| --- | --- |
| 基础镜像 | 官方 Python 3.11，摘要 `0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89`；实际 Python 3.11.17 |
| 应用依赖 | 保留根目录 100 个版本锁；99 个官方 wheel、1 个 docopt 源码包，共 257,383,797 字节 |
| 文件哈希 | 从固定版本 PyPI API 取得官方文件信息，下载后逐一核对长度和 SHA256；新增目标平台补充锁 |
| 构建工具 | setuptools 81.0.0 独立 bootstrap，不计入原 100 个应用依赖 |
| Python 安装 | 独立 `/opt/application`；禁网安装、`--require-hashes`、完整依赖解析及 `pip check` 通过 |
| NLTK | punkt_tab.zip 沿用原安装脚本 SHA256 `e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106` |
| 系统库 | libgomp1 / libstdc++6 `12.2.0-14+deb12u1`，libc6 `2.36-9+deb12u14` |
| 实际导入 | Pipecat、PyAV、soundfile、ONNX Runtime、NLTK；实际构造 Silero VAD 并加载其随包模型 |
| 运行身份 | UID 65532，验证主进程 Python `-I`；测试/应用子进程从明确的应用目录加载源码 |
| 最终镜像 | `sha256:c5892ec958ab8f20f3718934b58b4d408372bd4a813921c9b4a4f7c5d463bd70` |

根 `requirements.lock` 原先只锁版本，没有文件哈希。本轮没有声称它原来具备哈希校验，也没有升级依赖；补充锁为 Linux x86_64 / 固定 Python 3.11 镜像选择的具体发行文件，不是所有平台通用锁。

docopt 的原始源码包哈希与本地构建 wheel 哈希分别记录。实际 wheel 为 `docopt-0.6.2-py2.py3-none-any.whl`，13,781 字节，SHA256 `e29fbf1133901824b02d1552fb53f4c2a83d15a49a1c092559c2b85e5aa29972`。这不是官方发布的 wheel，也不承诺每次构建字节相同。基础镜像与 APT 获取联网，Python 安装步骤禁网；APT 没有完整仓库快照，因此不承诺未来镜像 ID 相同。

## 实际验收与保留问题

验证容器限制为 2 个 CPU、4 GiB 内存，没有 GPU；根文件系统只读，`/tmp` 与应用 runtime 使用临时内存文件系统，结果写入独立输出目录。现有 HTTP/WebSocket 路由测试使用测试后端，不把它们记作真实模型推理。

实际 uvicorn 子进程通过六个路由检查：`/avatar`、三个静态文件、`/avatar/providers`、`/health`。没有模型配置时，providers 不宣称已配置，health 明确报告未就绪。随后探针发送 SIGTERM，日志确认应用关闭完成且进程结束，未使用强杀。锁定的 Uvicorn 0.53.0 在完成清理后重新触发信号，实际退出码为 `-15`；探针仅在这些条件同时满足时接受该退出码。容器自身退出码为 0，已移除。

首次实际回归运行了 292 项，失败 1 项：测试将虚拟环境解释器软链接写死为 `/usr/bin/python3`，而官方镜像使用 `/usr/local/bin`，该链接不存在。应用正确回退至当前解释器。仅把测试目标改为 `sys.executable` 后重新构建源码层，完整测试通过；未修改应用行为、模型或依赖版本。失败报告保留，未混入成功批次计数。

初始依赖构建成功后，因修正关闭检查、加入源码清单及修复上述测试，后续镜像复用了同一依赖层，刷新源码层。报告分别记录实际镜像和源码摘要，不把多个尝试合并为一次无失败运行。

## 可复现入口

[发行获取脚本](../deploy/application/acquire_distributions.py)、[Dockerfile](../deploy/application/Dockerfile)和[实际探针](../deploy/application/probe_environment.py)均随源码包发布。以下从仓库根目录执行，需要 Docker BuildKit 和本机 Python；不需要宿主安装应用 Python 包。

```sh
APP_STAGE=$(mktemp -d)
chmod 0755 "$APP_STAGE"
mkdir "$APP_STAGE/input" "$APP_STAGE/output"
cp requirements.lock "$APP_STAGE/input/"
cp deploy/application/acquire_distributions.py "$APP_STAGE/input/"
chmod 0644 "$APP_STAGE/input/"*
APP_BASE=python@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
docker run --rm --read-only --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,nosuid,size=256m --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount "type=bind,src=$APP_STAGE/input,dst=/input,readonly" \
  --mount "type=bind,src=$APP_STAGE/output,dst=/output" \
  "$APP_BASE" python /input/acquire_distributions.py
cmp deploy/application/requirements.container.lock "$APP_STAGE/output/requirements.container.lock"
cmp deploy/application/build-bootstrap.lock "$APP_STAGE/output/build-bootstrap.lock"
```

上一步访问官方 PyPI 及 NLTK 地址。使用新输出目录；如网络失败保留失败记录，另建目录重试。`cmp` 不同则先检查上游变化，不能直接覆盖本次补充锁。

```sh
mkdir "$APP_STAGE/context" "$APP_STAGE/context/source"
python export_demo.py --output "$APP_STAGE/source.tar.gz"
tar -xzf "$APP_STAGE/source.tar.gz" --strip-components=1 -C "$APP_STAGE/context/source"
cp "$APP_STAGE/source.tar.manifest.json" "$APP_STAGE/context/source.manifest.json"
cp deploy/application/Dockerfile "$APP_STAGE/context/"
cp "$APP_STAGE/output/requirements.container.lock" "$APP_STAGE/output/build-bootstrap.lock" \
  "$APP_STAGE/output/punkt_tab.zip" "$APP_STAGE/context/"
cp -R "$APP_STAGE/output/distributions" "$APP_STAGE/context/"
docker build --progress=plain --iidfile "$APP_STAGE/image.id" \
  -t voxastage-app-isolated:local "$APP_STAGE/context"
```

构建输入只使用导出源码包，不要用开发目录整体替代 `source/`。镜像不含模型、私人配置或运行时人物素材。此时可执行 CPU 验收：

```sh
APP_IMAGE_ID=$(cat "$APP_STAGE/image.id")
APP_PROBE_OUTPUT=$(mktemp -d)
# 独立测试输出目录需允许镜像的 UID 65532 写入；不放凭据或私有素材。
chmod 1777 "$APP_PROBE_OUTPUT"
docker run --rm --read-only --network none --cpus 2 --memory 4g \
  --tmpfs /tmp:rw,nosuid,size=512m \
  --tmpfs /opt/app/runtime:rw,nosuid,size=64m,uid=65532,gid=65532,mode=0700 \
  --cap-drop ALL --security-opt no-new-privileges \
  --mount "type=bind,src=$APP_PROBE_OUTPUT,dst=/output" \
  --entrypoint /opt/application/bin/python "$APP_IMAGE_ID" \
  -I /opt/app/deploy/application/probe_environment.py
```

`result.json` 与完整日志保留在输出目录。源码清单和被复制文件逐个校验，任何应用测试或 HTTP 检查失败都会返回非零。宿主 `--rm` 清理临时容器，探针负责关闭它启动的应用子进程。镜像默认入口为 uvicorn，后续真实模型网络配置及组合会话验收另行进行；不要将当前无模型检查当作完整部署。

## 原始证据与边界

- [官方分发文件及来源](benchmarks/2026-10-16-application-container-distributions.json)、[构建与本地 wheel 记录](benchmarks/2026-10-16-application-container-build.json)
- [完整环境与 HTTP 检查](benchmarks/2026-10-16-application-container-environment.json)、[实际源码清单](benchmarks/2026-10-16-application-container-source.json)
- [292 项应用测试日志](benchmarks/2026-10-16-application-container-application-tests.txt)、[34 项评测测试日志](benchmarks/2026-10-16-application-container-evaluation-tests.txt)、[启动与关闭日志](benchmarks/2026-10-16-application-container-server.txt)
- [首次探针失败记录](benchmarks/2026-10-16-application-container-first-probe.json)

源码清单对应测试当时的 508 文件，不含随后新增的本报告及其证据。未进行浏览器渲染或前端 Node 测试，也未验证音画质量、真人语音识别、真实工具调用/推理组合或物理声学延迟。应用、ASR 和 LLM 的独立镜像证据不能自动合成为全链路验收；下一步应连接所需 worker 验证真实流式会话，继续保留原人工质量和新 GPU 主机门槛。
