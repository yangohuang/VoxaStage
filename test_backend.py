import unittest
import io
import json
import wave
from unittest.mock import patch
import httpx

from backend import LocalBackend, bounded_messages, pcm16_chunks


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_asr_diagnostics_preserve_audio_and_empty_transcription(self):
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\0' * 32000)
        original = output.getvalue()

        def handler(request):
            self.assertEqual(request.content, original)
            return httpx.Response(200, json={'text': '', 'elapsed_s': .2})

        with patch('backend.logger', create=True) as logger:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(await LocalBackend(client).transcribe(original), '')
            logger.info.assert_called_once()
            event = json.loads(logger.info.call_args.args[1])
            self.assertEqual(event['text'], '')
            self.assertEqual(event['audio_s'], 1)
            self.assertIsNone(event['rms_dbfs'])
            self.assertEqual(event['clipped_fraction'], 0)
            self.assertNotIn('audio', event)

    async def test_ndjson_stream_and_error(self):
        async def handler(request):
            self.assertEqual(request.url.path, '/generate')
            return httpx.Response(200, text='{"text":"你好"}\n{"error":"worker failed"}\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = LocalBackend(client)
            stream = backend.generate([{'role': 'user', 'content': '你好'}])
            self.assertEqual(await anext(stream), '你好')
            with self.assertRaisesRegex(RuntimeError, 'worker failed'):
                await anext(stream)

    async def test_pcm_reassembles_odd_transport_boundaries(self):
        async def source():
            for chunk in [b'\x01', b'\x02\x03', b'\x04']:
                yield chunk
        chunks = [chunk async for chunk in pcm16_chunks(source())]
        self.assertTrue(all(len(c) % 2 == 0 for c in chunks))
        self.assertEqual(b''.join(chunks), b'\x01\x02\x03\x04')

    async def test_rejects_truncated_pcm(self):
        async def source():
            yield b'\x01'
        with self.assertRaisesRegex(ValueError, 'truncated'):
            _ = [chunk async for chunk in pcm16_chunks(source())]

    def test_history_preserves_system_and_latest_turn(self):
        messages = [{'role': 'system', 'content': '中文助手'}]
        messages += [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': str(i)} for i in range(25)]
        result = bounded_messages(messages)
        self.assertLessEqual(len(result), 20)
        self.assertEqual(result[0], messages[0])
        self.assertEqual(result[-1], messages[-1])
        self.assertEqual(result[1]['role'], 'user')


if __name__ == '__main__':
    unittest.main()
