# 固定音色模式的多角色映射

此文件适用于本项目现有 IndexTTS 服务的 `common/resource_provider.py` 接口。
原 `RESOURCE_MODE=fixed` 会忽略请求的 `audio_id`，始终选择 `FIXED_SPEAKER_ID`；
仅调用 `/api/tts/registry` 注册新 ID 无法改变该行为。

备份并替换服务端 `common/resource_provider.py` 后，为 TTS 进程配置
`LOCAL_SPEAKERS_JSON=/absolute/path/voices.json`：

```json
[
  {"id":"pipecat_male","wav_local_path":"/absolute/path/male.wav"},
  {"id":"female11","wav_local_path":"/absolute/path/female.wav"}
]
```

文件由服务管理员提供，音频路径不能来自浏览器。参考音频必须在每次启动时可读。
原默认音色继续保留，最多增加7个音色；现有启动注册逻辑会加载它们。
应用服务器用 `PIPECAT_MALE_SPEAKER_ID` 和 `PIPECAT_FEMALE_SPEAKER_ID` 指向对应 ID。
未配置该文件时完全沿用原固定音色行为，数据库模式也不改变。

本机保存了 `runtime/tts-resource-provider.before.py`、`runtime/tts-supervisord.before.conf`
用于回退。修改已部署到现有容器文件系统，重建镜像时须将该适配器和环境配置纳入部署；
不要把当前容器修改误认为上游 IndexTTS 已原生支持这个配置。
