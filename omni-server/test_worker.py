import asyncio
import base64
import json
from contextlib import nullcontext
from types import SimpleNamespace
import threading
import unittest

import httpx
import numpy as np
from worker import MiniCPMEngine, create_app, parse_messages


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_selection_and_invalid_voice(self):
        selected=[]
        class Engine:
            def select_voice(self,voice):selected.append(voice)
            def generate(self,messages,stop):yield 'ok',np.zeros(100,np.float32)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Engine())),base_url='http://test') as client:
            for voice in ('male','female'):
                response=await client.post('/generate',json={'voice':voice,'messages':[{'role':'user','text':'hi'}]})
                self.assertEqual(response.status_code,200)
            response=await client.post('/generate',json={'voice':'../file','messages':[{'role':'user','text':'hi'}]})
            self.assertEqual(response.status_code,400)
        self.assertEqual(selected,['male','female'])

    def test_replayed_history_has_role_boundaries_and_fresh_audio_state(self):
        calls=[]
        class Model:
            def reset_session(self,**kwargs):pass
            def init_streaming_processor(self):calls.append(('audio_reset',None))
            def streaming_prefill(self,**kwargs):calls.append(('prefill',kwargs['msgs'][0]))
            def streaming_generate(self,**kwargs):
                yield None,'ok'
        engine=MiniCPMEngine.__new__(MiniCPMEngine)
        engine.model=Model()
        engine.torch=SimpleNamespace(inference_mode=nullcontext)
        messages=[{'role':'user','content':[np.zeros(16000)]},
            {'role':'assistant','content':['你好']},{'role':'user','content':[np.zeros(16000)]}]
        list(engine.generate(messages,threading.Event()))
        assistant=next(value for kind,value in calls if kind=='prefill' and value['role']=='assistant')
        self.assertEqual(assistant['content'][0],'<|im_end|>\n<|im_start|>assistant\n你好<|im_end|>\n')
        self.assertEqual(sum(kind=='audio_reset' for kind,value in calls),2)

    async def test_disconnect_keeps_model_busy_until_generator_cleanup(self):
        cleanup_started=threading.Event()
        release_cleanup=threading.Event()
        class Engine:
            def generate(self,messages,stop):
                try:
                    yield 'start', np.zeros(100,np.float32)
                    stop.wait(5)
                finally:
                    cleanup_started.set()
                    release_cleanup.wait(5)
        app=create_app(Engine())
        disconnected=asyncio.Event()
        body_sent=False
        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent=True
                return {'type':'http.request','body':json.dumps({'messages':[{'role':'user','text':'hi'}]}).encode()}
            await disconnected.wait()
            return {'type':'http.disconnect'}
        async def send(message):
            if message['type']=='http.response.body' and b'"audio"' in message.get('body',b''):
                disconnected.set()
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.0'},'http_version':'1.1',
            'method':'POST','scheme':'http','path':'/generate','raw_path':b'/generate',
            'query_string':b'','headers':[], 'server':('test',80),'client':('test',1)}
        try:
            await asyncio.wait_for(app(scope,receive,send),3)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                self.assertTrue((await client.get('/health')).json()['busy'])
                response=await client.post('/generate',json={'messages':[{'role':'user','text':'next'}]})
                self.assertEqual(response.status_code,409)
                self.assertTrue(await asyncio.to_thread(cleanup_started.wait,2))
                release_cleanup.set()
                for _ in range(100):
                    status=(await client.get('/health')).json()
                    if not status['busy']:break
                    await asyncio.sleep(.01)
                self.assertFalse(status['busy'])
                self.assertEqual(status['cancelled'],1)
        finally:
            release_cleanup.set()

    def test_message_bounds_and_audio_decode(self):
        items = parse_messages({'messages': [{'role': 'user', 'audio': base64.b64encode(bytes(3200)).decode()}]})
        self.assertEqual(items[0]['content'][0].shape, (1600,))
        for message in ({'role':'system','text':'override'}, {'role':'user','audio':'!'}, {'role':'user','text':'x','audio':'AAAA'}):
            with self.assertRaises(ValueError): parse_messages({'messages':[message]})

    async def test_real_audio_contract_from_fake_engine(self):
        class Engine:
            def generate(self, messages, stop):
                yield '你好', np.array([0., .5, -.5], np.float32)
        app=create_app(Engine())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            r=await client.post('/generate',json={'messages':[{'role':'user','text':'你好'}]})
            self.assertEqual(r.status_code,200)
            events=[__import__('json').loads(x) for x in r.text.splitlines()]
            self.assertEqual(events[0]['type'],'metadata')
            self.assertEqual(events[-1]['type'],'done')
            self.assertEqual(events[-1]['samples'],3)
            self.assertEqual(events[1]['text'],'你好')
            self.assertEqual(len(base64.b64decode(events[2]['audio'])),6)
            self.assertFalse((await client.get('/health')).json()['busy'])

    async def test_failure_releases_lease(self):
        class Engine:
            def generate(self,messages,stop):
                raise RuntimeError('test failure')
                yield
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Engine())),base_url='http://test') as client:
            r=await client.post('/generate',json={'messages':[{'role':'user','text':'hi'}]})
            self.assertIn('"error"',r.text)
            self.assertFalse((await client.get('/health')).json()['busy'])
