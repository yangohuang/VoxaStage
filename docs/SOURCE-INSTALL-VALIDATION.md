# 源码包独立安装检查

2026-09-19，从提交 `a11efa29ee8b4915e31b46cc2079c5d97578d176` 导出的197文件源码包，在独立临时目录创建全新虚拟环境，原样执行 `bash install_demo.sh`。安装、测试及应用启动检查通过。[机器记录](benchmarks/2026-09-19-source-install.json)绑定源码包、安装日志、测试日志和验证脚本摘要。

这是现有 Linux x86_64 主机上的新应用环境验证。复用了系统库、Python和Node，未验证空白操作系统、容器或GPU模型全量安装。未复制开发目录中的 `.venv`、运行配置、模型权重或人物素材。

## 实测结果

| 检查 | 结果与范围 |
| --- | --- |
| 安装 | Python 3.11.15、uv 0.11.31；100个安装版本逐项匹配锁文件，`uv pip check`通过 |
| NLTK | 安装脚本下载并校验分句资源，运行时从新环境的 `.venv/nltk_data` 找到资源 |
| 源码隔离 | 检查结束后，197个导出文件摘要仍与源包清单完全一致 |
| 应用测试 | 147项通过，包括真实本机HTTP/WebSocket路由和模拟模型接口 |
| worker契约测试 | 11项通过；未加载真实模型 |
| 评测工具测试 | 12项通过，包含几何与多模态评测 |
| 前端模块测试 | 33项通过；本轮不包含浏览器渲染测试 |
| 应用启动 | 新环境启动 `uvicorn bot:app`，`/avatar`、3个静态资源、`/avatar/providers`及`/health`均返回200 |
| 能力状态 | 数字人和MiniCPM未配置；级联后端指向关闭的本机端口，`/health`明确返回未就绪 |
| 清理 | 两次启动的临时应用进程均已终止，日志显示完成应用关闭 |

所有模型连接均被清空或指向本机关闭端口，避免此次安装验证使用已部署的推理服务。应用能展示页面与模型能生成有效回复是两项不同验收。

## 复现步骤

从源码包解压后的项目目录开始，按[部署说明](DEPLOYMENT.md)安装。测试只需要应用环境；真实模型部署按各worker文档单独进行。

```bash
bash install_demo.sh
export NLTK_DATA="$PWD/.venv/nltk_data"
export NUMBA_CACHE_DIR="$PWD/runtime/numba-cache"
export OMP_NUM_THREADS=2
mkdir -p "$NUMBA_CACHE_DIR"
.venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
.venv/bin/python -m unittest discover -s omni-server -p 'test_*.py' -v
.venv/bin/python -m unittest discover -s evaluation -p 'test_*.py' -v
node --test avatar-web/tests/*.test.mjs
```

本轮启动检查移除了继承的 `PIPECAT_*`、`PYTHONPATH`、`PYTHONHOME` 等开发环境配置；未加载 `.env`。配置ASR、LLM和TTS为 `http://127.0.0.1:1`，四个数字人/端到端服务URL为空。应用绑定临时本机端口，由验证进程请求页面、静态资源、能力及健康接口；无论成功或失败均关闭本轮启动的子进程。

## 保留的失败与边界

第一次验证脚本的嵌套f-string不兼容Python 3.11，环境信息采集失败；安装本身已成功。第二次脚本把实际的 `app.mjs` 写成 `app.js`，收到404；四组测试当时全部通过。修正检查路径后仅补跑应用启动，不重复计算测试。两次失败报告、脚本与日志均保留，源码没有因这些检查脚本错误而修改。

本记录不能证明全新GPU机器部署、真实视觉模型推理、麦克风/音箱效果或人物素材可再分发。真实模型评测及消融实验仍需完成；安装验证不会替代这些阶段门槛。
