import json
import unittest
from unittest.mock import patch
import httpx
from backend import LocalBackend

class VoiceMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_character_voices_reach_tts_without_global_override(self):
        sent=[]
        class Socket:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send(self,message):sent.append(json.loads(message))
            def __aiter__(self):return self.receive()
            async def receive(self):
                yield bytes(480)
                yield '{"is_end":1}'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'speaker_ids':['pipecat_male','female11']}))) as client:
            with patch('backend.websockets.connect',return_value=Socket()),patch.dict('os.environ',{'PIPECAT_SPEAKER_ID':'wrong-global'}):
                for voice,expected in [('male','pipecat_male'),('female','female11')]:
                    sent.clear()
                    pcm=b''.join([x async for x in LocalBackend(client,voice=voice).synthesize('hello')])
                    self.assertEqual(len(pcm),480)
                    self.assertTrue(all(x['audio_id']==expected for x in sent))
