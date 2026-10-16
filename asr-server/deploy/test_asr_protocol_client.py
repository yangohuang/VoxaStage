import asyncio
import importlib.util
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'evaluation'))
from test_asr_capture import fixture, health, result
import httpx


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p=Path(__file__).with_name('protocol_client.py')
        self.assertTrue(p.exists(), 'ASR admission client missing')
        spec=importlib.util.spec_from_file_location('asr_protocol_client',p)
        self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)

    async def exercise(self, *, deny=False, fail=False, mismatch=False):
        with tempfile.TemporaryDirectory() as d:
            args, audio, _=fixture(d); args.rounds=1
            rows=[{'case':str(i),'phase':phase,'repeat':repeat,'case_id':'mixed','variant':variant}
                  for i,(phase,repeat,variant) in enumerate([('warmup',-1,'plain'),('warmup',-1,'terms'),('measured',0,'plain'),('measured',0,'terms')])]
            for row in rows:
                row['audio_sha256']=hashlib.sha256(audio).hexdigest()
                row['context_sha256']=hashlib.sha256(('' if row['variant']=='plain' else 'agent, Pipecat').encode()).hexdigest()
            if mismatch:rows[0]['variant']='wrong'
            events=[];posts=[];admitted=[]
            async def admit(case):
                if deny:raise TimeoutError('denied')
                admitted.append(case)
            async def handler(request):
                if request.method=='GET':return health()
                posts.append(request.content)
                return httpx.Response(500) if fail else result()
            manifest={'model_name':'test-model','timeouts_s':{'startup':1},'requests':rows}
            report=await self.mod.collect(args,manifest,emit=events.append,admit=admit,transport=httpx.MockTransport(handler))
            return report,events,posts,admitted

    async def test_real_capture_schedule_admitted_and_report_emitted(self):
        report,events,posts,admitted=await self.exercise()
        self.assertEqual(report['status'],'passed');self.assertEqual(admitted,['0','1','2','3'])
        self.assertEqual(len(posts),4)
        self.assertEqual(events[-1],{'event':'finished','requests':4})
        self.assertEqual(next(e for e in events if e['event']=='capture')['report'],report)

    async def test_admission_timeout_sends_no_audio(self):
        report,events,posts,_=await self.exercise(deny=True)
        self.assertEqual(report['status'],'failed');self.assertFalse(posts)
        self.assertEqual(events[-1]['event'],'failed')

    async def test_first_server_error_stops_batch_and_preserves_trial(self):
        report,events,posts,_=await self.exercise(fail=True)
        self.assertEqual(len(posts),1);self.assertEqual(report['trials'][0]['http_status'],500)
        self.assertEqual(events[-1]['event'],'failed')

    async def test_frozen_schedule_mismatch_sends_no_audio(self):
        report,events,posts,_=await self.exercise(mismatch=True)
        self.assertEqual(report['status'],'failed');self.assertFalse(posts)

if __name__=='__main__':unittest.main()
