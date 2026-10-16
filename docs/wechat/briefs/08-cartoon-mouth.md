# 08 · 从真人嘴型到卡通企鹅：一次 3D 嘴部重定向实践

状态：**选题简报，不是已发表文章**。已有单自由度回放证据；在线效果待验收。

目标篇幅：2500–3300字。核心论点：用源人脸的唇距驱动独立角色嘴部，需要处理标定、拓扑与生命周期。

## 写作结构

1. 展示源网格和卡通上下颚的结构差异。
2. 解释唇距归一化与局部轴旋转。
3. 说明GLB导出、闭嘴初态和参数标定。
4. 解释播放时钟复用与打断归零。
5. 对照回放与在线路径，列出音素与情绪扩展缺口。

## 配图与素材

- 唇距→开合→旋转映射图。
- 角色局部轴示意图。
- 15–40秒真实回放录像并明确标注回放。

## 发布门槛

取得角色可公开使用依据；若标题加入实时对话，必须先补完整在线联动证据。

必须保留的边界：一个开合自由度不等于完整音素嘴型；手工动画不冒充模型预测。

## 已有来源

- [MESH-RETARGETING.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/MESH-RETARGETING.md)
- [mouth-retarget.mjs](https://github.com/yangohuang/VoxaStage/blob/dd44878/avatar-web/mouth-retarget.mjs)
- [mouth-retarget.test.mjs](https://github.com/yangohuang/VoxaStage/blob/dd44878/avatar-web/tests/mouth-retarget.test.mjs)

[返回系列目录](../README.md)
