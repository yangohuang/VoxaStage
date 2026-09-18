"""Deployment-owned avatar choices. Never accept a service URL from the browser."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from avatar_backend import AvatarBackend
from avatar_video_backend import VideoBackend
from dinet_backend import DINetBackend


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str
    url: str
    description: str

    @property
    def voice(self):
        return 'female' if self.id == 'flashhead' else 'male'

    def make_backend(self):
        if self.id == 'dinet':
            return DINetBackend(self.url)
        return (AvatarBackend if self.kind == '3d' else VideoBackend)(self.url)

    def public(self):
        return dict(id=self.id, label=self.label, kind=self.kind, configured=bool(self.url),
                    voice=self.voice, idle_url=f'/avatar/idle/{self.id}.mp4',
                    description=self.description if self.url else '尚未配置模型服务')


class ProviderRegistry:
    def __init__(self, config_path=None, *, env=None):
        env = os.environ if env is None else env
        path = Path(config_path) if config_path else Path(__file__).parent / 'runtime/avatar-config.json'
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict) or not isinstance(config.get('providers', {}), dict):
            raise ValueError('Invalid avatar provider configuration')
        self.default = env.get('PIPECAT_AVATAR_PROVIDER', config.get('default_provider', 'streamingtalker'))
        self.providers = {}
        choices = (
            ('streamingtalker', 'StreamingTalker · 3D', '3d', 'PIPECAT_AVATAR_URL',
             config.get('url', 'ws://127.0.0.1:12544/v1/stream'), '语音驱动三维头部网格'),
            ('dinet', 'DINet · 2D', '2d', 'PIPECAT_DINET_URL', '', 'DINet 真人视频口型驱动'),
            ('flashhead', 'FlashHead · 2D', '2d', 'PIPECAT_FLASHHEAD_URL', '', 'FlashHead 音频驱动视频'),
        )
        for id, label, kind, variable, fallback, description in choices:
            entry = config.get('providers', {}).get(id, {})
            if not isinstance(entry, dict):
                raise ValueError(f'Invalid avatar provider: {id}')
            url = env.get(variable, entry.get('url', fallback))
            if not isinstance(url, str):
                raise ValueError(f'Invalid avatar URL: {id}')
            if url:
                parsed = urlsplit(url)
                if (parsed.scheme not in ('ws', 'wss') or not parsed.hostname or parsed.username
                        or parsed.password or parsed.fragment):
                    raise ValueError(f'Invalid avatar URL: {id}')
                _ = parsed.port  # Validate malformed/out-of-range ports at startup.
            self.providers[id] = Provider(id, label, kind, url, description)
        if self.default not in self.providers:
            raise ValueError('Unknown default avatar provider')

    def resolve(self, id=None):
        provider = self.providers.get(self.default if id is None else id)
        if provider is None:
            raise ValueError('未知数字人驱动，请重新选择。')
        if not provider.url:
            raise ValueError(f'{provider.label} 尚未配置模型服务。')
        return provider

    def public(self):
        return dict(default_provider=self.default, providers=[p.public() for p in self.providers.values()])
