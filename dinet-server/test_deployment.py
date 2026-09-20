import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import deployment

FACTORY = "def create_stream_generator(model, action, reference):\n    return (model, action, reference, {\"dinet_tensor_width\":128})\n"
PROXY = """async def bridge():
    async with websockets.connect(worker_ws_url, compression=None):
                await asyncio.gather(*tasks)
    rec_queue.put_nowait(message)
    rec_queue.put_nowait(message)
"""

class DeploymentTests(unittest.TestCase):
    def test_character_config_validates_before_native_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'model').write_bytes(b'model')
            (root/'frames').mkdir()
            value = dict(version=1, id='demo', tensor_width=256, model=str(root/'model'),
                         action=str(root/'frames'), reference=str(root/'frames'))
            path = root/'character.json'; path.write_text(json.dumps(value))
            spec = deployment.load_character(path)
            self.assertEqual(spec['id'], 'demo')
            for key, invalid in [('version', True), ('tensor_width', True), ('tensor_width', 128),
                                 ('id', '../demo'), ('model', 'relative.pth'),
                                 ('model', str(root/'frames')), ('action', str(root/'model')),
                                 ('reference', str(root/'absent')), ('other', 1)]:
                path.write_text(json.dumps({**value, key: invalid}))
                with self.subTest(key=key, value=invalid), self.assertRaises(ValueError):
                    deployment.load_character(path)
            for text in ('[]', '{', ' '*8193):
                path.write_text(text)
                with self.assertRaises(ValueError): deployment.load_character(path)

    def test_factory_guard_and_exact_arguments(self):
        sha = hashlib.sha256(FACTORY.encode()).hexdigest()
        with patch.object(deployment, 'FACTORY_SHA256', sha):
            factory = deployment.rewrite_factory(FACTORY, {})
            self.assertEqual(factory('m', 'a', 'r'), ('m', 'a', 'r', {'dinet_tensor_width': 256}))
            with self.assertRaises(ValueError): deployment.rewrite_factory(FACTORY+'# changed', {})
        with self.assertRaises(ValueError): deployment.rewrite_factory(FACTORY, {})

    def test_proxy_is_guarded_and_matches_validated_lifecycle(self):
        raw = PROXY.encode()
        with patch.object(deployment, 'PROXY_SHA256', hashlib.sha256(raw).hexdigest()):
            changed = deployment.rewrite_proxy(raw)
            self.assertEqual(changed.count('await rec_queue.put(message)'), 2)
            self.assertIn('close_timeout=.5', changed)
            self.assertIn('await asyncio.gather(*tasks, return_exceptions=True)', changed)
            compile(changed, '<proxy>', 'exec')
            with self.assertRaises(ValueError): deployment.rewrite_proxy(raw+b'# changed')
        with self.assertRaises(ValueError): deployment.rewrite_proxy(raw)

    def test_factory_failure_does_not_mutate_live_settings(self):
        settings = SimpleNamespace(resource_mode='original')
        creator = SimpleNamespace(create_stream_generator=lambda: None)
        service = SimpleNamespace(get_settings=lambda _: settings)
        with patch.object(deployment.inspect, 'getsource', return_value='unknown'):
            with self.assertRaises(ValueError):
                deployment.apply_character(service, creator, Path('/native'), dict(id='demo'))
        self.assertEqual(vars(settings), {'resource_mode': 'original'})
