import asyncio
import base64
import json
import unittest
import httpx
from omni_backend import OmniBackend, ConversationHistory


class OmniTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_is_sent_with_model_request(self):
        def respond(request):
            self.assertEqual(json.loads(request.content)['voice'],'male')
            return httpx.Response(200,text='{"type":"metadata","protocol":1,"sample_rate":24000}\n{"type":"audio","audio":"AAA="}\n{"type":"done","samples":1}\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=[x async for x in OmniBackend(client,'http://worker',voice='male').generate([{'role':'user','text':'hi'}])]
        self.assertEqual(result,[('audio',bytes(2))])

    async def test_original_audio_and_text_stream(self):
        data='\n'.join(json.dumps(x) for x in [
            {'type':'metadata','protocol':1,'sample_rate':24000},
            {'type':'text','text':'你好'}, {'type':'audio','audio':base64.b64encode(bytes(40)).decode()},
            {'type':'done','samples':20}])+'\n'
        def respond(request):
            self.assertEqual(json.loads(request.content)['messages'][-1]['text'],'hi')
            return httpx.Response(200,text=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=[x async for x in OmniBackend(client,'http://worker').generate([{'role':'user','text':'hi'}])]
        self.assertEqual(result,[('text','你好'),('audio',bytes(40))])

    async def test_truncation_and_wrong_sample_count_fail(self):
        for events in ([{'type':'metadata','protocol':1,'sample_rate':24000}],
                       [{'type':'metadata','protocol':1,'sample_rate':24000},{'type':'done','samples':1}]):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='\n'.join(map(json.dumps,events))+'\n'))) as client:
                with self.assertRaises(ValueError):
                    _=[e async for e in OmniBackend(client,'http://worker').generate([{'role':'user','text':'hi'}])]

    def test_unheard_reply_is_not_replayed_as_completed(self):
        h=ConversationHistory()
        h.begin({'role':'user','text':'记住暗号是蓝鲸'})
        h.generated('我已经提交订单')
        messages=h.begin({'role':'user','text':'暗号是什么'})
        self.assertIn('蓝鲸',messages[0]['text'])
        self.assertNotIn('提交订单',json.dumps(messages,ensure_ascii=False))
        self.assertIn('打断',messages[1]['text'])

    def test_heard_reply_and_bounded_history(self):
        h=ConversationHistory()
        for i in range(10):
            h.begin({'role':'user','text':str(i)});h.generated('ok');h.heard()
        messages=h.begin({'role':'user','text':'next'})
        self.assertLessEqual(len(messages),13)
        self.assertEqual(messages[-2]['text'],'ok')
        self.assertTrue(h.trimmed)
