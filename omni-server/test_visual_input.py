import base64
import io
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
import httpx
import numpy as np
from PIL import Image
from worker import MiniCPMEngine, create_app, parse_messages


def picture(identifier='frame-1', size=(64, 64), fmt='PNG'):
    data=io.BytesIO();Image.new('RGB',size,'red').save(data,format=fmt)
    return dict(id=identifier,source='upload',captured_at_ms=1250.5,data=base64.b64encode(data.getvalue()).decode())


class VisualInputTests(unittest.IsolatedAsyncioTestCase):
    def test_image_and_audio_preserved_with_capture_metadata(self):
        msg={'role':'user','audio':base64.b64encode(bytes(64000)).decode(),'images':[picture()]}
        parsed=parse_messages({'messages':[msg]},vision_enabled=True)
        content=parsed[0]['content']
        self.assertEqual(sum(isinstance(x,Image.Image) for x in content),1)
        self.assertEqual(next(x for x in content if isinstance(x,Image.Image)).size,(64,64))
        self.assertEqual(next(x for x in content if isinstance(x,np.ndarray)).shape,(32000,))
        self.assertTrue(any(isinstance(x,str) and 'frame-1' in x and '1250.5' in x for x in content))

    def test_disabled_rejects_instead_of_ignoring_images(self):
        with self.assertRaises(ValueError):parse_messages({'messages':[{'role':'user','text':'看图','images':[picture()]}]})

    def test_invalid_media_metadata_count_and_roles_rejected(self):
        bad=[dict(picture(),data='!'),dict(picture(),source='server'),dict(picture(),captured_at_ms=True),
             dict(picture(),captured_at_ms=float('nan')),dict(picture(),captured_at_ms=10**400),dict(picture(),captured_at_ms=-1),dict(picture(),id='../x'),
             dict(picture(),data='A'*350000),picture(size=(1025,1)),picture(fmt='GIF'),dict(picture(),extra='x')]
        for image in bad:
            with self.subTest(image={k:v for k,v in image.items() if k!='data'}),self.assertRaises(ValueError):
                parse_messages({'messages':[{'role':'user','text':'看图','images':[image]}]},vision_enabled=True)
        for messages in [
            [{'role':'user','text':'看图','images':[picture(),picture()]}],
            [{'role':'user','text':'看图','images':[picture(str(i)) for i in range(3)]}],
            [{'role':'assistant','text':'x','images':[picture()]},{'role':'user','text':'y'}],
            [{'role':'user','text':'x','images':[picture('a'),picture('b')]},{'role':'assistant','text':'x'},{'role':'user','text':'y','images':[picture('c')]}],
        ]:
            with self.assertRaises(ValueError):parse_messages({'messages':messages},vision_enabled=True)

    def test_image_sent_once_before_chunked_audio(self):
        calls=[]
        class Model:
            def reset_session(self,**kwargs):pass
            def init_streaming_processor(self):pass
            def streaming_prefill(self,**kwargs):calls.append(kwargs)
            def streaming_generate(self,**kwargs):yield None,'ok'
        engine=MiniCPMEngine.__new__(MiniCPMEngine);engine.model=Model();engine.torch=SimpleNamespace(inference_mode=nullcontext)
        image=Image.new('RGB',(16,16));samples=np.arange(35000,dtype=np.float32)
        list(engine.generate([{'role':'user','content':['frame-1',image,samples]}],threading.Event()))
        user=[c for c in calls if c['msgs'][0]['role']=='user']
        self.assertEqual(len(user),3)
        self.assertEqual(sum(isinstance(x,Image.Image) for c in user for x in c['msgs'][0]['content']),1)
        np.testing.assert_array_equal(np.concatenate([x for c in user for x in c['msgs'][0]['content'] if isinstance(x,np.ndarray)]),samples)
        self.assertEqual([c['is_last_chunk'] for c in user],[False,False,True])

    async def test_health_and_http_rejection_match_engine_capability(self):
        class Engine:
            def __init__(self,enabled):self.vision_enabled=enabled;self.calls=[]
            def generate(self,messages,stop):self.calls.append(messages);yield 'ok',np.zeros(100,np.float32)
        for enabled in (False,True):
            engine=Engine(enabled)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)),base_url='http://test') as client:
                health=(await client.get('/health')).json()
                self.assertEqual(health['capabilities']['image_input'],enabled)
                response=await client.post('/generate',json={'messages':[{'role':'user','text':'看图','images':[picture()]}]})
                self.assertEqual(response.status_code,200 if enabled else 400)
                self.assertEqual(len(engine.calls),int(enabled))
