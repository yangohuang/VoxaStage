# ASR 隔离容器安装计划

执行方式：沿用本会话分阶段执行与审查，不启动新的用户任务。完整数字人目标保持不变。

**目标：**在固定 Python 3.12 Linux 用户空间安装现有 ASR 的 111 个锁定包，覆盖源码包构建与系统库依赖，随后对真实模型进行独立协议验证。

**架构：**使用与 LLM 验证相同的摘要固定基础镜像，但建立独立 `/opt/worker`，不继承 LLM 镜像的 Python 包。复用原始分发文件缓存时逐个检查原 ASR 锁内哈希；缺失包仅从官方索引取得。模型和测试 WAV 在 GPU 阶段只读挂载。

**技术：**Docker BuildKit、Python 3.12.15、当前日常 Qwen3-ASR-1.7B、原始 SHA256 锁、原有 WAV 转写与采集器。

## 1. 核对基线与安装输入

- [x] 工作树从 PR29 主分支 `912819f` 创建，原有 ASR 3 项协议测试通过。
- [x] 原锁含 111 个包。`sox==1.5.0` 只有锁定的源码包；已核对 SHA256 和 setup.py，它使用 setuptools 构建，运行依赖 numpy 与 typing-extensions。
- [x] 独立 CPU 容器查询 Bookworm 系统包：libgomp1 `12.2.0-14+deb12u1`、sox `14.4.2+git20190427-3.5`。
- [x] 冻结官方分发文件清单，按目标 Python 3.12/amd64 标签选 wheel，无 wheel 时只接受锁定源码包。记录文件名、大小、SHA256、来源 URL 和缓存/下载来源。
- [x] 下载与传输后重验全部文件；任何不匹配均失败，不修改原始锁。

## 2. 构建与环境检查

新增 `asr-server/deploy/Dockerfile`，完整配方：

```dockerfile
# Minimal context: this recipe, original worker/lock, bootstrap lock, and verified
# original distributions. Python installation cannot fetch unpinned build tools.
FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1=12.2.0-14+deb12u1 sox=14.4.2+git20190427-3.5 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/worker
COPY asr-server/requirements.lock /opt/requirements.lock
COPY asr-server/deploy/build-bootstrap.lock /opt/build-bootstrap.lock
RUN --network=none --mount=type=bind,source=distributions,target=/opt/distributions,readonly \
    /opt/worker/bin/python -m pip install --no-index --find-links /opt/distributions \
      --require-hashes -r /opt/build-bootstrap.lock \
    && /opt/worker/bin/python -m pip install --no-index --find-links /opt/distributions \
      --no-build-isolation --require-hashes -r /opt/requirements.lock \
    && /opt/worker/bin/python -m pip check
COPY asr_server.py /opt/app/asr_server.py
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
ENV USER=voxastage HOME=/tmp NUMBA_CACHE_DIR=/tmp/numba-cache
ENV OMP_NUM_THREADS=2 NUMBA_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
USER 65532:65532
ENTRYPOINT ["/opt/worker/bin/python", "-I", "/opt/app/asr_server.py"]
```

`asr-server/deploy/build-bootstrap.lock` 仅逐字提取原锁中的 setuptools 条目。先安装它，使 sox 源码包可在 `--no-build-isolation` 下构建；全步骤无网络，不自动解析未锁定构建工具。若实测还需要构建依赖，保留失败并据实际声明修正，不能绕过哈希或跳过 SDK 依赖。

- [x] 独立复核配方；分发清单由获取器逐项按锁核验；最小上下文只含上述文件、原 worker/锁和分发文件。
- [x] 执行 `docker build --progress=plain -f asr-server/deploy/Dockerfile --iidfile image.id -t voxastage-asr-isolated:local .`，保存完整日志、构建输入哈希与不可变镜像 ID。
- [x] CPU 探针检查 111 个版本/包路径、pip check、Python 隔离、数字用户、系统依赖；导入实际 `Qwen3ASRModel` 与音频处理库，不只检查顶层包。运行原有 ASR 协议测试。
- [x] 显式区分官方源码包哈希与本地构建 wheel；安装成功不等于模型推理成功，也不保证镜像逐字重建。

