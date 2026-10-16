# 06 · StreamingTalker：浏览器如何让三维人脸跟着声音说话？

状态：**选题简报，不是已发表文章**。已有网格与服务材料；效果录像需准备。

目标篇幅：3000–3800字。核心论点：三维人脸输出的是顶点序列，播放器需要理解拓扑和时间。

## 写作结构

1. 对比视频帧和三维网格的输出形式。
2. 简述上游自回归扩散生成与增量服务。
3. 解释拓扑、帧号、音频时间与浏览器更新。
4. 用断连竞态说明任务终态和连接生命周期。
5. 展示打断、归零与恢复，说明3D头部的范围。

## 配图与素材

- 音频→顶点→浏览器示意图。
- 拓扑与帧时间图。
- 15–40秒真实网格播放与恢复录像。

## 发布门槛

确认公开演示资源许可、记录版本；源码可复现断连回归，不将测试数汇总成质量分数。

必须保留的边界：不得把面部网格生成写成全身动作生成；结构合法不等于主观口型准确。

## 已有来源

- [README.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/avatar-server/README.md)
- [STREAMINGTALKER-CLOSE-RACE.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/STREAMINGTALKER-CLOSE-RACE.md)
- [AVATAR-GEOMETRY-BASELINE.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/AVATAR-GEOMETRY-BASELINE.md)
- [STREAMINGTALKER-STANDALONE-VALIDATION.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/STREAMINGTALKER-STANDALONE-VALIDATION.md)

[返回系列目录](../README.md)
