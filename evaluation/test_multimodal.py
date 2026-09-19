import asyncio,base64,hashlib,json,tempfile,unittest,wave
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from multimodal import load_cases,build_messages,run

class MultimodalTests(unittest.IsolatedAsyncioTestCase):
 def fixture(self,root):
  root=Path(root);Image.new('RGB',(32,32),'red').save(root/'image.png')
  with wave.open(str(root/'question.wav'),'wb') as f:f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(bytes(3200))
  sha=lambda name:hashlib.sha256((root/name).read_bytes()).hexdigest()
  m=dict(version=1,cases=[dict(id='color-a',image='image.png',image_sha256=sha('image.png'),audio='question.wav',audio_sha256=sha('question.wav'),question_text='左边是什么颜色？',transcript_reviewed=False,source='synthetic',expected_facts=['红色'])])
  path=root/'cases.json';path.write_text(json.dumps(m,ensure_ascii=False));return path
 def test_input_hashes_and_ablation_do_not_leak_reference_answer(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);case=load_cases(p)[0]
   for mode in ('audio_only','image_only','image_text','image_audio'):
    msg=build_messages(case,mode)[0]
    self.assertNotIn('红色',json.dumps(msg,ensure_ascii=False))
    self.assertEqual('images' in msg,mode!='audio_only')
    self.assertEqual('audio' in msg,mode in ('audio_only','image_audio'))
   (Path(d)/'image.png').write_bytes(b'changed')
   with self.assertRaises(ValueError):load_cases(p)
 def test_manifest_rejects_duplicate_cases(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);m=json.loads(p.read_text());m['cases'].append(m['cases'][0]);p.write_text(json.dumps(m))
   with self.assertRaises(ValueError):load_cases(p)
 async def test_rotated_order_failure_denominator_and_unrated_semantics(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);calls=[]
   class Backend:
    async def generate(self,messages):
     calls.append(messages)
     if len(calls)==3:raise RuntimeError('injected failure')
     yield 'text','真实返回的测试文字';yield 'audio',bytes(100)
   args=SimpleNamespace(manifest=p,output=Path(d)/'result',rounds=2,timeout=2,url='unused',voice='female',save_audio=False)
   report=await run(args,backend=Backend())
   self.assertEqual(report['status'],'failed');self.assertEqual(len(report['trials']),9)
   rows=[x for x in report['trials'] if x['phase']=='measured']
   self.assertEqual([x['mode'] for x in rows],['audio_only','image_only','image_text','image_audio','image_only','image_text','image_audio','audio_only'])
   self.assertEqual(sum(x['failed'] for x in report['summary'].values()),1)
   self.assertTrue(all(x['semantic_assessment'] is None for x in rows))
   self.assertEqual(report['execution'],'injected_backend')
   self.assertEqual(json.loads((args.output/'results.json').read_text())['status'],'failed')
 async def test_timeout_closes_generator_and_keeps_failure_record(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);closed=[]
   class Backend:
    async def generate(self,messages):
     try:yield 'text','partial';await asyncio.sleep(1)
     finally:closed.append(True)
   args=SimpleNamespace(manifest=p,output=Path(d)/'result',rounds=1,timeout=.01,url='unused',voice='female',save_audio=False)
   result=await run(args,backend=Backend())
   self.assertEqual(len(closed),5);self.assertEqual(result['status'],'failed')
   self.assertTrue(all(x['status']=='failed' and x['text']=='partial' for x in result['trials']))

 async def test_manifest_digest_binds_the_same_parsed_snapshot(self):
  from unittest.mock import patch
  import multimodal
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);original=p.read_bytes();loader=multimodal.load_cases
   def mutate_after_loading(path,**kwargs):
    cases=loader(path,**kwargs);path.write_text('{"changed":true}');return cases
   class Backend:
    async def generate(self,messages):yield 'audio',bytes(2)
   args=SimpleNamespace(manifest=p,output=Path(d)/'result',rounds=1,timeout=2,url='unused',voice='female',save_audio=False)
   with patch('multimodal.load_cases',side_effect=mutate_after_loading):report=await run(args,backend=Backend())
   self.assertEqual(report['manifest_sha256'],hashlib.sha256(original).hexdigest())
   self.assertNotEqual(report['manifest_sha256'],hashlib.sha256(p.read_bytes()).hexdigest())
   self.assertEqual(report['cases'][0]['id'],'color-a')

 async def test_http_client_checks_capability_before_sending_inputs(self):
  import httpx
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);requests=[]
   def handle(request):
    requests.append(request)
    return httpx.Response(200,json={'ready':True,'capabilities':{'image_input':False}})
   client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
   args=SimpleNamespace(manifest=p,output=Path(d)/'result',rounds=1,timeout=2,url='http://worker.test',voice='female',save_audio=False)
   with patch('multimodal.httpx.AsyncClient',return_value=client):
    with self.assertRaises(ValueError):await run(args)
   self.assertEqual([r.url.path for r in requests],['/health'])
   self.assertTrue(client.is_closed)
   self.assertEqual(json.loads((args.output/'results.json').read_text())['status'],'failed')

 async def test_http_stream_failure_is_counted_and_saved_pcm_is_valid(self):
  import httpx
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);requests=[]
   def handle(request):
    if request.url.path=='/health':
     return httpx.Response(200,json={'ready':True,'capabilities':{'image_input':True},'model':'protocol-test-double'})
    requests.append(json.loads(request.content))
    events=[dict(type='metadata',protocol=1,sample_rate=24000),dict(type='text',text='fixture'),dict(type='audio',audio=base64.b64encode(bytes(8)).decode())]
    if len(requests)!=2:events.append(dict(type='done',samples=4))
    return httpx.Response(200,content=''.join(json.dumps(e)+'\n' for e in events))
   client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
   args=SimpleNamespace(manifest=p,output=Path(d)/'result',rounds=1,timeout=2,url='http://worker.test',voice='male',save_audio=True)
   with patch('multimodal.httpx.AsyncClient',return_value=client):report=await run(args)
   self.assertEqual(len(requests),5)
   self.assertTrue(all(r['voice']=='male' for r in requests))
   self.assertEqual(report['status'],'failed')
   self.assertEqual(report['summary']['audio_only']['failed'],1)
   self.assertEqual(report['trials'][1]['error_type'],'ValueError')
   self.assertEqual(report['trials'][1]['output_samples'],4)
   self.assertTrue(client.is_closed)
   with wave.open(str(args.output/'001.wav'),'rb') as wav:
    self.assertEqual((wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes()),(1,2,24000,4))
