# 07 · 声音、视频、网格各跑各的，怎么才能同步？

状态：**选题简报，不是已发表文章**。已有播放器与采集材料；代表作。

目标篇幅：3000–4000字。核心论点：生成时钟、网络到达时间和音频播放时钟必须区分。

## 写作结构

1. 用声音已经开始而画面还在等待的场景开篇。
2. 定义三个时钟和真正的测量边界。
3. 解释音频为参考的播放调度与有界队列。
4. 讨论提前缓存、迟到帧、欠载和后台节流。
5. 用现有采集记录复算调度现象并列出物理测量缺口。

## 配图与素材

- 三时钟关系图。
- 队列与迟到帧示意图。
- 现有数据的测量边界图。

## 发布门槛

每个图表数值可追溯输入、配置和逐轮数据；同期核对播放器实现，物理验收未完成就明确保留。

必须保留的边界：播放器调度偏差不是音素口型误差，也不是物理扬声器延迟。

## 已有来源

- [FIXED-PLAYBACK-BASELINE.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/FIXED-PLAYBACK-BASELINE.md)
- [CAPTURE-TIMING-BASELINE.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/CAPTURE-TIMING-BASELINE.md)
- [DINET-PACING-INTEGRATION.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/DINET-PACING-INTEGRATION.md)
- [timeline.mjs](https://github.com/yangohuang/VoxaStage/blob/dd44878/avatar-web/timeline.mjs)

[返回系列目录](../README.md)
