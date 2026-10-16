# 隔离GPU部署复现计划

**目标：**逐项补齐独立安装与真实后端联动证据，保留人工音画、物理声学、真实输入和空白主机验收范围。先完成日常Qwen3-4B的干净容器安装，再推进其余后端与应用；单worker通过不代表整站部署完成。

**方法：**在现有授权GPU主机上使用固定摘要的独立Linux用户空间，每个worker拥有自己的依赖环境，代码烘焙进镜像，模型作为经过核验的只读数据挂载。宿主Python包、Conda目录和日常服务配置不进入容器。已有驱动仍由宿主提供，需要记录其版本；这不能冒充空白物理主机与新驱动安装。

**技术：**Docker、官方Python3.12 Debian Bookworm基础镜像、现有完整SHA256依赖锁、NVIDIA容器运行时、现有流式HTTP worker。

## 选择及原目标边界

全新物理主机或虚拟机可覆盖更完整的主机安装边界，但当前没有新的空白主机。继续复用宿主虚拟环境不能填补系统依赖边界。选择干净容器作为可立即执行的一步，完成后仍明确披露共享内核和驱动。

安装检查先覆盖日常LLM，再依次覆盖ASR、应用和具备公开安装路径的数字人/端到端worker。IndexTTS目前只公开兼容层，DINet使用既有兼容部署服务；不得把复用其现有服务算作完整模型重新安装。需要分别核对可用源码、模型、人物和声音资源，保留无法复现的依赖，不以LLM单项结果替代全部链路。

不使用未通过推进门槛的Instruct候选。日常服务不切换、不停止；临时容器不发布端口，测试客户端在同一容器内访问localhost。GPU准入不足或日常会话占用时等待，不临时停止其他服务。

## 已验证的准备

- 固定基础镜像：`python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258`，amd64，Python3.12.15。
- 在独立容器核对：基础镜像没有libgomp；Debian Bookworm仓库提供`libgomp1=12.2.0-14+deb12u1`。最初对APT预检去掉全部Linux能力，导致其无法切换下载用户；改为普通Docker权限后查询成功，无宿主改动。运行时仍可去掉全部能力。
- 另一个固定上游PyTorch基础镜像完成单元素CUDA探针；它的Python3.11/Torch2.6不用于本轮锁安装，也不是项目模型推理证据。
- LLM继续使用仓库锁定的Python3.12 ABI依赖：torch2.11.0、transformers5.12.1、bitsandbytes0.50.2、accelerate1.14.0。不修改锁来绕过安装失败。

## 第一步：构建与安装隔离

文件：`llm-server/deploy/Dockerfile`、`llm-server/README.md`；继续使用根目录`llm_server.py`和`llm-server/deploy/requirements-standalone.lock`。

- [x] 创建独立工作树，核对基线`efe0234`与镜像摘要。
- [x] 添加Dockerfile，完整内容如下；构建上下文只包含该文件、原始worker与依赖锁，不上传整个运行目录。

```dockerfile
FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1=12.2.0-14+deb12u1 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/worker
COPY llm-server/deploy/requirements-standalone.lock /opt/requirements.lock
RUN /opt/worker/bin/python -m pip install --timeout 120 --no-cache-dir --require-hashes -r /opt/requirements.lock \
    && /opt/worker/bin/python -m pip check
COPY llm_server.py /opt/app/llm_server.py
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["/opt/worker/bin/python", "-I", "/opt/app/llm_server.py"]
```

- [ ] 对构建文件独立复核。执行`docker build --progress=plain -f llm-server/deploy/Dockerfile -t voxastage-llm-isolated:20261016 .`；保存完整日志、输入文件哈希和最终镜像ID，失败保留日志，不改宿主依赖。
- [ ] 在镜像内检查`/opt/worker/pyvenv.cfg`的`include-system-site-packages=false`、锁定包版本与实际发行包位置、`pip check`、系统包清单及Python版本。包必须来自`/opt/worker`，基础pip单独记录，不把它误算为模型锁中的包。记录固定Python补丁版本与原宿主不同。

## 第二步：真实推理与协议回归

