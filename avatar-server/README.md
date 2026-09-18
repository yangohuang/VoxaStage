# StreamingTalker 本地增量服务 overlay

这是数字人演示所用的**本地服务扩展**，不是官方自带 HTTP/WebSocket API。
它在 [StreamingTalker 上游](https://github.com/zju3dv/StreamingTalker)模型源码上
增加增量 PCM 输入、状态保留、队列上限和合作式取消。浏览器看到的是无真人纹理的
3D 头部网格。上层 Pipecat 客户端参见 [avatar_backend.py](../avatar_backend.py)，
服务协议见 [PROTOCOL.md](PROTOCOL.md)。

## 文件范围和源码基线

目录只包含 7 个 `serving/*.py`、3 个 CPU 测试、依赖清单、无头运行补丁、文档、
许可证及源码 SHA256。没有模型权重、人物 mesh/模板、照片、音色、录音、推理产物、
本机路径/IP 配置或 systemd 单元；没有自动下载模型或素材的脚本。
`api.py` 是增量接口共享准入锁所需的 HTTP 适配器，不依赖 `serving/render.py`，
因此未打包离线渲染代码及其 Blender/OpenGL 依赖。

固定上游基线：
[25b613ac273624947e5fa51c4c41c4933d8147be](https://github.com/zju3dv/StreamingTalker/tree/25b613ac273624947e5fa51c4c41c4933d8147be)。
该提交的 [LICENSE](https://github.com/zju3dv/StreamingTalker/blob/25b613ac273624947e5fa51c4c41c4933d8147be/LICENSE)
与附带历史 Apache-2.0 文件逐字一致。核对时上游后续提交 `1f645275…` 仅修改
LICENSE，已采用不同许可证，安装时不要用浮动 `main` 代替此基线。

原本地模型目录没有 `.git`，无法追认其真实 checkout 历史。因此这里报告的是
**文件内容比对结果**：全部本地 `algorithms/`、`configs/`、`utils/` 的 Python/YAML，
加 `requirements.txt` 和 LICENSE，共39项；38项与固定上游完全一致，唯一模型差异
是 `algorithms/models/diff_ar.py` 延迟导入可选可视化模块。提供的补丁复现该差异并
加入变更说明注释。详见 [upstream-comparison.json](upstream-comparison.json)、
[NOTICE.md](NOTICE.md)。

- `BASELINE-SOURCE.sha256`：未打补丁的固定上游核心文件指纹（不含训练专用文件），可在安装时核验。
- `MODEL-SOURCE.sha256`：原本地实际模型源文件指纹；仅供审计，不代表安装后的
  所有文件，因为导出补丁还增加了一行归属说明。
- `upstream-comparison.json`：每个文件的上游/本地指纹，以及应用补丁后的模型指纹。

## 准备上游代码和 Python 环境

以下从此演示项目根目录开始，仅拉取上游代码目录，不拉取演示音频或模型素材：

```sh
AVATAR_OVERLAY="$(pwd)/avatar-server"
git clone --filter=blob:none --no-checkout https://github.com/zju3dv/StreamingTalker.git StreamingTalker
cd StreamingTalker
git sparse-checkout init --cone
git sparse-checkout set algorithms configs utils
git checkout --detach 25b613ac273624947e5fa51c4c41c4933d8147be
sha256sum -c "$AVATAR_OVERLAY/BASELINE-SOURCE.sha256"
git apply --check "$AVATAR_OVERLAY/headless-model.patch"
git apply "$AVATAR_OVERLAY/headless-model.patch"
cp -R "$AVATAR_OVERLAY/serving" ./serving
```

上游环境和完整训练依赖见
[固定版本 README](https://github.com/zju3dv/StreamingTalker/blob/25b613ac273624947e5fa51c4c41c4933d8147be/README.md)
及其 `requirements.txt`。本地演示所用组合是 Python 3.10、PyTorch 2.5.1/CUDA12.4，
与上游文档列出的 PyTorch2.4 不同。下面提供现有运行环境中的直接依赖版本，
用于推理服务；不要求安装上游完整训练/可视化栈：

```sh
python3.10 -m venv .venv
. .venv/bin/activate
python -m pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -r "$AVATAR_OVERLAY/deploy/requirements-serving.txt"
python -m pip check
```

这是安装说明和直接依赖清单，**不是全新机器验证过的安装器，也不是完整传递依赖锁**。
CUDA驱动、系统动态库及包解析结果仍须在目标机器核验。现有
`requirements-incremental.txt` 只增加 `websockets==15.0.1`，不能单独安装整个服务。
无头补丁避免推理导入 Blender；可选离线可视化仍需上游自己的可视化依赖。

## 自行准备获准使用的权重和模板

按照上游安装说明，使用各发布方渠道自行取得文件。本包不包含或自动下载它们，
代码的历史 Apache-2.0 许可不说明这些文件的授权范围。

| 上游根目录中的目标路径 | 来源/用途 |
| --- | --- |
| `checkpoints/diffar_voca_241120.ckpt` | 上游说明中的 `youtopia/StreamingTalker` 主模型 |
| `checkpoints/voca_vae.ckpt` | 同一模型发布中的 VQ-VAE |
| `checkpoints/hubert-base-ls960/` | `facebook/hubert-base-ls960` 的完整本地 Transformers 模型目录 |
| `data/vocaset/templates.pkl` | 经授权取得的 VOCASET 模板字典 |
| `data/vocaset/templates/FLAME_sample.ply` | 上游指定的 FLAME 三角形拓扑 |
| `data/vocaset/FLAME_masks.pkl` | 模型损失类初始化所需 lips 索引，即使仅推理也会读取 |

素材准备入口：[上游说明](https://github.com/zju3dv/StreamingTalker/blob/25b613ac273624947e5fa51c4c41c4933d8147be/README.md#dataset)、
[VOCASET](https://voca.is.tue.mpg.de/)。模型权重及音频编码器同样需按各自发布方条款准备。
目录名和模板键必须与 `serving/engine.py` 对应；默认使用其 `SUBJECTS[0]`。
该接口没有运行时切换人物功能。模板和 checkpoint 会被 pickle/torch 加载，
只能使用可信来源的文件。

## 运行和连接

从准备好的上游根目录、已激活的模型环境运行：

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m serving.incremental_server --host 127.0.0.1 --port 12544
```

离线环境变量让缺少本地 HuBERT 文件时直接失败，不尝试补下载。服务在启动时加载
一次真实模型，默认 CUDA，只运行一个 worker。`GET /health` 和
`GET /v1/stream/info` 可检查能力和 busy 状态；增量入口为
`ws://127.0.0.1:12544/v1/stream`。Pipecat 适配器连接该地址，浏览器仍连接上层
`/avatar/ws`，两者协议不同。

客户端先发 `start`，双向并发传送16kHz单声道 PCM16LE 和接收网格帧，再发 `end`。
`cancel`/断连停止后续队列处理，但不能中断已进入的单次模型调用；GPU占用锁会保留到
推理和清理结束。完整事件、上限和时间戳口径见 [PROTOCOL.md](PROTOCOL.md)。
这是对已发布非因果模型的有限上下文适配，窗口归一化/上下文改变会影响输出；
不宣称与完整音频推理逐帧相同，也不宣称达成生产实时性能。

## 本次验证与未覆盖范围

从 overlay 目录可以直接运行 CPU 测试，不需要权重、素材或上游源码：

```sh
python -m pip install -r deploy/requirements-tests.txt
PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=4 python -m pytest -p no:cacheprovider tests -q
```

该命令仍需已经安装 PyTorch、NumPy、FastAPI、SoundFile、SciPy 等运行依赖。
本次在现有 Python3.10环境运行：**46 passed，1个第三方弃用警告，2.56秒**。
覆盖时钟、分包不变性、状态/随机数保留、有界历史、协议校验、背压、单worker准入、
取消和断连清理。FastAPI TestClient 在受限沙箱的线程唤醒路径卡住；同一测试在
获准解除沙箱限制后通过，没有加载真实模型或开启常驻服务。

另已验证：固定上游源码文件指纹；补丁 `git apply --check` 和实际应用；
overlay全部模块及补丁后的上游 `DIFF_AR` 在CPU环境可导入，未实例化模型。
没有在全新机器重建环境，没有在这份导出目录运行真实GPU推理，没有测试未指定的
PyTorch/CUDA/Transformers版本，也没有验证素材授权或素材在第三方机器上的可获得性。
