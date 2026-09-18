import json
import unittest
import httpx
from api_backends import configured_api

class APITests(unittest.IsolatedAsyncioTestCase):
    def test_opt_in_and_configuration_validation(self):
        self.assertIsNone(configured_api('llm',{}))
        with self.assertRaises(ValueError):configured_api('llm',{'PIPECAT_LLM_MODE':'api'})
        with self.assertRaises(ValueError):configured_api('llm',{'PIPECAT_LLM_MODE':'unknown'})

    async def test_streaming_chat_and_truncated_stream(self):
        config={'PIPECAT_LLM_MODE':'api','PIPECAT_LLM_API_BASE':'https://example.test/v1','PIPECAT_LLM_API_MODEL':'test','PIPECAT_LLM_API_KEY':'test-key'}
        api=configured_api('llm',config)
        def handler(r):
            self.assertEqual(str(r.url),'https://example.test/v1/chat/completions')
            self.assertEqual(r.headers['Authorization'],'Bearer test-key')
            self.assertTrue(json.loads(r.content)['stream'])
            return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: [DONE]\n\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            self.assertEqual([x async for x in api.generate(c,[{'role':'user','content':'hi'}])],['你好'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='data: {"choices":[]}\n\n'))) as c:
            with self.assertRaises(ValueError):_=[x async for x in api.generate(c,[])]

    async def test_transcription_multipart_keeps_source_audio(self):
        api=configured_api('asr',{'PIPECAT_ASR_MODE':'api','PIPECAT_ASR_API_BASE':'https://example.test/v1','PIPECAT_ASR_API_MODEL':'speech','PIPECAT_ASR_API_KEY':'test-key'})
        def handler(r):
            self.assertTrue(r.url.path.endswith('/audio/transcriptions'))
            self.assertIn(b'original-wav',r.content)
            return httpx.Response(200,json={'text':'语音 agent'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            self.assertEqual(await api.transcribe(c,b'original-wav'),'语音 agent')
