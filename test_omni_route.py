import base64
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from avatar_demo import register_avatar_routes

class OmniRouteTests(unittest.TestCase):
    def test_pipecat_branch_bypasses_cascade_and_streams_to_avatar(self):
        observed=[]
        def respond(request):
            if request.url.path=='/health':return httpx.Response(200,json={'ready':True})
            observed.append(json.loads(request.content))
            events=[{'type':'metadata','protocol':1,'sample_rate':24000},
                {'type':'text','text':'端到端回复'},
                {'type':'audio','audio':base64.b64encode(bytes(1920)).decode()},
                {'type':'done','samples':960}]
            return httpx.Response(200,text='\n'.join(map(json.dumps,events))+'\n')
        class Renderer:
            async def stream(self,source):
                audio=b''.join([x async for x in source])
                yield {'type':'avatar_meta','kind':'2d','sample_rate':24000,'fps':25,'width':16,'height':16,'codec':'jpeg'}
                yield {'type':'media','frame_index':0,'start_sample':0,'pts':0,'image':'test-only','audio':base64.b64encode(audio).decode()}
                yield {'type':'clip_end','total_samples':len(audio)//2}
        provider=SimpleNamespace(id='dinet',kind='2d',voice='male',make_backend=Renderer)
        registry=SimpleNamespace(resolve=lambda name:provider,public=lambda:{})
        real_client=httpx.AsyncClient
        def client(**kwargs):return real_client(transport=httpx.MockTransport(respond),**kwargs)
        def forbidden(backend):raise AssertionError('Cascade must not run for end-to-end model')
        app=FastAPI();sessions=set();workers={}
        with patch.dict('os.environ',{'PIPECAT_MINICPM_URL':'http://worker'}), patch('avatar_demo.ProviderRegistry',return_value=registry), patch('avatar_demo.httpx.AsyncClient',side_effect=client):
            register_avatar_routes(app,forbidden,sessions,workers)
            with TestClient(app) as client:
                with client.websocket_connect('/avatar/ws?provider=dinet&backend=minicpm') as ws:
                    ready=ws.receive_json()
                    self.assertEqual(ready['dialogue_backend'],'minicpm')
                    ws.send_json({'type':'text','text':'你好'})
                    events=[]
                    while True:
                        event=ws.receive_json();events.append(event)
                        if event['type'] in ('clip_end','error'):break
                    self.assertEqual(events[-1]['type'],'clip_end',events)
                    self.assertEqual(events[-1]['total_samples'],960)
                    self.assertTrue(any(e.get('text')=='端到端回复' for e in events))
                deadline=time.monotonic()+5
                while sessions and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertFalse(sessions)
                self.assertFalse(workers)
        self.assertEqual(observed[-1]['messages'][-1],{'role':'user','text':'你好'})
        self.assertFalse(sessions)
        self.assertFalse(workers)