- [ ] 加载前检查日常应用空闲和至少8GiB可用显存。先核对只读Qwen3-4B模型的10个核心文件，使用已有官方固定版本来源；哈希完成后、容器启动前再次准入。宿主包装器在每次请求前和生成期间每200ms检查日常应用active_sessions=0、LLM busy=false及健康就绪；检查异常或重新占用时立即中止本轮，保存已有记录，只停止本轮身份匹配的容器。容器没有网络，健康检查由宿主通过仅回环转发完成；不重新下载或改变权重。GPU阶段启动前须以故障注入验证该停止契约。
- [ ] 用镜像ID启动专用有标签的临时容器，参数为`--gpus all --network none --read-only --tmpfs /tmp:rw,nosuid,size=256m --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src="$MODEL_DIR",dst=/model,readonly`，worker参数为`--model /model --port 18311 --quantization 4bit`。本地变量`MODEL_DIR`仅表示部署者提供的已核验模型路径；不把机器路径发布到仓库。
- [ ] 通过`docker exec`在容器内检查健康与真实NDJSON。固定复用2026-10-10协议输入：1预热、中文/英文/中英混合各3次、1长回复、3组关闭流/恢复，共17次请求。[输入清单](../benchmarks/2026-10-16-container-llm-input.json)在测试前保存；保存所有增量、错误、时间和恢复证据。每请求90秒上限，加载180秒上限，恢复20秒上限。任何失败均停止并保留，不按答案筛选重跑。
- [ ] 长回复需要多个文字片段且首片段早于整轮结束。关闭流时先观察busy，等待锁释放，再验证下一请求成功；没有内部停止事件计数，不声称已证明GPU取消执行。协议通过不等于回答质量通过。
- [ ] 无论成功失败，都只停止本轮匹配镜像与标签的临时容器，保存退出状态、日志和模型只读核验；确认原有服务仍正常。不要停止或清理其他容器、共享镜像和卷。

## 第三步：报告与继续推进

- [ ] 发布实际镜像/环境/输入/协议结果，独立复核、脱敏检查后通过PR交付；仅描述实测范围。
- [ ] 对余下ASR、应用、端到端、2D/3D驱动和TTS逐项推进安装与组合启动，避免通过单项冒充完整部署。继续保留物理设备与人工质量所需证据。

首次构建在下载 Python 包时因读取超时失败，完整日志保留。确认进程已经退出后，仅将 pip 下载读取超时调整为120秒，再进行一次有界重试；锁文件和 worker 不变。尚未运行上述17次请求，不将准备状态记为部署成功。

持续占用保护与按完整容器ID、镜像ID、非空运行标签核验的清理原语已实现，12项CPU测试通过；审查发现的空标签漏洞已新增负例并修复。下一步用不占GPU的真实Docker故障演练验证这两项原语，随后再将其接入完整推理包装器。原语通过不等于完整包装器或GPU测试已通过。后续任务按此计划在当前会话顺序执行，不额外创建其他任务窗口。

## 下载故障恢复与保护验证

第一次构建因读取超时失败；第二次把读取超时增至120秒后，423.1MB的
`nvidia-cublas` wheel只收到约231.8MB，哈希校验拒绝安装。两次退出码均为1，
日志保留，不改锁内哈希来接纳不完整文件。

已有10个同版本原始wheel共2,525,741,588字节，逐个SHA256均在现有锁内。
使用独立`Dockerfile.cached-wheels`把这些文件作为BuildKit只读挂载，
`--find-links`提供下载缓存，其余小包照常联网下载。安装仍有完整
`--require-hashes`与`pip check`；不复制宿主Python包或Conda环境，
不把wheel写进最终镜像层。传输后须再次验证大小和哈希。该恢复方案
静态审查通过，构建与真实模型推理结果分别记录。

占用与健康超时两项真实CPU Docker故障演练均通过：错误运行标签的清理
被拒绝，partial日志保留，失败不会恢复成可继续状态，本轮容器均已移除。
Docker在5秒停止期限后升级终止，退出137；这证明有界强停，不是优雅退出。
完整推理控制器、TERM/HUP和创建超时处理仍待实现和验证。
