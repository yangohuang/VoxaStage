import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('tts_resources',Path(__file__).parent/'tts-compat/resource_provider.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class ResourceTests(unittest.TestCase):
 def test_explicit_local_voices_resolve_and_load_at_startup(self):
  settings=SimpleNamespace(RESOURCE_MODE='fixed',FIXED_SPEAKER_ID='legacy',FIXED_SPEAKER_WAV_PATH='/legacy.wav')
  with tempfile.TemporaryDirectory() as directory:
   p=Path(directory)/'voices.json';p.write_text(json.dumps([{'id':'pipecat_male','wav_local_path':'/male.wav'},{'id':'female11','wav_local_path':'/female.wav'}]))
   with patch.dict(os.environ,{'LOCAL_SPEAKERS_JSON':str(p)}):
    self.assertEqual(module.resolve_speaker('pipecat_male',1.,settings),('pipecat_male','/male.wav'))
    self.assertEqual(module.resolve_speaker('female11',1.,settings),('female11','/female.wav'))
    self.assertEqual(len(module.get_available_audios(settings)),3)
    self.assertEqual(module.resolve_speaker('legacy',1.,settings),('legacy','/legacy.wav'))
 def test_no_mapping_preserves_original_fixed_behavior(self):
  settings=SimpleNamespace(RESOURCE_MODE='fixed',FIXED_SPEAKER_ID='legacy',FIXED_SPEAKER_WAV_PATH='/legacy.wav')
  with patch.dict(os.environ,{'LOCAL_SPEAKERS_JSON':''}):self.assertEqual(module.resolve_speaker('anything',1.,settings),('legacy','/legacy.wav'))
