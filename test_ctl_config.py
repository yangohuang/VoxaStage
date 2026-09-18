import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ctl

class BackendConfigTests(unittest.TestCase):
    def test_persistent_endpoints_with_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'backend-env.json'
            path.write_text(json.dumps({'PIPECAT_LLM_URL':'http://127.0.0.1:18212','PIPECAT_ASR_MODEL':'Qwen3-ASR-1.7B'}))
            with patch.object(ctl,'STATE',Path(directory)):
                env=ctl.backend_environment({'PIPECAT_LLM_URL':'http://127.0.0.1:18311','PATH':'/bin'})
            self.assertEqual(env['PIPECAT_LLM_URL'],'http://127.0.0.1:18311')
            self.assertEqual(env['PIPECAT_ASR_MODEL'],'Qwen3-ASR-1.7B')
            self.assertEqual(env['PATH'],'/bin')
    def test_rejects_unrelated_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'backend-env.json').write_text(json.dumps({'LD_PRELOAD':'bad'}))
            with patch.object(ctl,'STATE',Path(directory)),self.assertRaises(ValueError):
                ctl.backend_environment({})
