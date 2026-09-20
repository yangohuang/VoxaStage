# 本地动态质量复核

使用已完成的45次输出捕获，建立不调用模型的离线复核入口。保存帧与PCM的原始配对关系；网络供帧和生产播放器卡顿仍由原有基线测量，离线复核不替代它们。

## 分工与接口

1. `avatar_review.py`读取`captures/<provider>/<case>/`的manifest、metadata、index、output.wav和frames；验证成功状态、PCM哈希、帧索引/PTS、拓扑与有界尺寸。匿名编号由固定seed随机排序，映射只存本地复核目录。API不返回模型名、路径或角色ID。读取帧时校验原始payload哈希，修改过的文件不能被当作原捕获播放。
2. 浏览器一次预载一个有界片段：2D解码JPEG，3D复用HeadRenderer；以audio.currentTime选择原始PTS帧。暂停/拖动/逐帧查看，保留播放观测，但不自动把播放结束等同人工验收。
3. 人工或自动复核来源显式记录。问题维度含timing、phonetics、continuity、appearance、pose；严重程度0–3，无选择时禁止提交。3D无纹理网格不提供人像appearance评分。记录片段起止、备注、复核者代号及捕获fingerprint，写到本地annotation目录；导出JSON供后续对照，不自动进Git。
4. 先以合成捕获写失败测试，验证损坏媒体、路径边界、记录合法性及3D限制；前端测试覆盖帧边界、加载失败/取消清理及评审范围。真实浏览器重放三种驱动的语音、静音和停顿，验证seek/切换/标注；自动化记录不冒充人工质量判断。
5. 明确复核材料就绪与质量评审完成的区别。后续只有实际观察支撑的质量结论才写报告，未听过的语音不填口型音素评分。

### API约定

- `GET /api/clips`：`{clips:[{id,kind,category,duration,fps,frames,fingerprint}]}`，frames是数量。
- `GET /api/clips/{id}`：上述字段，frames替换为`[{index,pts,url}]`，另有`sampleRate`、`audioUrl`，2D含width/height，3D含vertexCount/faces。
- 帧URL由详情提供；2D为JPEG，3D为小端float32的xyz原始字节。音频为WAV。
- `POST /api/annotations`：`{clipId,fingerprint,reviewer,reviewerKind,dimension,start,end,severity,notes,reviewed:true}`；reviewerKind为human或automated。服务端验证后返回保存记录，201。
- `GET /api/annotations`：`{annotations:[...]}`。未知样片、fingerprint不匹配或未明确复核的请求拒绝。
- 页面`/`，资源`/review.mjs`、`/review-core.mjs`、`/renderer.mjs`；只绑定127.0.0.1，Host和写请求Origin检查绑定端口，默认18426。

实现前端与本地数据服务可独立进行，之后按接口集成并分别作需求、代码审查。继续使用既有独立worktree，草稿PR保留阶段边界。

实现检查点：服务15项与核心前端9项测试通过，45捕获加载、9片浏览器播放/回看、快速切换及本地标注导出验证通过。审查发现的切换资源暂留与Unicode保存后重启失败已各补红绿回归。正式目录尚无质量评分；结果见[复核说明](../AVATAR-QUALITY-REVIEW.md)。
