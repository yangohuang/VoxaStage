import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from prepare_avatar_cases import prepare
from test_avatar_eval_capture import jpeg
from trace_dinet import run

class TraceTests(unittest.IsolatedAsyncioTestCase):
 async def test_same_pcm_warmup_and_repeats_with_ordered_events(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   async def synth(text):yield b'\x01\x00'*960
   await prepare(root/'inputs',synth,voice_label='test')
   observers=[]
   class Backend:
    def __init__(self,observe):self.observe=observe;observers.append(observe)
    async def stream(self,source):
     pcm=b''.join([chunk async for chunk in source])
     yield dict(type='avatar_meta',kind='2d',codec='jpeg',width=4,height=4,fps=25,sample_rate=24000)
     self.observe(dict(type='encode_start',frame=0));self.observe(dict(type='encode_end',frame=0))
     yield dict(type='media',frame_index=0,start_sample=0,pts=0,audio=base64.b64encode(pcm).decode(),image=base64.b64encode(jpeg()).decode())
     yield dict(type='clip_end',total_samples=len(pcm)//2)
   args=SimpleNamespace(output=root/'trace',manifest=root/'inputs/cases.json',case='zh-00',config=None,rounds=2,timeout=1,deployment_label='mock')
   report=await run(args,backend_factory=Backend)
   self.assertEqual(report['status'],'passed');self.assertEqual(len(report['trials']),3)
   self.assertEqual([r['phase'] for r in report['trials']],['warmup','measured','measured'])
   for row in report['trials']:
    self.assertEqual([e['type'] for e in row['events']],['encode_start','encode_end','paired_media'])
    self.assertEqual(sorted(e['at_ms'] for e in row['events']),[e['at_ms'] for e in row['events']])
 async def test_failure_is_saved_without_raw_exception(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   async def synth(text):yield b'\x01\x00'*960
   await prepare(root/'inputs',synth,voice_label='test')
   class Backend:
    def __init__(self,observe):pass
    async def stream(self,source):
     raise RuntimeError('ws://private-service?token=secret')
     yield
   args=SimpleNamespace(output=root/'trace',manifest=root/'inputs/cases.json',case='zh-00',config=None,rounds=1,timeout=1,deployment_label='mock')
   report=await run(args,backend_factory=Backend)
   self.assertEqual(report['status'],'failed');self.assertTrue(all(r['status']=='failed' for r in report['trials']))
   self.assertNotIn('secret',(root/'trace/results.json').read_text())

if __name__=='__main__':unittest.main()
