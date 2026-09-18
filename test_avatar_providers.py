import json
from pathlib import Path
import tempfile
import unittest

from avatar_providers import ProviderRegistry


class ProviderTests(unittest.TestCase):
    def test_voice_and_idle_follow_character(self):
        registry=self.registry()
        self.assertEqual({k:p.voice for k,p in registry.providers.items()},
                         {'streamingtalker':'male','dinet':'male','flashhead':'female'})
        for key,provider in registry.providers.items():
            self.assertEqual(provider.public()['idle_url'],f'/avatar/idle/{key}.mp4')

    def registry(self, config=None, env=None):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'config.json'
        if config is not None:
            path.write_text(json.dumps(config))
        return ProviderRegistry(path, env=env or {})

    def test_default_compatible_and_public_has_no_urls(self):
        registry = self.registry({'url': 'ws://localhost:12544/v1/stream'})
        self.assertEqual(registry.resolve(None).id, 'streamingtalker')
        public = registry.public()
        self.assertEqual(len(public['providers']), 3)
        self.assertNotIn('ws://', json.dumps(public))
        self.assertFalse(public['providers'][1]['configured'])

    def test_explicit_2d_config_and_environment_override(self):
        registry = self.registry({'providers': {'flashhead': {'url': 'ws://localhost:8203/v1/stream'}}},
                                 {'PIPECAT_AVATAR_PROVIDER': 'flashhead'})
        provider = registry.resolve(None)
        self.assertEqual(provider.kind, '2d')
        self.assertEqual(provider.make_backend().url, 'ws://localhost:8203/v1/stream')

    def test_dinet_replaces_mindtalker_and_uses_native_adapter(self):
        from dinet_backend import DINetBackend
        registry = self.registry(env={'PIPECAT_DINET_URL': 'ws://localhost:19003/api/ws/live_video/person'})
        self.assertEqual(set(registry.providers), {'dinet', 'flashhead', 'streamingtalker'})
        self.assertIsInstance(registry.resolve('dinet').make_backend(), DINetBackend)
        with self.assertRaises(ValueError):
            registry.resolve('mindtalker')

    def test_unknown_unconfigured_and_bad_addresses_fail_clearly(self):
        registry = self.registry()
        for name in ('unknown', 'dinet', 'mindtalker'):
            with self.assertRaises(ValueError):
                registry.resolve(name)
        for url in ('https://localhost/', 'ws://user:secret@localhost/', 'ws://localhost/#bad'):
            with self.assertRaises(ValueError):
                self.registry(env={'PIPECAT_FLASHHEAD_URL': url})
