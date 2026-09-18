import json
from pathlib import Path
import tempfile
import unittest
from dialogue_backends import DialogueRegistry

class DialogueTests(unittest.TestCase):
    def test_default_and_configured_endpoint_remain_server_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            r=DialogueRegistry(path,env={})
            self.assertEqual(r.resolve(None),'cascade')
            with self.assertRaises(ValueError):r.resolve('minicpm')
            path.write_text(json.dumps({'minicpm_url':'http://127.0.0.1:18201'}))
            r=DialogueRegistry(path,env={})
            self.assertEqual(r.resolve('minicpm'),'minicpm')
            self.assertNotIn('127.0.0.1',json.dumps(r.public()))
            for bad in ('http://user:pass@localhost','file:///tmp/model','http://localhost/#x'):
                with self.assertRaises(ValueError):DialogueRegistry(path,env={'PIPECAT_MINICPM_URL':bad})
            with self.assertRaises(ValueError):r.resolve('unsupported-backend')
