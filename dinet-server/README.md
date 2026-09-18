# 既有 DINet 服务的消费节奏配置

这里只提供本项目的策略和启动器，调用部署者已经安装的特定原生服务。没有复制模型、原生服务实现、人物或权重，也不适用于直接克隆的DINet官方仓库。来源边界见[第三方声明](../THIRD_PARTY.md)，实验依据见[提前量对照](../docs/DINET-PACING-EXPERIMENT.md)。

原消费者快速处理前五个200ms块后，按完整已消费时长等待，曾导致约250ms供帧间隔。启动器只在内存中将等待目标减去配置提前量，保留前五块条件、正值才睡眠、原模型调用、心跳、代理、缓存和取消清理。原生文件不修改。

## 使用

先在独立部署配置文件中明确写入兼容模式：

```json
{"version":1,"lookahead_seconds":0}
```

保留原worker的工作目录、环境变量、GPU分配与进程数量，只把它的启动命令替换成：

```bash
python /path/to/voxastage/dinet-server/launch.py \
  --native-root /path/to/native-service \
  --settings /path/to/deployment/pacing.json
```

启动器在导入原生代码前检查配置及`worker.py`、`ws_service.py`完整SHA。当前只支持`launch.py`列出的已审计文件；不匹配则拒绝启动，不绕过校验猜测兼容性。部署应使用固定快照，不让运行进程依赖持续编辑的开发目录。

## 开关与回退

`lookahead_seconds`必须是0–0.8之间的有限数字，配置最多1024字节，不支持额外键。0保持原等待公式；0.4是本次实验能消除停顿的最小已测值，未证明是所有负载下的最优值。

每个WebSocket音频消费者启动时读取一次配置，并记录应用值和配置SHA。浏览器不能设置该值。当前VoxaStage每个clip使用新连接，因此更新在后续clip生效；已经运行的消费者保持自己的快照。

更新时原子替换文件，避免读到半写入JSON。例如：

```python
import json
from pathlib import Path

path = Path('/path/to/deployment/pacing.json')
temporary = path.with_suffix('.new')
temporary.write_text(json.dumps({'version': 1, 'lookahead_seconds': 0.4}) + '\n')
temporary.replace(path)
```

回退策略时同样写0。若撤销启动器，恢复部署前的原worker命令，在没有活跃连接时重启该worker组。模型文件和原生源码不需要恢复，因为启动器未修改它们。缺失或损坏配置会使新消费者失败并由原生异常处理清理，不能被当作静默回退。

## 验证

```bash
python -m unittest discover -s dinet-server -p 'test_*.py'
```

测试覆盖兼容公式、保持提前量后的200ms节奏、明确配置及其快照、无效范围、未知表达式拒绝。原代理路径的真实生成、配置故障、取消恢复和播放器还必须在目标部署验证，不能仅凭CPU测试启用。

计时优化不证明整体口型质量。0与0.4秒在一条固定输入上已得到完全相同的61帧JPEG/PCM；其他输入、物理扬声器、长时间会话和更广泛质量仍按各自证据判断。

当前部署已完成[原代理验收](../docs/DINET-PACING-INTEGRATION.md)：17次API检查、配置故障与恢复、10次正式浏览器对照，以及独立回滚演练。其他部署仍须按自身版本和环境验证。
