import asyncio
import io
import threading
import unittest
import wave

import httpx
import numpy as np

from asr_server import create_app


def wav(seconds=.2, rate=16000, channels=1, value=100):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes(np.full(int(seconds * rate * channels), value, '<i2').tobytes())
    return out.getvalue()


class ASRTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_bounded_and_passed_without_rewriting_audio(self):
        seen=[]
        def decode(audio,context=''):
            seen.append(context);return 'Pipecat agent'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(decode,'test',supports_context=True)),base_url='http://test') as c:
            result=await c.post('/transcribe',content=wav(),headers={'X-ASR-Context':'Pipecat, MiniCPM-o, agent'})
            self.assertEqual(result.json()['text'],'Pipecat agent')
            self.assertEqual(seen,['Pipecat, MiniCPM-o, agent'])
            self.assertEqual((await c.post('/transcribe',content=wav(),headers={'X-ASR-Context':'x'*501})).status_code,400)

    async def test_audio_contract_and_silence(self):
        calls = []
        def decode(audio):
            calls.append(audio)
            return '实时语音交互'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(decode, 'test')), base_url='http://test') as c:
            self.assertEqual((await c.get('/health')).json()['model'], 'test')
            for audio in [b'invalid', wav(rate=8000), wav(channels=2), wav(seconds=30.1), wav()[:-10]]:
                self.assertEqual((await c.post('/transcribe', content=audio)).status_code, 400)
            self.assertEqual((await c.post('/transcribe', content=b'x' * 2_000_001)).status_code, 413)
            self.assertEqual((await c.post('/transcribe', content=wav(value=0))).json()['text'], '')
            self.assertEqual(calls, [])
            result = (await c.post('/transcribe', content=wav())).json()
            self.assertEqual(result['text'], '实时语音交互')
            self.assertAlmostEqual(result['audio_s'], .2)
            self.assertEqual(calls[0].dtype, np.float32)

    async def test_busy_and_recovery_after_inference_error(self):
        entered, release = threading.Event(), threading.Event()
        def decode(audio):
            entered.set()
            release.wait(5)
            raise RuntimeError('inference failed')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(decode, 'test'), raise_app_exceptions=False), base_url='http://test') as c:
            task = asyncio.create_task(c.post('/transcribe', content=wav()))
            await asyncio.to_thread(entered.wait, 2)
            try:
                self.assertTrue((await c.get('/health')).json()['busy'])
                self.assertEqual((await c.post('/transcribe', content=wav())).status_code, 409)
            finally:
                release.set()
                response = await task
            self.assertEqual(response.status_code, 500)
            self.assertFalse((await c.get('/health')).json()['busy'])
            self.assertEqual((await c.post('/transcribe', content=wav(value=0))).status_code, 200)


if __name__ == '__main__':
    unittest.main()
