# 04 · DINet 接入实战：为什么推理能跟上，数字人还是会卡？

状态：**选题简报，不是已发表文章**。已有固定部署对照；写作前复核图表与版本。

目标篇幅：2800–3500字。核心论点：推理吞吐足够时，消费者的等待策略仍可能制造供帧停顿。

## 写作结构

1. 用第19→20帧附近的间隔现象开场。
2. 用一幅图简述音频驱动人脸与当前服务分块边界。
3. 画出原等待目标和消费提前量的差别。
4. 复算0与0.4秒API和浏览器交错对照。
5. 解释少展示帧、测量边界和回滚验证。

## 配图与素材

- 供帧时间线PNG及源文件。
- 同输入两配置对照图，标记API/浏览器不同测量点。
- 15–40秒真实对照录像，使用可公开素材。

## 发布门槛

复算报告中的逐轮间隔；短句、长片段、停顿分别展示；人物素材授权或替代素材准备完成。

必须保留的边界：不得称为模型算力加速、全局最优或视觉零跳帧。

## 已有来源

- [DINET-PACING-INTEGRATION.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/DINET-PACING-INTEGRATION.md)
- [DINET-PACING-EXPERIMENT.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/DINET-PACING-EXPERIMENT.md)
- [DINET-DELIVERY-ATTRIBUTION.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/DINET-DELIVERY-ATTRIBUTION.md)
- [README.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/dinet-server/README.md)

[返回系列目录](../README.md)
