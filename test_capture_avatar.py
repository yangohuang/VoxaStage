import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from capture_avatar import run
from prepare_avatar_cases import prepare
from test_avatar_eval_capture import jpeg

class CaptureRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_captures_all_cases_and_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            async def synth(text): yield b'\x01\x00'*960
            await prepare(root/'inputs',synth,voice_label='test')
            class Backend:
                async def stream(self,source):
                    pcm=b''.join([x async for x in source])
                    yield dict(type='avatar_meta',kind='2d',codec='jpeg',width=4,height=4,fps=25,sample_rate=24000)
                    yield dict(type='media',frame_index=0,start_sample=0,pts=0,
                               audio=base64.b64encode(pcm).decode(),image=base64.b64encode(jpeg()).decode())
                    yield dict(type='clip_end',total_samples=len(pcm)//2)
            args=SimpleNamespace(manifest=root/'inputs/cases.json',output=root/'captures',provider='flashhead',config=None,timeout=1)
            report=await run(args,backend_factory=Backend)
            self.assertEqual(report['status'],'passed')
            self.assertEqual(len(report['clips']),15)
            self.assertEqual(report['clips'][0]['case']['category'],'zh')
            self.assertTrue((args.output/'zh-00/output.wav').exists())
            self.assertFalse(report['perceptual_quality_measured'])

    async def test_failure_is_recorded_without_backend_error_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            async def synth(text): yield b'\x01\x00'*960
            await prepare(root/'inputs',synth,voice_label='test')
            class Broken:
                async def stream(self,source):
                    raise RuntimeError('ws://secret@host')
                    yield
            args=SimpleNamespace(manifest=root/'inputs/cases.json',output=root/'captures',provider='flashhead',config=None,timeout=1)
            report=await run(args,backend_factory=Broken)
            self.assertEqual(report['status'],'failed')
            self.assertTrue(all(x['status']=='failed' for x in report['clips']))
            self.assertNotIn('secret',(args.output/'results.json').read_text())

if __name__=='__main__': unittest.main()
