import base64,json,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from avatar_demo import register_avatar_routes
from test_visual_context import frame

class VisualRouteTests(unittest.TestCase):
    def test_confirmed_picture_reaches_model_and_old_picture_is_trimmed(self):
        requests=[]
        def respond(request):
            if request.url.path=='/health':return httpx.Response(200,json=dict(ready=True,capabilities=dict(image_input=True)))
            requests.append(json.loads(request.content))
            events=[dict(type='metadata',protocol=1,sample_rate=24000),dict(type='text',text='模拟回复'),
                    dict(type='audio',audio=base64.b64encode(bytes(1920)).decode()),dict(type='done',samples=960)]
            return httpx.Response(200,text='\n'.join(map(json.dumps,events))+'\n')
        class Renderer:
            async def stream(self,source):
                audio=b''.join([x async for x in source])
                yield dict(type='avatar_meta',kind='2d',sample_rate=24000,fps=25,width=16,height=16,codec='jpeg')
                yield dict(type='media',frame_index=0,start_sample=0,pts=0,image='test-only',audio=base64.b64encode(audio).decode())
                yield dict(type='clip_end',total_samples=len(audio)//2)
        registry=SimpleNamespace(resolve=lambda name:SimpleNamespace(id='dinet',kind='2d',voice='male',make_backend=Renderer),public=lambda:{})
        original_client=httpx.AsyncClient
        app=FastAPI();sessions=set();workers={}
        with patch.dict('os.environ',{'PIPECAT_MINICPM_URL':'http://worker'}),patch('avatar_demo.ProviderRegistry',return_value=registry),patch('avatar_demo.httpx.AsyncClient',side_effect=lambda **kw:original_client(transport=httpx.MockTransport(respond),**kw)):
            register_avatar_routes(app,lambda b:self.fail('Unexpected cascade'),sessions,workers)
            with TestClient(app) as client:
                providers=client.get('/avatar/providers').json()
                self.assertTrue(next(b for b in providers['dialogue_backends'] if b['id']=='minicpm')['image_input'])
                with client.websocket_connect('/avatar/ws?provider=dinet&backend=minicpm') as ws:
                    ready=ws.receive_json();self.assertTrue(ready['image_input'])
                    for n in range(1,4):
                        message=frame(n);message['session_id']=ready['session_id'];ws.send_json(message)
                        self.assertEqual(ws.receive_json()['type'],'visual_ack')
                        ws.send_json(dict(type='text',text='看这张图'))
                        events=[]
                        while True:
                            event=ws.receive_json();events.append(event)
                            if event['type'] in ('clip_end','error'):break
                        self.assertEqual(events[-1]['type'],'clip_end',events)
                        bound=next(e for e in events if e['type']=='visual_bound')
                        self.assertEqual(bound['turn_id'],n)
                        self.assertEqual(bound['images'][0]['id'],f'image-{n}')
                        self.assertEqual(requests[-1]['messages'][-1]['images'][0]['id'],f'image-{n}')
                    messages=requests[-1]['messages']
                    self.assertEqual(sum(len(m.get('images',[])) for m in messages),2)
                    self.assertTrue(messages[0]['images_omitted'])
                    ws.send_json(dict(frame(4),session_id='old-session'))
                    self.assertEqual(ws.receive_json()['type'],'visual_error')
                deadline=time.monotonic()+5
                while sessions and time.monotonic()<deadline:time.sleep(.01)
                self.assertFalse(sessions);self.assertFalse(workers)
