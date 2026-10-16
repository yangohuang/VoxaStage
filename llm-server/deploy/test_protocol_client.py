import importlib.util
import json
import os
from pathlib import Path
import unittest

import httpx


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).with_name('protocol_client.py')
        self.assertTrue(path.exists(), 'protocol collector missing')
        spec = importlib.util.spec_from_file_location('protocol_client', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.events = []
        self.admitted = []
        self.busy = False
        self.posts = []
        self.lines = [{'text': 'first'}, {'text': ' second'}, {'text': ' third'}]
        self.slow = False
        self.first_busy = True
        self.stuck = False

    def manifest(self, cases):
        return {'timeouts_s': {'startup': .2, 'request': .1, 'recovery': .2},
                'requests': [{'case': case, 'phase': 'formal', 'messages': [
                    {'role': 'user', 'content': case}], 'max_new_tokens': 48,
                    'cancel_after_first_text': case.startswith('cancel')} for case in cases]}

    def client(self):
        owner = self
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                import asyncio
                for line in owner.lines:
                    if owner.slow:
                        await asyncio.sleep(1)
                    yield line if isinstance(line, bytes) else (json.dumps(line) + '\n').encode()
            async def aclose(self):
                owner.busy = owner.stuck
        def handle(request):
            if request.url.path == '/health':
                return httpx.Response(200, json={'ready': True, 'busy': self.busy and self.first_busy})
            self.posts.append(json.loads(request.content))
            self.busy = True
            return httpx.Response(200, stream=Stream())
        return httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handle))

    async def admit(self, case):
        self.admitted.append(case)

    async def run_cases(self, cases):
        async with self.client() as client:
            return await self.module.collect(self.manifest(cases), client, self.events.append, self.admit)

    async def test_streaming_and_input_preserved(self):
        await self.run_cases(['long'])
        chunks = [x for x in self.events if x['event'] == 'chunk']
        done = [x for x in self.events if x['event'] == 'complete'][0]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(done['text'], 'first second third')
        self.assertLess(chunks[0]['elapsed_s'], done['elapsed_s'])
        self.assertEqual(self.admitted, ['long'])
        self.assertEqual(self.posts[0], {'messages': [{'role': 'user', 'content': 'long'}], 'max_new_tokens': 48})

    async def test_cancel_closes_after_first_text_then_recovers(self):
        await self.run_cases(['cancel-0', 'recovery-0'])
        chunks = [x for x in self.events if x['event'] == 'chunk' and x['case'] == 'cancel-0']
        self.assertEqual(len(chunks), 1)
        self.assertFalse(self.busy)
        self.assertEqual([x['case'] for x in self.events if x['event'] == 'complete'], ['cancel-0', 'recovery-0'])
        self.assertEqual(self.events[-1]['event'], 'finished')

    async def test_server_error_preserves_partial_text_and_stops(self):
        self.lines = [{'text': 'partial'}, {'error': 'generation_failed'}]
        with self.assertRaisesRegex(RuntimeError, 'generation_failed'):
            await self.run_cases(['one', 'two'])
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(any(x.get('text') == 'partial' for x in self.events))
        self.assertEqual(self.events[-1]['event'], 'failed')
        self.assertFalse(self.busy)

    async def test_empty_stream_is_failure(self):
        self.lines = []
        with self.assertRaisesRegex(RuntimeError, 'empty_response'):
            await self.run_cases(['one'])

    async def test_cancellation_requires_busy_evidence(self):
        self.first_busy = False
        with self.assertRaisesRegex(RuntimeError, 'not_busy_at_first_text'):
            await self.run_cases(['cancel-0'])

    async def test_wall_timeout_preserves_failure_and_closes_stream(self):
        self.slow = True
        with self.assertRaises(TimeoutError):
            await self.run_cases(['one'])
        self.assertFalse(self.busy)
        self.assertEqual(self.events[-1]['event'], 'failed')

    async def test_refused_admission_never_posts(self):
        async def refuse(case):
            raise RuntimeError('host_busy')
        self.admit = refuse
        with self.assertRaisesRegex(RuntimeError, 'host_busy'):
            await self.run_cases(['one'])
        self.assertEqual(self.posts, [])

    async def test_invalid_json_line_is_preserved(self):
        self.lines = [b'{truncated\n']
        with self.assertRaises(json.JSONDecodeError):
            await self.run_cases(['one'])
        self.assertTrue(any(x.get('line') == '{truncated' for x in self.events))

    async def test_lock_never_releases_stops_before_next_request(self):
        self.stuck = True
        with self.assertRaises(TimeoutError):
            await self.run_cases(['cancel-0', 'recovery-0'])
        self.assertEqual(len(self.posts), 1)

    async def test_long_reply_requires_multiple_chunks(self):
        self.lines = [{'text': 'one'}]
        with self.assertRaisesRegex(RuntimeError, 'long_reply_not_incremental'):
            await self.run_cases(['long'])

    async def test_partial_admission_line_has_a_wall_deadline(self):
        self.assertTrue(hasattr(self.module, 'read_ack'), 'bounded pipe reader missing')
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{"continue":')
            with self.assertRaisesRegex(TimeoutError, 'host_admission_timeout'):
                self.module.read_ack(read_fd, 'one', timeout=.02)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    async def test_eof_admission_never_posts(self):
        self.assertTrue(hasattr(self.module, 'read_ack'), 'bounded pipe reader missing')
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        async def admit(case):
            self.module.read_ack(read_fd, case, timeout=.02)
        self.admit = admit
        try:
            with self.assertRaisesRegex(RuntimeError, 'host_admission_eof'):
                await self.run_cases(['one'])
            self.assertEqual(self.posts, [])
        finally:
            os.close(read_fd)

    async def test_wrong_case_admission_never_posts(self):
        self.assertTrue(hasattr(self.module, 'read_ack'), 'bounded pipe reader missing')
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'{"continue":"different"}\n')
        async def admit(case):
            self.module.read_ack(read_fd, case, timeout=.02)
        self.admit = admit
        try:
            with self.assertRaisesRegex(RuntimeError, 'host_admission_refused'):
                await self.run_cases(['one'])
            self.assertEqual(self.posts, [])
        finally:
            os.close(read_fd)
            os.close(write_fd)


if __name__ == '__main__':
    unittest.main()
