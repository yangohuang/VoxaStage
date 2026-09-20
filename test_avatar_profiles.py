import dataclasses
import json
from pathlib import Path
import tempfile
import unittest

from avatar_providers import ProviderRegistry


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.json'
        self.providers = ProviderRegistry(Path(self.temp.name) / 'providers.json', env={
            'PIPECAT_DINET_URL': 'ws://localhost:19003/api/ws/live_video/person',
            'PIPECAT_FLASHHEAD_URL': 'ws://localhost:8203/v1/stream',
        })

    def config(self):
        return {'version': 1, 'default_profile': 'kanghui256', 'profiles': [{
            'id': 'kanghui256', 'label': '康辉 256', 'provider': 'dinet', 'voice': 'male',
            'idle_url': '/avatar/idle/dinet.mp4', 'description': '部署的人物素材',
        }]}

    def registry(self, config=None, providers=None):
        import avatar_profiles
        if config is not None:
            self.path.write_text(json.dumps(config))
        return avatar_profiles.ProfileRegistry(providers or self.providers, self.path)

    def test_empty_environment_override_preserves_configured_default(self):
        from avatar_profiles import ProfileRegistry
        self.path.write_text(json.dumps(self.config()))
        registry = ProfileRegistry(self.providers, self.path,
                                   env={'PIPECAT_AVATAR_PROFILE': ''})
        self.assertEqual(registry.default, 'kanghui256')

    def test_environment_overrides_default_but_explicit_selection_wins(self):
        from avatar_profiles import ProfileRegistry
        registry = ProfileRegistry(self.providers, self.path,
                                   env={'PIPECAT_AVATAR_PROFILE': 'dinet'})
        self.assertEqual(registry.resolve().id, 'dinet')
        self.assertEqual(registry.resolve('flashhead').id, 'flashhead')
        with self.assertRaises(ValueError):
            ProfileRegistry(self.providers, self.path,
                            env={'PIPECAT_AVATAR_PROFILE': 'unknown'})

    def test_missing_config_preserves_legacy_default_choices_and_voices(self):
        registry = self.registry()
        self.assertEqual(registry.default, self.providers.default)
        self.assertEqual(registry.resolve().id, 'streamingtalker')
        self.assertEqual({p.id: p.voice for p in registry.profiles.values()},
                         {'dinet': 'male', 'flashhead': 'female', 'streamingtalker': 'male'})
        self.assertNotIn('康辉', registry.profiles['dinet'].label)
        self.assertEqual(registry.profiles['dinet'].idle_url, '/avatar/idle/dinet.mp4')

    def test_explicit_profile_is_immutable_and_public_contains_no_endpoint(self):
        registry = self.registry(self.config())
        profile = registry.resolve()
        self.assertEqual(profile.label, '康辉 256')
        self.assertEqual(profile.make_backend(self.providers).url,
                         self.providers.resolve('dinet').url)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.voice = 'female'
        with self.assertRaises(TypeError):
            registry.profiles['other'] = profile
        public = registry.public()
        self.assertEqual(public['default_profile'], 'kanghui256')
        self.assertEqual(public['profiles'][0]['provider'], 'dinet')
        self.assertEqual(public['profiles'][0]['kind'], '2d')
        self.assertTrue(public['profiles'][0]['configured'])
        self.assertNotIn('ws://', json.dumps(public))
        public['profiles'][0]['label'] = 'Changed'
        self.assertEqual(registry.resolve().label, '康辉 256')

    def test_missing_provider_endpoint_is_visible_but_cannot_resolve(self):
        providers = ProviderRegistry(Path(self.temp.name) / 'empty.json', env={})
        registry = self.registry(self.config(), providers)
        self.assertFalse(registry.public()['profiles'][0]['configured'])
        with self.assertRaises(ValueError):
            registry.resolve()
        with self.assertRaises(ValueError):
            registry.profiles['kanghui256'].make_backend(providers)

    def test_deployment_override_supports_a_distinct_same_driver_resource(self):
        config = self.config()
        config['profiles'].append(dict(config['profiles'][0], id='anchor2', label='主播 2',
                                       url='wss://avatar.internal:19004/api/ws/live_video/anchor2',
                                       idle_url='/avatar/idle/anchor2.mp4', voice='female'))
        registry = self.registry(config)
        profile = registry.resolve('anchor2')
        self.assertEqual(profile.voice, 'female')
        self.assertEqual(profile.make_backend(self.providers).url, config['profiles'][1]['url'])
        self.assertNotIn('avatar.internal', json.dumps(registry.public()))

    def test_override_can_configure_a_known_driver_without_a_global_endpoint(self):
        config = self.config()
        config['profiles'][0]['url'] = 'ws://localhost:19004/api/ws/live_video/anchor2'
        providers = ProviderRegistry(Path(self.temp.name) / 'empty.json', env={})
        registry = self.registry(config, providers)
        self.assertTrue(registry.public()['profiles'][0]['configured'])
        self.assertEqual(registry.resolve().make_backend(providers).url,
                         config['profiles'][0]['url'])

    def test_duplicate_driver_resources_need_distinct_endpoints_and_idle_assets(self):
        for changes in ({}, {'url': self.providers.resolve('dinet').url},
                        {'url': 'ws://localhost:19004/stream', 'idle_url': '/avatar/idle/dinet.mp4'}):
            config = self.config()
            config['profiles'].append(dict(config['profiles'][0], id='anchor2',
                                           idle_url='/avatar/idle/anchor2.mp4', **{
                                               key: value for key, value in changes.items()
                                               if key != 'idle_url'}))
            if 'idle_url' in changes:
                config['profiles'][1]['idle_url'] = changes['idle_url']
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.registry(config)

    def test_rejects_invalid_document_schema_and_defaults(self):
        invalid = [[], None, {}, {'version': True}, dict(self.config(), version=2),
                   dict(self.config(), version=1.0), dict(self.config(), profiles=[]),
                   dict(self.config(), profiles={}), dict(self.config(), extra=True),
                   dict(self.config(), default_profile='unknown'),
                   dict(self.config(), default_profile=['kanghui256']),
                   dict(self.config(), profiles=[None])]
        for value in invalid:
            self.path.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.registry()

    def test_rejects_invalid_profile_fields_and_unknown_fields(self):
        invalid = {'id': ['', '../foo', 'Upper', 'a/b', 'x' * 65, 1],
                   'label': ['', '   ', 7, 'x' * 121],
                   'provider': ['unknown', 'mindtalker', {}, 2],
                   'voice': ['other', 'Male', None],
                   'description': [None, 2, 'x' * 1025],
                   'backend_url': ['ws://localhost/'],
                   'idle_url': ['https://example.com/idle.mp4', '//evil/idle.mp4',
                                '/avatar/idle/../secret.mp4', '/avatar/idle/%2e%2e.mp4',
                                '/avatar/idle/a.mp4?x=1', '/avatar/idle/a.mp4#x',
                                '/avatar/idle/a.MP4', None]}
        for key, values in invalid.items():
            for value in values:
                config = self.config()
                config['profiles'][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.registry(config)
        for key in ('id', 'label', 'provider', 'voice', 'idle_url'):
            config = self.config()
            del config['profiles'][0][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.registry(config)

    def test_rejects_unsafe_backend_urls(self):
        for url in ('', 'https://localhost/', 'file:///tmp/model', 'ws://user:secret@localhost/',
                    'ws://@localhost/', 'ws://localhost/#fragment', 'ws://localhost:99999/',
                    'ws://localhost:bad/', 'ws:///stream', 'ws://local\nhost/stream', None):
            config = self.config()
            config['profiles'][0]['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.registry(config)

    def test_rejects_duplicate_ids_and_unknown_lookup(self):
        config = self.config()
        config['profiles'].append(config['profiles'][0].copy())
        with self.assertRaises(ValueError):
            self.registry(config)
        registry = self.registry(self.config())
        for key in ('missing', ['kanghui256'], {}, 5, ''):
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.resolve(key)

    def test_known_enum_driver_must_exist_in_the_provider_registry(self):
        del self.providers.providers['dinet']
        with self.assertRaises(ValueError):
            self.registry(self.config())

    def test_rejects_malformed_json_and_excessive_profiles(self):
        self.path.write_text('{')
        with self.assertRaises(ValueError):
            self.registry()
        config = self.config()
        config['profiles'] = config['profiles'] * 65
        with self.assertRaises(ValueError):
            self.registry(config)


if __name__ == '__main__':
    unittest.main()
