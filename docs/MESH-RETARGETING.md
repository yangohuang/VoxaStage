# 用 StreamingTalker 驱动独立 3D 角色

StreamingTalker 预测人脸顶点，浏览器将上、下唇顶点对的平均距离映射到 GLB 角色的两个嘴部节点。角色可以是企鹅或其他带上下颚控制节点的 mesh。它复用已有的音频播放时钟、迟到帧过滤和打断机制，不增加另一套语音模型。

## 部署配置

把自包含的 GLB 和映射 JSON 放入 `runtime/avatar-meshes/`，在 `runtime/avatar-profiles.json` 增加一个 `streamingtalker` 角色：

```json
{
  "id": "penguin",
  "label": "企鹅",
  "provider": "streamingtalker",
  "voice": "male",
  "idle_url": "/avatar/idle/penguin.mp4",
  "mesh_url": "/avatar/mesh/penguin.glb",
  "retarget_url": "/avatar/mesh/penguin.json"
}
```

`mesh_url`、`retarget_url` 必须成对出现，且只能引用这两个同源路由。服务只公开已配置的资源名，不接受浏览器提供的磁盘路径或模型地址。多个角色可以复用同一个 StreamingTalker 服务，但必须使用不同的 mesh 和 idle 资源标识。现有人脸角色继续使用原渲染器。

mesh 角色待机时直接显示闭嘴的 GLB，不播放 `idle_url` 视频；该字段保留角色配置兼容性，不要求为 mesh 角色制作 MP4。首次加载失败会阻止连接，重新选择角色可重试。浏览器需支持 WebGL 2。

映射 JSON 示例（5023 顶点的 VOCASET / FLAME 拓扑）：

```json
{
  "version": 1,
  "vertex_count": 5023,
  "pairs": [[3531, 3513], [1669, 1836], [1670, 1778], [2786, 2934], [2787, 2892]],
  "closed": 0.0018,
  "open": 0.014,
  "upper": {"node": "Beak Upper", "axis": "x", "radians": -0.052359877560},
  "lower": {"node": "Beak Lower", "axis": "x", "radians": 0.436332312999}
}
```

`closed` 与 `open` 是源网格坐标单位中的闭嘴、张嘴距离。每帧计算平均唇距，线性映射并限制到 `[0, 1]`；不是从音量生成嘴型。两个节点在导出时应处于闭嘴姿态，原点在各自旋转轴心。`axis` 是 GLB 节点的局部轴；`radians` 是相对初始姿态的最大转角。舌头等子节点随下颚继承运动。

不同源拓扑必须重新选择唇部顶点并标定。仅顶点数量相同不代表拓扑兼容；部署者必须保证顶点顺序一致。当前前端检查数量、数值与配置范围，不会自动推断新模型的语义拓扑。

## 企鹅版本的边界

- 企鹅 rig 与原始 mesh 来自独立的角色制作任务，未随源码分发；部署者另行准备有权使用的资源。本项目新增的是浏览器导入和嘴型映射。
- 浏览器兼容导出保留网格、纹理、上下喙、口腔与舌头；不包含 Blender 曲线毛发和离线灯光，因此外观与离线渲染不同。
- 当前只有一个开合自由度，不是完整音素、唇形或情绪重定向；不预测全身动作。离线角色任务的头部摆动属于手工动画，未冒充模型预测。
- 回应结束、打断和断开时归零为闭嘴姿态。待机不消耗模型推理，但当前没有循环全身动作。
- GLB 必须自包含、不引用外部纹理或缓冲区；不启用压缩解码扩展。GLB 上限 32 MiB，映射文件上限 64 KiB。
- 渲染使用本地固定版本 Three.js 0.180.0（MIT），包括 GLTFLoader；详见 [NOTICE](../avatar-web/vendor/three/NOTICE) 与 [LICENSE](../avatar-web/vendor/three/LICENSE)。

## 验证

运行 `python -m unittest test_avatar_profiles test_avatar_conversations` 与 `node --test avatar-web/tests/*.test.mjs`。人工或浏览器验收还需检查：闭嘴和张嘴外观、连续语音、打断后归零、断开、快速切换以及加载失败后重试。录制推理回放可以验证显示与播放逻辑，但不能替代在线 ASR、LLM、TTS 和 StreamingTalker 的完整对话验收。
