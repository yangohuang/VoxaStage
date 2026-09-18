import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from aiohttp import ClientSession, web
from avatar_playback_lab import make_app
from prepare_avatar_cases import prepare
from test_avatar_eval_capture import jpeg

class LabTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  async def synth(text):yield b'\x01\x00'*960
  await prepare(self.root/'inputs',synth,voice_label='test')
  self.closed=False
  owner=self
  class Backend:
   async def stream(self,source):
    try:
     pcm=b''.join([x async for x in source])
     yield dict(type='avatar_meta',kind='2d',codec='jpeg',width=4,height=4,fps=25,sample_rate=24000)
     yield dict(type='media',frame_index=0,start_sample=0,pts=0,audio=base64.b64encode(pcm).decode(),image=base64.b64encode(jpeg()).decode())
     yield dict(type='clip_end',total_samples=len(pcm)//2)
    finally:owner.closed=True
  self.factories={'dinet':Backend}
  self.app=make_app(self.root/'inputs/cases.json',factories=self.factories,timeout=1)
  self.runner=web.AppRunner(self.app);await self.runner.setup()
  site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
  self.url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
  self.client=ClientSession()
 async def asyncTearDown(self):
  await self.client.close();await self.runner.cleanup();self.tmp.cleanup()
 async def test_fixed_input_completes_and_closes(self):
  async with self.client.ws_connect(self.url+'/ws?provider=dinet&case=zh-00',origin=self.url) as ws:
   events=[]
   async for msg in ws:
    events.append(msg.json())
  self.assertEqual([x['type'] for x in events],['avatar_meta','media','clip_end','stream_end'])
  self.assertEqual(events[-1]['status'],'passed');self.assertTrue(self.closed)
  self.assertTrue(all('adapter_elapsed_ms' in x for x in events))
 async def test_invalid_choice_and_cross_origin_are_rejected(self):
  async with self.client.get(self.url+'/ws?provider=unknown&case=zh-00',headers={'Origin':self.url}) as r:self.assertEqual(r.status,400)
  async with self.client.get(self.url+'/ws?provider=dinet&case=zh-00',headers={'Origin':'http://evil.invalid'}) as r:self.assertEqual(r.status,403)
 async def test_config_never_exposes_local_paths(self):
  async with self.client.get(self.url+'/config') as r:data=await r.json()
  self.assertEqual(len(data['cases']),15);self.assertNotIn(str(self.root),str(data))

 async def test_backend_failure_is_sanitized(self):
  class Broken:
   async def stream(self,source):
    raise RuntimeError('ws://secret-service?token=private')
    yield
  self.factories['dinet']=Broken
  async with self.client.ws_connect(self.url+'/ws?provider=dinet&case=zh-00',origin=self.url) as ws:
   data=(await ws.receive()).json()
  self.assertEqual(data['status'],'failed');self.assertNotIn('secret',str(data))
 async def test_busy_rejection_and_disconnect_cleanup(self):
  started=asyncio.Event();closed=asyncio.Event()
  class Waiting:
   async def stream(self,source):
    try:
     started.set();await asyncio.Event().wait();yield {}
    finally:closed.set()
  self.factories['dinet']=Waiting
  ws=await self.client.ws_connect(self.url+'/ws?provider=dinet&case=zh-00',origin=self.url)
  await asyncio.wait_for(started.wait(),1)
  async with self.client.get(self.url+'/ws?provider=dinet&case=zh-00',headers={'Origin':self.url}) as r:self.assertEqual(r.status,409)
  await ws.close();await asyncio.wait_for(closed.wait(),1)
 async def test_timeout_is_terminal_and_closes_backend(self):
  closed=asyncio.Event()
  class Waiting:
   async def stream(self,source):
    try:await asyncio.Event().wait();yield {}
    finally:closed.set()
  self.factories['dinet']=Waiting
  async with self.client.ws_connect(self.url+'/ws?provider=dinet&case=zh-00',origin=self.url) as ws:
   data=(await ws.receive()).json()
  self.assertEqual(data['status'],'failed');self.assertEqual(data['error_type'],'TimeoutError');self.assertTrue(closed.is_set())
 async def test_foreign_host_is_rejected_even_with_matching_origin(self):
  async with self.client.get(self.url+'/config',headers={'Host':'evil.invalid'}) as r:self.assertEqual(r.status,403)

if __name__=='__main__':unittest.main()
