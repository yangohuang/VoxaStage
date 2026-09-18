"""Dialogue-model selection is independent from avatar-renderer selection."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

class DialogueRegistry:
    def __init__(self, config_path=None, *, env=None):
        env=os.environ if env is None else env
        path=Path(config_path) if config_path else Path(__file__).parent/'runtime/dialogue-config.json'
        config=json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config,dict):raise ValueError('Invalid dialogue configuration')
        self.url=env.get('PIPECAT_MINICPM_URL',config.get('minicpm_url',''))
        self.default=env.get('PIPECAT_DIALOGUE_BACKEND',config.get('default_backend','cascade'))
        if not isinstance(self.url,str):raise ValueError('Invalid MiniCPM URL')
        if self.url:
            parsed=urlsplit(self.url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise ValueError('Invalid MiniCPM URL')
            _=parsed.port
        if self.default not in ('cascade','minicpm'):raise ValueError('Invalid default dialogue backend')

    def resolve(self,name):
        name=self.default if name is None else name
        if name not in ('cascade','minicpm'):raise ValueError('未知对话后端。')
        if name=='minicpm' and not self.url:raise ValueError('MiniCPM-o 尚未配置模型服务。')
        return name

    def public(self):
        return {'default_backend':self.default,'dialogue_backends':[
            {'id':'cascade','label':'Qwen · 级联语音','configured':True},
            {'id':'minicpm','label':'MiniCPM-o · 端到端语音','configured':bool(self.url)}]}