## 3. 真实模型验证前的必要检查

- [ ] 核验当前日常 1.7B 的 12 个模型文件及既有官方 revision `7278e1e70fe206f11671096ffdd38061171dd6e5`，冻结 15 条既有合成 WAV、两个术语配置、三轮共 90 次正式请求加两次预热；原音频不公开，保留字节哈希与合成提示词身份。
- [x] 使用已有占用保护和按完整容器身份清理逻辑，给 ASR 请求、模型加载、整体批次设置期限，先测试失败和清理路径；不得直接复用仅理解 LLM 事件的控制器来假称 ASR 已受保护。
- [ ] 启动前与运行期间检查日常应用、LLM、ASR 空闲及显存；不停止其他服务。GPU 准入不足则等待，不能消耗旧的停服授权。
- [ ] 原采集器运行真实 WAV 请求，保留每次结果、失败和输入/context 哈希；运行前后模型哈希一致，临时容器清理后原服务仍正常。

## 4. 交付与未覆盖项

- [ ] 发布实际安装/推理证据与失败记录，经独立复核、文本脱敏、导出检查后提交 PR。
- [ ] 继续应用与其余 worker 的独立安装、组合启动、独立 GPU 主机、物理声学和人工动态质量验收。

本阶段不改 ASR 算法，不把合成提示词当真人标注，不因协议通过宣称识别率提升。容器仍共享宿主内核和 GPU 驱动，不冒充整站或新主机验收。

安装结果与仍未完成的 GPU 阶段见[安装报告](../ASR-CONTAINER-VALIDATION.md)。

实际应用配置与健康接口再次确认日常 ASR 是 1.7B；0.6B 的本轮 9 文件来源核验仅补历史基线，不替代日常模型的 GPU 验证。CPU 镜像和锁为两者共用 SDK，不涉及模型切换。

## GPU 采集实现补充

复用已验收的容器生命周期与事件监督器；基类仅提取 `create_args()` 并增加可选整批截止时间。ASR 适配器独立检查应用、LLM、ASR 三个回环健康接口（单次整体 5 秒期限），核对当前 1.7B 的 12 个模型文件和只读采集载荷，使用 ASR 启动参数。

原 `evaluation/asr_capture.py` 增加可选异步 observer：上传前等待宿主准入，完成后输出实际 trial；默认调用行为保持。ASR 客户端逐项匹配冻结的 phase/repeat/case/variant/audio/context 哈希，首错停止，保留完整采集报告和逐条事件。 stdin 准入复用原有 20 秒有界读取。

固定清单见 `docs/benchmarks/2026-10-16-asr-container-input.json`：92 次请求、26 个载荷文件哈希。加载 180 秒、单请求 30 秒、整批监督 600 秒；这些是超时上限而非性能目标。真实执行前须独立复核，并验证容器内实际客户端入口，不能只测试模拟回调。

实际镜像内的 CPU CLI 演练已完成：假 HTTP 服务下执行 92 次上传，输出 187 份完整元数据 checkpoint；另一轮在第二条请求准入前向客户端发送 SIGTERM，仅上传第一条，末 checkpoint 保留 `ok/running` 两条状态。强停后的 `running` 报告是部分证据，应结合宿主失败状态解释，不能称为完整终态。演练容器已清理；这些请求不计入模型成绩。对应 JSON 与演练源码位于 `docs/benchmarks/2026-10-16-asr-container-client-drill.*`。

新增 checkpoint 在原采集器每次持久化后同步输出。载荷验证要求七份执行源码、cases/variants 与全部引用 WAV，并将源码哈希绑定宿主记录值；缺少文件或自行改写 manifest 不能绕过来源核验。
