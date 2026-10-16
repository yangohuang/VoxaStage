# 05 · 把 FlashHead 接进实时对话：从生成视频到持续播放

状态：**选题简报，不是已发表文章**。已有接入材料；效果录像需准备。

目标篇幅：2500–3200字。核心论点：把视频模型放进对话，需要把生成片段变成可取消、能配对的媒体流。

## 写作结构

1. 以等待首段与持续播放的区别开场。
2. 说明采用的FlashHead版本及肖像驱动机制。
3. 追踪音频块、视频帧及采样数量的对应关系。
4. 解释取消、背压、音色配置与idle恢复。
5. 给出接入检查清单，讨论资源与兼容补丁。

## 配图与素材

- 模型与服务边界图。
- 媒体块配对图。
- 15–40秒真实播放、打断、idle录像。

## 发布门槛

锁定上游版本，核对采样率与时间单位；完成可公开素材的实际录像，不使用静态图伪装视频效果。

必须保留的边界：不能把上游生成质量或速度当成自研；不能仅凭实时显示宣称低延迟。

## 已有来源

- [README.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/video-server/README.md)
- [avatar_video_backend.py](https://github.com/yangohuang/VoxaStage/blob/dd44878/avatar_video_backend.py)
- [AVATAR-OUTPUT-BASELINE-2026-09-19.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/docs/AVATAR-OUTPUT-BASELINE-2026-09-19.md)
- [THIRD_PARTY.md](https://github.com/yangohuang/VoxaStage/blob/dd44878/THIRD_PARTY.md)

[返回系列目录](../README.md)
