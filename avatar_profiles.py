"""Deployment-owned characters, independently selectable from their driver.

Only a server configuration file may supply a backend URL. Browser selections
are profile IDs; public metadata contains only a same-origin idle asset route.
"""
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from urllib.parse import urlsplit


_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
_IDLE = re.compile(r'/avatar/idle/[a-z0-9][a-z0-9_-]{0,63}\.mp4\Z')
_DRIVERS = frozenset(('dinet', 'flashhead', 'streamingtalker'))
_REQUIRED = frozenset(('id', 'label', 'provider', 'voice', 'idle_url'))
_OPTIONAL = frozenset(('description', 'url'))


def _text(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _backend_url(value):
    """Validate deployment configuration without including its secrets in errors."""
    if not _text(value, 2048) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError('Invalid avatar profile backend URL')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('ws', 'wss') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or '\\' in value):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ValueError('Invalid avatar profile backend URL') from None
    return value


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    provider: str
    voice: str
    idle_url: str
    description: str = ''
    url: str = field(default='', repr=False)

    def make_backend(self, provider_registry):
        """Build the configured driver; a profile's voice is applied by dialogue."""
        provider = provider_registry.providers.get(self.provider)
        if provider is None or not (self.url or provider.url):
            raise ValueError('数字人角色尚未配置模型服务。')
        return replace(provider, url=self.url or provider.url).make_backend()

    def public(self, provider_registry):
        provider = provider_registry.providers.get(self.provider)
        configured = bool(provider and (self.url or provider.url))
        return dict(id=self.id, label=self.label, provider=self.provider,
                    kind=provider.kind if provider else '', voice=self.voice,
                    idle_url=self.idle_url, configured=configured,
                    description=self.description if configured else '尚未配置模型服务')


class ProfileRegistry:
    def __init__(self, provider_registry, config_path=None, *, env=None):
        env = os.environ if env is None else env
        self.provider_registry = provider_registry
        path = (Path(config_path) if config_path is not None
                else Path(__file__).parent / 'runtime/avatar-profiles.json')
        if not path.exists():
            # Existing deployments retain their driver IDs, default and voices.
            self.default = provider_registry.default
            profiles = {
                p.id: Profile(p.id, p.label, p.id, p.voice,
                              f'/avatar/idle/{p.id}.mp4', p.description)
                for p in provider_registry.providers.values()
            }
        else:
            if path.stat().st_size > 256 * 1024:
                raise ValueError('Avatar profile configuration is too large')
            config = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(config, dict)
                    or set(config) != {'version', 'default_profile', 'profiles'}
                    or type(config['version']) is not int or config['version'] != 1
                    or not isinstance(config['profiles'], list)
                    or not 1 <= len(config['profiles']) <= 64):
                raise ValueError('Invalid avatar profile configuration')
            profiles = {}
            resources = {}
            for entry in config['profiles']:
                profile = self._parse(entry)
                if profile.id in profiles:
                    raise ValueError('Duplicate avatar profile ID')
                provider = provider_registry.providers.get(profile.provider)
                if provider is None:
                    raise ValueError('Avatar profile references an unknown driver')
                endpoint = profile.url or provider.url
                # Listing a new character must not simply alias the same model
                # or idle clip and imply that another resource was deployed.
                for other_endpoint, other_idle in resources.get(profile.provider, []):
                    if endpoint == other_endpoint or profile.idle_url == other_idle:
                        raise ValueError('Same-driver profiles require distinct backend and idle resources')
                resources.setdefault(profile.provider, []).append((endpoint, profile.idle_url))
                profiles[profile.id] = profile
            self.default = config['default_profile']
        self.default = env.get('PIPECAT_AVATAR_PROFILE') or self.default
        if not isinstance(self.default, str) or self.default not in profiles:
            raise ValueError('Unknown default avatar profile')
        self.profiles = MappingProxyType(profiles)

    @staticmethod
    def _parse(entry):
        if (not isinstance(entry, dict) or not _REQUIRED <= set(entry)
                or set(entry) - _REQUIRED - _OPTIONAL):
            raise ValueError('Invalid avatar profile fields')
        if (not isinstance(entry['id'], str) or not _ID.fullmatch(entry['id'])
                or not _text(entry['label'], 120)
                or not isinstance(entry['provider'], str) or entry['provider'] not in _DRIVERS
                or not isinstance(entry['voice'], str) or entry['voice'] not in ('male', 'female')
                or not isinstance(entry['idle_url'], str) or not _IDLE.fullmatch(entry['idle_url'])
                or not isinstance(entry.get('description', ''), str)
                or len(entry.get('description', '')) > 1024):
            raise ValueError('Invalid avatar profile values')
        if 'url' in entry:
            _backend_url(entry['url'])
        return Profile(**entry)

    def resolve(self, id=None):
        key = self.default if id is None else id
        profile = self.profiles.get(key) if isinstance(key, str) else None
        if profile is None:
            raise ValueError('未知数字人角色，请重新选择。')
        provider = self.provider_registry.providers.get(profile.provider)
        if provider is None or not (profile.url or provider.url):
            raise ValueError('数字人角色尚未配置模型服务。')
        return profile

    def public(self):
        return dict(default_profile=self.default,
                    profiles=[profile.public(self.provider_registry)
                              for profile in self.profiles.values()])
